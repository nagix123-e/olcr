# Personal preferences, goals and scheduled checks

通常の操作は[チャットからの指示](chat-personal-workflows.md)で行えます。タスク・目標・プロフィール・フォーム・ツールを起動するために設定画面へ移動する必要はありません。

The personal assistant service saves preferences, measurable goals and bounded scheduled jobs in the local SQLite database. Chat is the normal entry point; unread results are surfaced in chat. The retained diagnostics component can inspect the same APIs. Jobs run while the backend process is alive; this is **not** an OS daemon or a guarantee of execution while OLCR is closed. A job's time is a timestamp; the editor displays the device's time zone. Repeating jobs use elapsed seconds, so daily/weekly intervals do not adjust for daylight saving time.

Preferences enter normal chat only when **Use my preferences in chat** is enabled. They are explicitly entered by the user, never inferred. Background processing has a separate master switch and separate mail/web/model switches, all initially off. Saving a job through the data API alone does not enable its category or connector permissions. Chat can propose the minimum personal background-category grants beside the exact task and apply them only after approval. Confirm permission mode blocks scheduled network checks because a background worker cannot obtain a fresh request approval.

Built-in jobs:

- `reminder`: a local inbox reminder, once or at a fixed interval.
- `mail_review`: reads at most 10 messages through the existing Gmail or Apple Mail connector, checking read access before every request. With a model extraction adapter and the model switch enabled, extracts validated date proposals; otherwise recognizes explicit full dates near a deadline cue. Relative or ambiguous dates are not guessed by the fallback. Mail is untrusted data and cannot select tools or actions. Original mail bodies are not persisted in task results.
- `web_watch`: searches at most five results through the configured OLCR search provider; it does not open a user-supplied URL or crawl results. Identical results do not generate another inbox item.
- `model_review`: performs a bounded text review using the host model callback, producing a summary and at most 12 next steps. It is not arbitrary agent/code execution. Create linked jobs or update goal progress explicitly to track longer work.

Mail proposals stay in the inbox. **Review calendar draft** calls `AssistantActions.prepare_calendar` with a stable draft ID and an all-day date. The existing calendar review UI lets the user correct dates and explicitly confirm or cancel. A scheduled job never confirms a calendar write, sends mail, makes purchases, opens a URL, executes shell commands, or runs model-produced code.

## Host integration

```python
from .personal_task_api import register_personal_task_routes
personal_tasks = register_personal_task_routes(app, personal_host)
# Start once on backend startup; stop on shutdown.
personal_tasks.start()  # 15-second polling, one bounded job each tick
personal_tasks.stop()
# Include only when relevant in ordinary chat context:
preferences_text = personal_tasks.profile_context()  # empty unless opted in
```

`personal_host()` returns `db` (Database object), `settings`, `service_connectors`, `assistant_service`. The last three may be actual objects or zero-argument getters. `personal_owner` is an optional stable local-profile identifier (default `local-user`) selected by the trusted host, never by request parameters. The service must be registered once per user profile; the existing desktop session middleware protects `/api/personal/*` routes. No new credentials are stored.

Optional callbacks:

```python
personal_task_model_review(
    task=task_dict, profile=profile_dict, goals=goal_list,
    max_seconds=180, max_tokens=1800,
) -> {"summary": "...", "steps": ["..."]}

personal_task_extract_deadlines(
    messages=[{"id": "...", "subject": "...", "content": "..."}],
    profile=profile_dict, max_seconds=120, max_tokens=1200,
) -> {"proposals": [{"title": "...", "date": "YYYY-MM-DD", "message_id": "..."}]}
```

Callbacks must enforce generation deadlines, model selection, output limits and external-provider permission checks, and must not invoke tools. Results are validated independently by the worker. The extractor can reference only supplied message IDs. It runs only when the model category is enabled. Profile data passed to callbacks contains only language/time zone until personalization is enabled. Missing review callbacks produce an explicit `MODEL_REVIEW_UNAVAILABLE` run result; missing extraction callbacks use the conservative fallback.

The React export is `PersonalTasks({api, onBack?, conversationId?})`. The `api` prop is the existing authenticated API wrapper. Supply a conversation ID to enable calendar draft review. The component uses the existing `AssistantActionBlockView` for explicit calendar confirmation.

## Durability and stopping

Each run claims `(task ID, version, scheduled time)` in an immediate SQLite transaction before any I/O. Multiple workers cannot claim the same slot. A 10-minute lease detects abandoned runs. A restart records expired work as interrupted and advances its schedule instead of replaying it. This intentionally accepts a missed check after interruption to avoid duplicate work. No scheduled operation has external write side effects.

Pause/edit/stop increments the task revision; execution checks it before each mail fetch and before publishing results. Paused or stopped tasks cannot publish stale results. Stop requires editing the task to restart. An in-flight provider/model call may finish before cancellation is observed. Backend shutdown signals the worker and waits briefly; unfinished runs recover as interrupted later.

Inbox notifications are in-app records, not native OS notifications. Only changed results create a new item. The service retains the latest 200 inbox records and 500 completed/failed/interrupted/cancelled run records per user profile. Task and goal definitions persist. Goal progress is user-editable; successful reviews do not invent progress.

## Verification

`PYTHONPATH=backend .venv312/bin/python -m unittest backend.tests.test_personal_tasks -v`

`npm --prefix frontend test -- --run src/PersonalTasks.test.tsx`

Tests use temporary databases and mocks, with no real accounts, network requests, GUI access or calendar writes.
