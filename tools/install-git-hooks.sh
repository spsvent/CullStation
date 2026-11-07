#!/bin/sh
# Install only after reviewing the tracked hooks. Never replace existing hooks silently.
set -eu
root=$(git rev-parse --show-toplevel)
configured=$(git config --local --get core.hooksPath || true)
effective=$(git config --get core.hooksPath || true)
if [ "$effective" != .githooks ] && { [ -n "$effective" ] || [ -n "$configured" ]; }; then
    echo 'Existing hooksPath found; review and integrate it manually before installing.' >&2
    exit 1
fi
hooks_dir=$(git rev-parse --git-path hooks)
if [ "$effective" != .githooks ]; then
for existing in "$hooks_dir"/*; do
    case "$existing" in *.sample) continue ;; esac
    if [ -f "$existing" ] && [ -x "$existing" ]; then
        echo 'Existing active hook found; review and integrate it manually before installing.' >&2
        exit 1
    fi
done
fi
for name in pre-commit pre-push pre-merge-commit prepare-commit-msg pre-applypatch; do
    if [ ! -x "$root/.githooks/$name" ]; then
        echo 'Tracked SkyPark hook is missing or not executable.' >&2
        exit 1
    fi
done
command -v python3 >/dev/null
test -f "$root/.githooks/check_identity.py"
test -f "$root/.githooks/policy.json"
git config --local core.hooksPath .githooks
echo 'SkyPark hooks enabled for this clone. Server-side PR rules are still required.'
