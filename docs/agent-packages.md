# Agent ZIP files

The Download button in each agent card saves one `.olcr-agent.zip` file:

- `.olcr-agent.json`: version 2 agent definitions, original filenames, media types,
  SHA256 hashes and knowledge-file references.
- `knowledge/<agent>/<index>.<extension>`: complete original knowledge files,
  including PDF/PNG/JPEG/WebP and UTF-8 text/CSV/Markdown/JSON/YAML/XML/TSV.

Choose Create from file beside Create agent or at the top of the new-agent form
and select this ZIP. The entire archive is validated before creating any agents.
Imported definitions receive new IDs and delegate references are remapped together
in one database transaction. Existing agents and conversations are preserved.

Knowledge uploads preserve their original bytes. On macOS the bundled local
PDFKit/Vision helper extracts PDF text, OCRs scanned PDF pages and images, and
stores the text for reference. No download is required for this helper. Original
images are also available to the configured general-image model when answering
questions, so diagrams are not limited to their OCR text. Raw base64 data is never
inserted into text prompts. File knowledge is displayed as a filename and read-only
extracted text; direct notes remain editable.

Limits: 50 MB per ZIP and expanded archive, 32 linked definitions, 20 knowledge
items per agent, 10 MB per file, 20 MB of original files per agent, 100 PDF pages,
100,000 extracted characters per file. Invalid structure/version, missing files,
wrong media types, hash mismatches, symlinks, encrypted entries and traversal paths
are rejected. ZIP members are read in memory, never extracted onto the filesystem.

Model binaries, installed skill packages and account credentials are not included.
Their selections are preserved and existing execution policies apply on use.
The previous JSON-only API remains readable for existing definitions; the UI now
exports and imports ZIPs.

Verification: agent-package tests (11), agent behavior tests (3), UI tests (8) and
TypeScript checks passed. Original PDF/PNG/CSV bytes survived export/import. Real
native OCR recognized `OLCR Knowledge 42` from both a generated PNG and an
image-only PDF in a normal macOS execution context. The tool sandbox prevents
Vision OCR; the normal app is not subject to that sandbox. GUI appearance and the
native Save dialog were not exercised through CUA.
