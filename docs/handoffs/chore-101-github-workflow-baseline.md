# chore/101-github-workflow-baseline

**Goal:** Adopt the employee GitHub workflow. Tracking: spsvent/SkyPark#101.
**Done:** Prepared shared agent rules/import, ownership, PR template, secret ignores,
clone-local identity guards and handoff documentation. Existing project context is retained.
**Next:** Review this PR; merge only with independent approval. Enable the reviewed
hooks in each clone. Confirm server-side protection entitlement and second reviewer.
**Verify:** The common guard passed 28 tests covering real Git operations and instruction checks in the SkyPark toolkit.
Check the exact PR diff; no deployed application code or existing production configuration changes.
**Gotchas:** This is a proposed baseline, not active server enforcement. Hooks need
per-clone setup; existing custom hooks must be integrated. No credentials rotated,
tracked runtime secrets removed, history rewritten, visibility changed or services restarted.
