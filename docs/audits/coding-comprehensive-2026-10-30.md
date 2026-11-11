# Coding audit — 2026-10-30

Scope: request intent, coding/chat separation, task planning and execution, structured outputs, patch preconditions, verification authoring/repair, Chromium acceptance checks, pause/cancellation/resume, saved task consoles, final reporting, model availability, fallback ordering and conversation isolation.

Results:
- 20 coding-related backend modules: 377 collected, 372 passed, 5 skipped. Skips are existing pre-Orchestrator Gemma/guard lifecycle expectations, not newly added exclusions.
- 7 routing/model/context backend modules: 173 passed.
- 9 frontend modules: 134 passed.
- After the additional ownership fix: conversation/API suites 32 passed again; TypeScript and whitespace checks passed.
- Backend runs used isolated DB/support paths. The Node component was referenced through a temporary owned-runtime layout and installed Chromium's executable path was injected into the browser resolver. Browser checks actually executed; application source/acceptance outputs were generated only in test workspaces.

Additional issue found in source inspection: submission completion unconditionally removed both origin and destination AbortController map entries. After a draft moved into a created conversation, the origin could be reused by another submission while the earlier request was still running. The older completion could then remove the newer submission's stop handle. Cleanup now checks controller identity before deleting either entry.

Previously applied reasoning safeguards remained under test: finite verification JSON budget, exact repeated-reasoning detection with stream closure, bounded socket inactivity, and explicit output-limit diagnostics. Thinking policy/MAX behavior was not weakened.

Limits: these results do not prove every generated application is correct. No complete long-running real-model meal-planner generation was performed in this audit, and the user's running app/task was not restarted or modified. Model outputs in orchestration tests are controlled fixtures; deterministic tool and browser verification paths run for real.
