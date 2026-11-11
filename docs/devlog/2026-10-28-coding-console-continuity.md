# Coding console continuity and scoped changes

The console's close button previously called archive, which removed the task
from ordinary resume selection. Phase reports were already durable, but the
visible task became inaccessible through the user's expected chat control.
Automatic instructions also waited for the entire task to finish, and a user
revision consumed the same budget as automatic error recovery.

## Behavior

- Closing the console now persists display state without pausing, archiving or
  deleting the task. The existing explicit Archive operation remains separate.
  A short notice explains that “resume” restores the task.
- Schema 37 adds `console_hidden` and durable coding events. Closing/restoring
  records an atomic snapshot of the actual task status, plan revision, current
  phase, retries, recovery state, phase statuses and persisted report IDs.
  Reports and instruction records remain in their existing tables. The logs
  endpoint exposes the saved task, events, phase reports and instructions.
- Chat resume restores the same task ID and current checkpoint on normal and
  streaming transports, including legacy records archived by the old close
  button. Completed work is displayed without being rerun. Pending approval
  remains pending. Cross-conversation task IDs are rejected.
- Contextual/polite controls include `再開してください`, `コーディングを再開して`,
  `コーディングタスクに戻って` and `please resume`.
- During active work, automatic amendments enter the next safe boundary;
  explicit Queue retains after-completion semantics. During final reporting,
  an automatic amendment is retained for a scoped follow-up; explicit Steer
  can still request a change before completion.
- Automatic corrections to paused/blocked work become a scoped child task in
  the existing workspace. The larger parent task remains paused with its
  original plan and history. `前の作業を再開して` can return from the child to
  that parent. An explicit Steer choice still edits the original task's plan.
- Follow-up planning treats the current request as the scope. The previous
  goal is historical context, rather than a product backlog to replay.
  Casual acknowledgments and explanation-only messages do not become edits.
- Explicit instruction-driven revisions do not spend automatic recovery
  counters or get blocked by old automatic-failure signatures. Their planner
  invocation/schema repair remains bounded, and authorization/scope gates
  remain enforced. An unchanged all-completed plan cannot consume an
  instruction while pretending to apply it.
- A plan-revision race during receipt is rebased once. An instruction arriving
  as the task completes is redirected to a scoped follow-up instead of being
  lost to a stale-state rejection.

## Validation

- Backend: 609 cases, 603 passed and six existing skips, with temporary
  database/settings/workspace profiles.
- Frontend: 78 tests passed across ChatComposer, CodingTaskProgress and
  PendingInstructions; frontend build passed.
- Dedicated tests cover persistence across DB reopening, schema-36 migration,
  both chat transports, archived legacy records, completed-task restoration,
  pending authorization, cross-conversation protection, scoped paused-task
  changes, parent restoration, receipt races and user-revision budgets.
- Existing orchestrator, checkpoint, finalization, chat routing, source repair
  and real Vite/browser verification regressions passed.
- The original live database and running task were not used for tests. No
  external generation-provider calls or live OLCR GUI acceptance run were made.

The app bundle was rebuilt for the updated frontend. The running user instance
must be quit and reopened to load the new frontend and backend together.
