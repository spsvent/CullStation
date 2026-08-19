"""ProRes(.mov) -> HEVC transcode worker: pausable/resumable, per-file progress,
off-hours scheduler, prevent-sleep. NEVER deletes or modifies a source file."""
import os, json, time, threading, subprocess, platform, glob, atexit, tempfile
import config as C

MIN_SRC_BYTES = 50 * 1024 * 1024  # skip tiny clips

def _ffprobe_duration(p):
    try:
        r = subprocess.run([C.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", p], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        s = r.stdout.strip()
        return float(s) if s and s != "N/A" else None
    except Exception:
        return None

def _ffmpeg_has_encoder(name):
    try:
        r = subprocess.run([C.FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and name in r.stdout
    except Exception:
        return False

# Quality presets -> encoder params (user picks a word, not CRF/q numbers)
VT_Q   = {"high": "70", "balanced": "62", "smaller": "50"}
X265_Q = {"high": "16", "balanced": "19", "smaller": "23"}

# Encode attempts, most faithful first. An NLE/camera export can carry a track the mov
# muxer refuses next to HEVC ("Could not write header (incorrect codec parameters ?)"),
# which used to write the whole clip off as FAIL. Each retry narrows what we carry over;
# whichever attempt succeeds is recorded on the clip so nothing is silently degraded.
ATTEMPTS = (
    {"name": "all streams",
     "why": "everything the source carries",
     "map": ["-map", "0", "-map_metadata", "0"], "aud": ["-c:a", "copy", "-c:s", "copy"]},
    {"name": "video+audio only",
     "why": "dropped data/subtitle/extra tracks the mov muxer rejected",
     "map": ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-dn", "-sn"], "aud": ["-c:a", "copy"]},
    {"name": "audio re-encoded (AAC 320k)",
     "why": "the source audio codec can't live in a .mov, so it was re-encoded",
     "map": ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-dn", "-sn"],
     "aud": ["-c:a", "aac", "-b:a", "320k"]},
)

# Only these failures can plausibly be fixed by carrying less over. Anything else (an
# unreadable source, a full disk, a dead encoder) fails on the first attempt rather than
# burning two more encodes to reach the same answer.
_RETRYABLE = ("could not write header", "incorrect codec parameters", "only supported in",
              "automatic encoder selection failed", "could not find tag for codec",
              "unsupported codec", "muxer does not support", "codec not currently supported in container")

def _retryable(err):
    e = (err or "").lower()
    return any(m in e for m in _RETRYABLE)

class EncodeError(RuntimeError):
    """Carries the RAW ffmpeg stderr alongside the display message — the retry decision
    must read the whole thing, not the shortened version shown in the UI."""
    def __init__(self, msg, raw=""):
        super().__init__(msg)
        self.raw = raw

# x265/x264 chatter on stderr is a banner, not an error; it would otherwise crowd the
# actual diagnosis out of the message we keep.
_NOISE = ("x265 [info]:", "x265 [warning]:", "x264 [info]:", "x264 [warning]:")

def _squeeze_err(err, cap=1200):
    """ffmpeg puts the ACTIONABLE line first ('opus only supported in MP4') and the
    consequences after it. Keeping only the tail — as this used to — threw the diagnosis
    away and left an unreadable stack of 'Invalid argument'. Keep head AND tail."""
    lines = [l.strip() for l in (err or "").splitlines()
             if l.strip() and not l.strip().startswith(_NOISE)]
    if not lines:
        lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    if not lines:
        return ""
    keep = lines if len(lines) <= 10 else lines[:7] + ["…"] + lines[-2:]
    out = " | ".join(keep)
    return out if len(out) <= cap else out[:cap - 1] + "…"

def _video_meta(src):
    """(year, duration) for a source clip. Year from embedded creation_time (authoritative
    shoot date), else a leading-year path folder, else file mtime."""
    year = dur = None
    try:
        r = subprocess.run([C.FFPROBE, "-v", "error",
                            "-show_entries", "format=duration:format_tags=creation_time",
                            "-of", "json", src], capture_output=True, text=True, timeout=60)
        fmt = (json.loads(r.stdout or "{}").get("format") or {})
        d = fmt.get("duration"); dur = float(d) if d and d != "N/A" else None
        ct = (fmt.get("tags") or {}).get("creation_time", "")
        if len(ct) >= 4 and ct[:4].isdigit():
            year = int(ct[:4])
    except Exception:
        pass
    if year is None:
        for part in src.split(os.sep):
            if len(part) >= 4 and part[:4].isdigit() and 1990 < int(part[:4]) < 2100:
                year = int(part[:4]); break
    if year is None:
        try: year = time.localtime(os.path.getmtime(src)).tm_year
        except Exception: pass
    return year, dur

def _is_hevc(p):
    try:
        r = subprocess.run([C.FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "default=nk=1:nw=1", p],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and "hevc" in r.stdout.lower()
    except Exception:
        return False

class VideoWorker:
    def __init__(self):
        self.lock = threading.RLock()   # guards ALL state["done"] iteration + mutation (reentrant: _run holds it across assign+save)
        self.thread = None
        self.pause = threading.Event()
        self.stop = threading.Event()
        self.current = None
        self.cur_pct = 0.0
        self.caffeinate = None
        self.state = self._load_state()
        self.cfg = self._load_cfg()
        self.keep = self._load_keep()   # rel paths to KEEP as full-quality originals (skip transcode)
        self.last_error = None
        # Auto-pick the encoder: hardware VideoToolbox if this ffmpeg has it (the Mac),
        # else software x265 — so a missing encoder can never silently fail every file.
        forced = os.environ.get("CULL_VIDEO_ENCODER")
        if forced in ("hevc_videotoolbox", "libx265"):
            self.encoder = forced
        elif _ffmpeg_has_encoder("hevc_videotoolbox"):
            self.encoder = "hevc_videotoolbox"
        else:
            self.encoder = "libx265"
        self._files = None
        self._files_t = 0
        # Safety: output tree must not equal or live inside the source tree, or an
        # encoded file could be re-discovered as a source (or overwrite an original).
        s, o = os.path.realpath(C.VIDEO_SRC), os.path.realpath(C.VIDEO_OUT)
        self.config_error = ("VIDEO_OUT must not be inside VIDEO_SRC"
                             if (o == s or o.startswith(s + os.sep)) else None)
        atexit.register(self._prevent_sleep, False)  # never orphan caffeinate

    # ---------- persistence ----------
    def _load_state(self):
        if os.path.exists(C.VIDEO_STATE):
            try:
                with open(C.VIDEO_STATE) as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {"done": {}, "din": 0, "dout": 0}   # ok/fail are recomputed from done in summary()

    def _save_state(self):
        try:
            # Snapshot under the lock (cheap, in-memory) so we never iterate state["done"]
            # while _run mutates it — then do the json.dump + fsync-to-NAS OUTSIDE the lock,
            # so a slow network fsync can't stall summary()/set_cfg (which the UI polls every 4s).
            with self.lock:
                snap = {**self.state, "done": dict(self.state["done"])}
            _write_json(C.VIDEO_STATE, snap)   # unique-temp write (below) is safe under concurrency; fsync stays off the lock
        except Exception as e:
            print(f"[video] failed to persist state: {e}")

    def _cfg_path(self):
        return os.path.join(os.path.dirname(C.VIDEO_STATE) or ".", "_video_config.json")

    def _keep_path(self):
        return os.path.join(os.path.dirname(C.VIDEO_STATE) or ".", "_video_keep.json")

    def _load_keep(self):
        try:
            with open(self._keep_path()) as fh:
                return set(json.load(fh))
        except Exception:
            return set()

    def set_keep(self, rel, keep):
        if not isinstance(rel, str):
            return {"error": "rel required"}
        (self.keep.add if keep else self.keep.discard)(rel)
        try:
            _write_json(self._keep_path(), sorted(self.keep))
        except Exception as e:
            print(f"[video] keep save failed: {e}")
        return {"rel": rel, "keep": rel in self.keep, "total_kept": len(self.keep)}

    def _load_cfg(self):
        d = {"quality": "balanced", "schedule_enabled": False, "start_hour": 22, "end_hour": 7,
             "cutoff_year": 2020}   # only convert ProRes from this year and older
        try:
            with open(self._cfg_path()) as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                d.update({k: saved[k] for k in d if k in saved})   # ignore stale/unknown keys
        except Exception:
            pass
        if d["quality"] not in VT_Q:
            d["quality"] = "balanced"
        return d

    def set_cfg(self, patch):
        # Only a friendly Quality preset + an optional schedule (encoder is auto-detected).
        if patch.get("quality") in VT_Q:
            self.cfg["quality"] = patch["quality"]
        if "schedule_enabled" in patch:
            self.cfg["schedule_enabled"] = bool(patch["schedule_enabled"])
        for k in ("start_hour", "end_hour"):
            if k in patch:
                try:
                    self.cfg[k] = max(0, min(23, int(patch[k])))   # bad hour can't crash status
                except (ValueError, TypeError):
                    pass
        if "cutoff_year" in patch:
            try:
                ny = int(patch["cutoff_year"])
                if not (1990 <= ny <= 2100):   # ignore implausible values (e.g. a blank field → 0)
                    ny = None                  # rather than clamping 0→1990 and skipping everything
            except (ValueError, TypeError):
                ny = None
            if ny is not None and ny != self.cfg.get("cutoff_year"):
                self.cfg["cutoff_year"] = ny
                # re-triage clips previously skipped by the old cutoff. The lock is REQUIRED:
                # this comprehension iterates state["done"] while the _run thread may be
                # inserting keys — without it CPython raises "dict changed size during iteration".
                with self.lock:
                    self.state["done"] = {k: v for k, v in self.state["done"].items()
                                          if v.get("status") != "SKIP_DATE"}
                self._save_state()   # outside the lock: its fsync must not run under the hold
        try:
            _write_json(self._cfg_path(), self.cfg)
        except Exception as e:
            print(f"[video] failed to persist config: {e}")
        return self.cfg

    # ---------- discovery ----------
    def list_files(self):
        if self._files is not None and (time.time() - self._files_t) < 60:
            return self._files
        out_rp = os.path.realpath(C.VIDEO_OUT)
        files = []
        for dp, _, fns in os.walk(C.VIDEO_SRC):
            rdp = os.path.realpath(dp)
            if rdp == out_rp or rdp.startswith(out_rp + os.sep):   # never treat outputs as sources
                continue
            for fn in fns:
                if fn.lower().endswith(".mov"):
                    files.append(os.path.join(dp, fn))
        files.sort()
        self._files, self._files_t = files, time.time()
        return files

    def queue(self, limit=300):
        out = []
        for f in self.list_files()[:limit]:
            d = self.state["done"].get(f)
            rel = os.path.relpath(f, C.VIDEO_SRC); kept = rel in self.keep
            out.append({"rel": rel, "keep": kept,
                        "status": "KEEP" if kept else (d or {}).get("status", "PENDING"),
                        "src_mb": round((d or {}).get("src_bytes", _safe_size(f)) / 1e6),
                        "out_mb": round((d or {}).get("out_bytes", 0) / 1e6) or None})
        return out

    # ---------- failures: list, explain, retry ----------
    def _contained_src(self, rel):
        """Resolve a caller-supplied rel path inside VIDEO_SRC, or None."""
        if not isinstance(rel, str) or not rel:
            return None
        p = os.path.realpath(os.path.join(C.VIDEO_SRC, rel))
        root = os.path.realpath(C.VIDEO_SRC)
        return p if (p == root or p.startswith(root + os.sep)) else None

    def failed(self, limit=100):
        """Clips that failed, with the full ffmpeg message — the dashboard lists these
        instead of showing one truncated error blob."""
        with self.lock:
            items = [(k, v) for k, v in self.state["done"].items()
                     if str(v.get("status", "")).startswith("FAIL")]
        items.sort(key=lambda kv: kv[1].get("ts", 0), reverse=True)
        out = [{"rel": os.path.relpath(k, C.VIDEO_SRC), "status": v.get("status"),
                "err": v.get("err", ""), "ts": v.get("ts"),
                "src_mb": round(v.get("src_bytes", 0) / 1e6) or None} for k, v in items[:limit]]
        return {"count": len(items), "items": out}

    def fallbacks(self, limit=100):
        """Clips that only encoded after dropping/re-encoding something — surfaced so a
        compromise is never invisible."""
        with self.lock:
            items = [(k, v) for k, v in self.state["done"].items() if v.get("via")]
        out = [{"rel": os.path.relpath(k, C.VIDEO_SRC), "via": v.get("via"),
                "why": v.get("via_why", "")} for k, v in items[:limit]]
        return {"count": len(items), "items": out}

    def diagnose(self, rel):
        """ffprobe a source clip: which tracks it carries is what decides whether the mov
        muxer can hold them, so this is the answer to 'why did this one fail?'."""
        p = self._contained_src(rel)
        if not p:
            return {"error": "path outside the video source tree"}
        if not os.path.isfile(p):
            return {"error": "file not found (is the share mounted?)"}
        try:
            r = subprocess.run([C.FFPROBE, "-v", "error", "-show_streams", "-show_format",
                                "-of", "json", p], capture_output=True, text=True, timeout=90)
            d = json.loads(r.stdout or "{}")
        except Exception as e:
            return {"error": f"ffprobe failed: {e}"}
        if r.returncode != 0 or not d.get("streams"):
            # a clip ffprobe can't even read is the diagnosis — say so instead of
            # rendering an empty stream list.
            why = _squeeze_err(r.stderr, cap=300) or "no streams found"
            return {"error": f"ffprobe could not read this file — {why}"}
        streams = [{k: st.get(k) for k in ("index", "codec_type", "codec_name", "codec_tag_string",
                                           "profile", "pix_fmt", "width", "height", "r_frame_rate",
                                           "channels", "sample_rate", "nb_frames")
                    if st.get(k) is not None} for st in d.get("streams", [])]
        fmt = d.get("format") or {}
        return {"rel": rel, "streams": streams,
                "format": {k: fmt.get(k) for k in ("format_name", "duration", "size", "bit_rate")
                           if fmt.get(k) is not None},
                "encoder": self.encoder}

    def retry(self, rel):
        """Forget a clip's FAIL record so the next run re-attempts it (with the fallback
        ladder). Never touches the source file."""
        p = self._contained_src(rel)
        if not p:
            return {"error": "path outside the video source tree"}
        with self.lock:
            prev = self.state["done"].pop(p, None)
            if prev and prev.get("status") == "DONE":
                # this clip's bytes are already in the running totals; leaving them there
                # would double-count them when it re-encodes.
                self.state["din"] = max(0, self.state.get("din", 0) - prev.get("src_bytes", 0))
                self.state["dout"] = max(0, self.state.get("dout", 0) - prev.get("out_bytes", 0))
        if prev is None:
            return {"rel": rel, "cleared": False, "note": "no recorded result for this clip"}
        self._save_state()
        if self.last_error and rel in self.last_error:
            self.last_error = None
        return {"rel": rel, "cleared": True, "was": prev.get("status"),
                "running": bool(self.thread and self.thread.is_alive())}

    # ---------- scheduler / sleep ----------
    def in_window(self):
        if not self.cfg.get("schedule_enabled"):
            return True
        try:
            a, b = int(self.cfg.get("start_hour", 20)), int(self.cfg.get("end_hour", 7))
        except (ValueError, TypeError):
            return True   # never wedge the dashboard/worker on a bad persisted value
        if a == b:
            return True   # zero-length window would otherwise disable encoding forever
        h = time.localtime().tm_hour
        return (a <= h or h < b) if a > b else (a <= h < b)

    def _prevent_sleep(self, on):
        if platform.system() != "Darwin":
            return
        if on and self.caffeinate is None:
            try:
                self.caffeinate = subprocess.Popen([C.CAFFEINATE, "-dimsu"])
            except Exception:
                self.caffeinate = None
        elif not on and self.caffeinate is not None:
            try:
                self.caffeinate.terminate()
            except Exception:
                pass
            self.caffeinate = None

    # ---------- encode + verify ----------
    def _encode(self, src, tmp, duration, attempt=0):
        a = ATTEMPTS[attempt]
        q = self.cfg.get("quality", "balanced")
        if self.encoder == "libx265":
            venc = ["-c:v", "libx265", "-crf", X265_Q.get(q, "19"), "-preset", "medium",
                    "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]
        else:
            venc = ["-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-pix_fmt", "p010le",
                    "-q:v", VT_Q.get(q, "62"), "-tag:v", "hvc1"]
        # `-f mov` is REQUIRED: the temp name ends in ".partial", so ffmpeg can't infer
        # the muxer from the extension. Without this every encode fails. (review finding)
        cmd = [C.FFMPEG, "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
               "-i", src] + a["map"] + venc + a["aud"] + \
              ["-movflags", "+faststart", "-f", "mov", tmp]
        # Capture stderr to a temp file (avoids pipe deadlock) so a FAIL record carries the
        # real ffmpeg error (missing encoder, unsupported -q:v, etc.) instead of just an exit code.
        errf = tmp + ".log"
        with open(errf, "w") as ef:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, text=True)
            for line in p.stdout:
                if line.startswith("out_time_us=") and duration:
                    try:
                        self.cur_pct = min(99.9, 100 * (int(line.split("=")[1]) / 1e6) / duration)
                    except Exception:
                        pass
            p.wait()
        err = ""
        try:
            with open(errf) as f:
                err = f.read().strip()
            os.remove(errf)
        except Exception:
            pass
        if p.returncode != 0:
            raise EncodeError(f"ffmpeg rc={p.returncode} [{a['name']}]: {_squeeze_err(err)}", raw=err)

    def _encode_fallback(self, src, tmp, duration, rel):
        """Run the attempt ladder; return the attempt that worked. Raises the LAST error
        (which carries every attempt's ffmpeg message) only if all of them fail."""
        errs = []
        for i, a in enumerate(ATTEMPTS):
            try:
                _rm(tmp)
                self._encode(src, tmp, duration, i)
                return a
            except Exception as e:
                errs.append(str(e))
                if not _retryable(getattr(e, "raw", "") or str(e)):
                    break            # a narrower map can't fix this one
                if i + 1 < len(ATTEMPTS):
                    self.last_error = f"{rel}: '{a['name']}' failed, retrying as '{ATTEMPTS[i+1]['name']}'"
                    print(f"[video] {self.last_error}")
        raise RuntimeError(" ‖ ".join(errs))

    def _verify(self, src, out, src_dur):
        """(ok, reason). The reason is recorded on a FAIL_VERIFY so 'output failed
        verification' is never the whole story — an unreadable source, a duration mismatch
        and a non-HEVC output need different responses."""
        if not os.path.getsize(out):
            return False, "encoder produced an empty file"
        do = _ffprobe_duration(out)
        if do is None:
            return False, "could not read the duration of the encoded file"
        if src_dur is None:
            return False, "could not read the duration of the SOURCE, so the output can't be checked against it"
        if abs(src_dur - do) > 0.6:
            return False, f"duration mismatch: source {src_dur:.1f}s vs output {do:.1f}s"
        if not _is_hevc(out):   # confirm we actually wrote HEVC, not a silent fallback
            return False, "output is not HEVC (the encoder fell back to another codec)"
        return True, ""

    # ---------- main loop ----------
    def _run(self):
        if self.config_error:
            return
        # sweep orphan *.partial (and their *.partial.log) left by a previous hard kill
        try:
            for f in glob.glob(os.path.join(C.VIDEO_OUT, "**", "*.partial*"), recursive=True):
                _rm(f)
        except Exception:
            pass
        self._prevent_sleep(True)
        try:
            for src in self.list_files():
                if self.stop.is_set():
                    break
                if self.pause.is_set() or not self.in_window():
                    self.current = None   # not actually encoding while paused/out-of-window
                    while (self.pause.is_set() or not self.in_window()) and not self.stop.is_set():
                        time.sleep(2)
                if self.stop.is_set():
                    break
                rel = os.path.relpath(src, C.VIDEO_SRC)
                if rel in self.keep:
                    continue   # user marked this clip to keep as full-quality original
                prev = self.state["done"].get(src)
                if prev and prev.get("status") in ("DONE", "SKIP_SMALL", "SKIP_DATE"):
                    continue   # already finished/triaged; FAIL/FAIL_VERIFY are retried on restart
                sb = _safe_size(src)
                if sb < MIN_SRC_BYTES:
                    with self.lock:
                        self.state["done"][src] = {"status": "SKIP_SMALL"}
                    self._save_state(); continue
                # Date filter: only convert ProRes from the cutoff year and older. ONE ffprobe
                # yields both the shoot year and the duration (reused by the encode — no 2nd probe).
                year, dur = _video_meta(src)
                cutoff = self.cfg.get("cutoff_year", 2020)
                if year and year > cutoff:
                    with self.lock:
                        self.state["done"][src] = {"status": "SKIP_DATE", "year": year}
                    self._save_state(); continue
                out = os.path.join(C.VIDEO_OUT, os.path.splitext(rel)[0] + ".mov"); tmp = out + ".partial"
                os.makedirs(os.path.dirname(out), exist_ok=True)
                self.current, self.cur_pct = rel, 0.0
                try:
                    used = self._encode_fallback(src, tmp, dur, rel)
                    vok, vwhy = self._verify(src, tmp, dur)
                    if vok:
                        os.replace(tmp, out); ob = os.path.getsize(out)
                        rec = {"status": "DONE", "src_bytes": sb, "out_bytes": ob}
                        if used is not ATTEMPTS[0]:   # note the compromise, never silently
                            rec["via"] = used["name"]; rec["via_why"] = used["why"]
                            self.last_error = f"{rel}: encoded on fallback — {used['why']}"
                        with self.lock:
                            self.state["done"][src] = rec
                            self.state["din"] += sb; self.state["dout"] += ob
                    else:
                        _rm(tmp)
                        with self.lock:
                            self.state["done"][src] = {"status": "FAIL_VERIFY", "src_bytes": sb,
                                                       "err": f"output failed verification — {vwhy}",
                                                       "ts": int(time.time())}
                        self.last_error = f"{rel}: output failed verification — {vwhy}"
                except Exception as e:
                    _rm(tmp)
                    with self.lock:
                        self.state["done"][src] = {"status": "FAIL", "src_bytes": sb,
                                                   "err": str(e)[:1500], "ts": int(time.time())}
                    self.last_error = f"{rel}: {str(e)[:300]}"
                self._save_state()
        finally:
            self.current, self.cur_pct = None, 0.0
            self._prevent_sleep(False)

    def start(self):
        with self.lock:
            if self.config_error:
                return
            if self.thread and self.thread.is_alive():
                # cancel any pending stop/pause so Start during a stop-drain actually resumes
                self.stop.clear(); self.pause.clear(); return
            self.stop.clear(); self.pause.clear()
            self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()

    def do_pause(self): self.pause.set()
    def resume(self): self.pause.clear()
    def do_stop(self): self.stop.set(); self.pause.clear()

    def summary(self):
        # NOTE: must stay cheap — polled every few seconds. Do NOT walk the 8TB tree here;
        # use the cached file count if available (populated lazily by list_files/queue/start).
        s, running = self.state, bool(self.thread and self.thread.is_alive())
        # actionable total = discovered clips minus the ones marked keep (which are never processed),
        # so "done" can actually reach "total".
        if self._files is not None:
            total = sum(1 for f in self._files if os.path.relpath(f, C.VIDEO_SRC) not in self.keep)
        else:
            total = None
        # snapshot under the lock: _run mutates state["done"] from another thread, and
        # iterating it live would raise "dict changed size during iteration".
        with self.lock:
            dd = list(s.get("done", {}).values())
        ndone = len(dd)
        nok = sum(1 for v in dd if v.get("status") == "DONE")
        nfail = sum(1 for v in dd if str(v.get("status", "")).startswith("FAIL"))
        nskip_date = sum(1 for v in dd if v.get("status") == "SKIP_DATE")
        nvia = sum(1 for v in dd if v.get("via"))   # encoded, but only after a fallback
        return {"running": running, "paused": self.pause.is_set(), "config_error": self.config_error,
                "in_window": self.in_window(), "current": self.current, "current_pct": round(self.cur_pct, 1),
                "total": total, "done": ndone, "ok": nok, "fail": nfail,
                "skipped_date": nskip_date, "fallback": nvia,
                "cutoff_year": self.cfg.get("cutoff_year", 2020),
                "in_gb": round(s.get("din", 0) / 1e9, 1), "out_gb": round(s.get("dout", 0) / 1e9, 2),
                "reclaimed_gb": round((s.get("din", 0) - s.get("dout", 0)) / 1e9, 1),
                "encoder": self.encoder, "quality": self.cfg.get("quality"), "last_error": self.last_error,
                "kept": len(self.keep), "cfg": self.cfg, "src": C.VIDEO_SRC, "out": C.VIDEO_OUT}

def _safe_size(p):
    try: return os.path.getsize(p)
    except Exception: return 0
def _rm(p):
    try: os.remove(p)
    except Exception: pass
def _write_json(path, obj):
    # Unique temp per writer: two concurrent saves (e.g. the _run thread + a set_cfg request)
    # must never share one ".tmp" name, or one's rename races the other into a torn/missing file.
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)   # atomic; concurrent replaces each leave a complete valid file (last wins)
    except Exception:
        try: os.remove(tmp)
        except Exception: pass
        raise
