# Git / GitHub workspace integration

OLCR's project workspace must be the Git repository root. Status, worktree/index diff, recent history and configured remotes are available in the Git / GitHub panel above the chat composer. Projectless conversations first need a project with a workspace. No repository is selected by model-generated paths.

## User flow

1. Open a project conversation and expand Git / GitHub. Refresh status.
2. Select individual changed files and enter a commit message. Review commit creates a persisted pending operation with the complete selected diff; it does not change HEAD or the normal index.
3. Approve and execute commits the reviewed tree. Other staged changes and all worktree file contents are preserved. Selected files are committed as complete files, including any earlier edits the user explicitly selects. Repo hooks are disabled for this execution path; configured commit signing still applies.
4. Choose a configured GitHub remote and destination branch, then Review push. External Access must be ON. This reads the remote branch tip to show outgoing commits. If that object is absent locally, the review explicitly labels the complete local history instead of claiming an exact outgoing set.
5. Approve and execute pushes the pinned commit SHA. No force option, branch deletion, tags, clone, automatic pull/rebase, PR publication or config/authentication mutation is exposed.

Existing Git credential helpers and SSH authentication are used. OLCR does not request or store a GitHub token for Git operations. If authentication is missing, the panel links to https://cli.github.com/manual/gh_auth_login and shows `gh auth login --hostname github.com && gh auth setup-git` for the user to run. A configured helper is not reported as proof of authenticated access; remote review/push verifies actual access. Credential helper dialogs belong to the system. Terminal password prompts are disabled and missing credentials return an actionable Git error.

Supported push URLs: credential-free HTTPS, `git@github.com:owner/repo.git`, and `ssh://git@github.com/owner/repo.git`. Multiple push destinations and URL rewrites require user configuration changes before a reviewed push. This first implementation supports github.com, not GitHub Enterprise hosts.

## Chat and Coding Orchestrator

`/git status`, `/git connect`, `/git diff`, `/git log`, `/git commit {"paths":["file.py"],"message":"Fix bug"}`, `/git push [remote] [branch]`, `/git approve <operation-id>` and `/git reject <operation-id>` use the same service as the panel. Exact Japanese Git status/diff/push requests also enter this route. Existing `software.github` is unchanged and remains a metadata lookup API.

When a coding request explicitly asks to commit/push, the planning prompt delegates Git execution to the host after implementation/verification. The host records initially dirty files, reads verified changed-file evidence and the exact file manifest, and prepares a commit review for eligible task files. Previously dirty files are excluded from automatic selection; missing provenance requires manual selection. Coding completion reports Git work as pending, rather than claiming it already ran. A requested follow-on push creates a separate review after the commit, never silently publishes. Failed Git work does not rerun the coding task.

## Execution and persistence

`git_workspace.py` owns fixed Git argument lists and a scrubbed subprocess environment. Preview/commit use an isolated index. After committing that frozen tree, only selected paths in the normal index are reconciled. Failures after commit retain the commit SHA and are reported as succeeded with warning, preventing duplicate retries. Reviews bind the real workspace, branch, HEAD, full index diff, worktree status, selected file bytes and (for push) the exact remote URL, destination branch and remote branch tip. These are checked again on approval. Changed state makes the review stale. Approvals expire after one hour.

`git_api.py` provides authenticated-host API routes and chat/coding adapters. Operations persist in the application's SQLite DB with conversation/task IDs, review contents, status and result. A restart retains pending reviews; interrupted running operations become INTERRUPTED and are never replayed. Git operations are serialized within the application. Git itself locks refs/index during writes; users should avoid switching branches concurrently with an approved operation.

Validation uses temporary repositories and a temporary local bare remote. No user repository is committed/pushed, and no live GitHub write is performed by the implementation checks.

## Implementation verification (2026-10-08)

- Git-specific backend: 21 passing tests, including actual local commits/pushes, isolated-index preservation, new/deleted/literal-pathspec files, stale approvals, conversation binding, duplicate/concurrent approvals, interrupted operations, post-commit failures, coding-task handoff and dynamically revoked network access.
- Related coding planning/finalization/recovery/real-runtime regressions passed (5 existing skips in the broader selection).
- Frontend Git review, ChatComposer and chat-workspace-command selection: 31 passing tests. TypeScript and the frontend production build passed, with the existing large-chunk warning.
- The actual host app imports and exposes all 7 Git routes. No real-GUI acceptance run or authenticated live GitHub push is claimed.
