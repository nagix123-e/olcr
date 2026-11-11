# 2026-10-17: Temporarily hide hands-free UI

## Change

- Hide the hands-free mode toggle in the chat utility controls.
- Hide the hands-free panel, including its user-facing setup/configuration controls.
- Preserve `HandsFreePanel`, frontend state and command handlers, API routes, and backend implementation. The visibility switch is `HANDS_FREE_UI_VISIBLE` in `frontend/src/ChatComposer.tsx`.

## Restore

When hands-free is brought back, restore the UI as well as any desired feature entry points: set `HANDS_FREE_UI_VISIBLE` to `true` in `frontend/src/ChatComposer.tsx` to show the chat toggle and the hands-free panel/settings again. Do not treat the existing backend as a complete restoration; the user-facing UI must also be re-enabled.
