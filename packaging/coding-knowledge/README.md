# OLCR Coding Knowledge release input

`sources.json` and `records.json` are reviewed generation inputs. They contain
short original syntheses and provenance metadata, not bulk upstream documents.
Only `APPROVED` sources may be used by `scripts/build_coding_knowledge.py`.

To create the immutable artifact, start a local Ollama service with the exact
`embeddinggemma:latest` model, then run:

```sh
PYTHONPATH=backend python3 scripts/build_coding_knowledge.py
```

The builder resolves and records Ollama's model digest, embeds the detailed
records once, validates the schema and integrity gates, and writes
`coding-knowledge-index.json` plus `source-provenance-inventory.json`. Runtime
code never rebuilds or mutates this artifact. A release build must provide the
completed index through `OLCR_CODING_KNOWLEDGE_INDEX`.
