# GitHub workflow and clone setup

Owner: Justin Nunn (`@skyparkjustin`). Organization: `spsvent`.
Track the organization rollout in [SkyPark#101](https://github.com/spsvent/SkyPark/issues/101).
The repository-specific instructions are in [AGENTS.md](../AGENTS.md);
Claude imports them from CLAUDE.md.

## One-time setup in each clone

After reviewing the tracked hook source, use Python 3 and Git:

```sh
git config --local user.email "YOUR_WORK_EMAIL@skyparksantasvillage.com"
git remote -v
gh auth status
sh tools/install-git-hooks.sh
```

Use your own work email and name. Git identity, transport credentials, gh login,
and a cloud agent's selected account are separate settings.
The installer refuses an existing custom hooksPath or active hooks; integrate
those safeguards with Justin rather than overwriting them.
It does not change any global identity settings or log in to GitHub.

The offline guard checks effective author/committer emails, exact org/repository
origin and resolved push URLs (including URL rewrites), the actual push target,
and default/main/master branch refs. Standard commit, merge, patch and message
entrypoints are covered. It recognizes github.com and the github.com-skypark SSH
alias. It blocks detached-HEAD commits. It does not scan existing commit history,
prove account ownership, scan secrets, or cover API/UI commits. Hooks are advisory:
GitHub protection and independent human review remain necessary.

## Every task

1. Open an issue or use a specifically relevant cross-repo tracking issue.
2. Fetch and branch from the actual default branch named in .githooks/policy.json.
3. Make focused changes and run the project's relevant checks.
4. Open a PR with What changed / Why / How I tested it and the issue link.
5. Obtain independent review. Production changes require a human approval.
6. Squash merge, remove the branch, and record a deployed commit/tag plus rollback
   point when a separately approved deployment actually occurs.
7. Leave a handoff using the template in docs/handoffs/README.md.

Do not run deployment scripts just to test a documentation change. No runtime
commands are supplied here because each project has its own setup and safety rules.

## Adoption status

This baseline becomes available to new clones only after PR review and merge.
Hooks must then be enabled in each clone. CODEOWNERS attribution alone does not
enforce approval; private-repo feature entitlement and server settings must be
confirmed. A second eligible human reviewer is needed for Justin's own PRs.
Ignore additions do not remove already tracked secrets. Org settings, credentials,
membership/2FA, repo visibility, deployments and old history are separate work.

## Agent entry points and server checks

AGENTS.md is the shared policy for every agent. Claude imports it from CLAUDE.md;
Gemini imports it from GEMINI.md. Copilot's repository instruction file and Cursor's
always-apply rule direct the agent to the same policy. Any other runner must load
AGENTS.md explicitly before allowing edits. The startup acknowledgement is an
observable convention, not proof that a model internally followed every instruction.

The SkyPark agent baseline workflow checks that entry points, ownership and hook
configuration remain present and consistent. Configure this as a required check
once plan entitlement/admin access permits. No file can force every external agent
to read it; least-privilege app access, protected branches, required checks and
independent owner review provide the enforceable controls. Existing sessions need
to reload the instructions after the reviewed baseline is merged.

Vendor references: [Copilot](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions),
[Gemini](https://geminicli.com/docs/cli/gemini-md/),
[Cursor](https://cursor.com/docs/rules).
