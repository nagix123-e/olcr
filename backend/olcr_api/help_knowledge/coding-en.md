# Coding and resuming

Specify a working folder and a concrete deliverable, such as “Implement this app” or “Fix this file”.
The coding orchestrator card shows the plan, phases, changes, execution evidence, and verification results.
Closing the card preserves task records. Send “Resume” in the same conversation to continue from saved state.
Give specific follow-up changes in that conversation. Complete any outstanding clarification or scope approval.
On failure, inspect the unfinished phase and reason. Unfinished file writes or verification do not count as completion.

You can explicitly enable “Coding subagents” in Settings. This setting is independent from Coding Orchestrator and defaults to OFF. Enabling it never starts workers automatically. Workers can be used only for a heavy Coding Task with implementation phases that can be split into independent write scopes, and only after a direct user instruction for that task or a separate task-specific approval. Small tasks, dependent phases, and overlapping write scopes continue with one implementer.

Workers start with the current Coding Task implementation model. In unified Brain mode they use that same primary model. If it fails, workers may use only the configured fallback sequence snapshotted when the task-specific approval was created; workers cannot choose or add models. Local models are not recommended because role adherence and synthesis quality, latency, and memory/compute load can suffer. Workers return change proposals limited to their assigned scopes. OLCR validates the current file state and scope, then applies proposals serially. The parent task continues its normal review and verification, and workers are not used for release or publishing work. The task progress card provides separate approval or decline controls. Pausing the task or turning the setting off stops its workers.
