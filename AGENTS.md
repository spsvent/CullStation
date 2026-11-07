# AGENTS.md

## Required agent startup

At the start of every session accessing this repository, including read-only
inspection, search, review and editing, every human-operated or autonomous agent must:
1. Read this root AGENTS.md in full, then applicable nested AGENTS.md files and
   the project run/test instructions. Load the current branch handoff if one exists.
2. Confirm the canonical repository in .githooks/policy.json, the current branch,
   selected GitHub account, and effective Git author/committer identity.
3. State which instruction files were loaded and the intended scope before making
   changes. If the runner cannot load the instructions, stop before modifying files.
4. Follow these rules throughout the session and leave a handoff when stopping.

CLAUDE.md, GEMINI.md, Copilot instructions and the Cursor always-apply rule are
entry points to this shared rulebook, not separate policies. New agents/runners
must explicitly load AGENTS.md if they do not discover it automatically.
Do not remove or weaken the instruction entry points, hooks or baseline check
as a shortcut. Changes to these safeguards require independent owner review.



## SkyPark repository workflow

Owner: Justin Nunn (`@skyparkjustin`). Each employee uses their own authorized
work GitHub account; the owner's account is not a shared login.
- Work in an issue branch; reference the local issue or the cross-repo tracking issue.
  Never commit/push directly to the default branch, force-push, rewrite history,
  or bypass checks with `--no-verify`. Do not merge without independent review.
- Use the work commit email (`@skyparksantasvillage.com`) and this repository's
  exact `spsvent` origin and push destinations. Check the selected account for
  API/cloud work too. Never create a repository without the `spsvent/` owner.
- Production changes need human approval; a merge may auto-deploy this project.
  Do not execute a live command merely to verify documentation.
- Never commit private keys, tokens, real environment files, connection strings,
  or guest/passholder exports. Use approved secret stores/runtime environment variables.
  Existing tracked credentials require coordinated rotation and cleanup, not only ignores.
- Install the reviewed clone-local guard using `sh tools/install-git-hooks.sh`.
  Preserve existing hooks; do not replace a custom hooksPath to get around the installer.
  Hooks do not run for GitHub API/UI writes and do not replace server-side review rules.
- Run relevant verification before claiming success. Before stopping, update
  `docs/handoffs/<branch-with-slashes-replaced-by-hyphens>.md` with
  Goal / Done / Next / Verify / Gotchas, and link the issue/PR.
- Read [docs/GITHUB-WORKFLOW.md](docs/GITHUB-WORKFLOW.md) for setup and limitations.
  Existing stricter project-specific safety and production rules below still apply.


NAS photo/video review, conversion and culling tools.

## Phase boundaries

This rollout implements Phase 1 of the employee workflow guide. Buzz and shared
team agents are Phase 2 only; do not provision them or create a skypark-bot account
as part of repository work. A future shared agent needs its own approved GitHub
identity and IT API credentials, never a person's subscription login. Any future
bot identity must be explicitly configured and reviewed; do not add a blanket
bot/noreply identity bypass to the clone guards.
