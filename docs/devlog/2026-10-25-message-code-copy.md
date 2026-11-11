# Copy individual code blocks in chat answers

Chat Markdown now renders fenced/indented code blocks with their own Copy button.
The button copies only that block's text, excluding prose, Markdown fences and
the toolbar. It preserves indentation, quotes and internal newlines, removing
only the final newline added by the Markdown renderer. Inline code stays inline.

Copy success and failure are visible. A selection-based fallback supports
WebViews where Clipboard API is absent or rejects writes, restoring focus and
selection after the temporary copy field is removed. A block's toolbar remains
outside its horizontal scroll area so long commands keep an accessible button.

Validation: 7 interaction tests passed; `npm run build` passed. A separate
headless Chromium fixture passed at 320px and 960px widths: keyboard activation,
exact text from two independent blocks, and a visible stationary toolbar after
scrolling a genuinely overflowing command. Browser clipboard writes were mocked
to avoid changing the user's system clipboard. No native OLCR GUI was operated.
