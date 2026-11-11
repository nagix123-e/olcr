# Agent icon image upload under desktop CSP

The agent editor decoded selected images using `URL.createObjectURL`. The desktop Content Security Policy allows `img-src 'self' data:` (and the map tile origin), so `blob:` images are rejected before `Image.decode()` can finish. This surfaced as “画像を読み込めませんでした。” for valid image files.

The editor now reads the selected file using FileReader's data URL path, which the existing desktop policy permits. File selection and drag-and-drop share the corrected loader. The existing input size/type checks, 256-pixel thumbnail conversion, preview and save format remain in place. No CSP expansion is needed.

Validation:

- All 11 agent editor tests pass, including picker/drop uploads, permitted data-URL loading without object URLs, proportional resizing, preview, save/card display, failure preservation and retry.
- An isolated headless Chromium run uses the exact CSP from `tauri.conf.json` and the actual loader extracted from `CustomAgents.tsx`. A generated 1024×512 PNG fails with the old blob loader and succeeds with the corrected loader, producing a valid 256×128 WebP preview.
- That actual converted image survives creation, update, DB reload and a bound agent-conversation snapshot in a disposable database.
- Frontend typecheck and production build pass. The debug macOS bundle is rebuilt with build ID `olcr-agent-icon-data-url-20261028`.

The user’s running OLCR instance is not terminated or relaunched; reopening the rebuilt app applies the update.
