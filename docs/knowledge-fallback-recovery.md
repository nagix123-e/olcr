# General knowledge recovery (2026-10-17)

The installed `embeddinggemma:latest` package was inspected through local Ollama.
Its digest is `82f094e4c0e19bc4208c692996d4f1ecf14e82a8dee579ee1d2ca1175260058e`.
`/api/show` reported `gemma-embedding`, 307,581,696 parameters and BF16.
Two local embedding inputs each returned 768 dimensions. The deployment manifest
now pins this inspected package; the previous run baseline remains historical.
Digest validation still rejects different artifacts and normalizes the optional
`sha256:` prefix. A digest diagnostic is not itself proof that generation failed.

API routing keeps deterministic routes and the EmbeddingGemma/Laya candidate
selection. Embedding failures retain their category and use the existing degraded
candidate route. A provider failure continues to the configured search provider,
then Wikipedia. An initial Wikipedia failure also enters recovery, without
retrying the same Wikipedia source after search fails.

After sources are exhausted, the configured inference role (`planner_replanner`,
Brain's general inference selection) answers from general knowledge with a host
notice that external evidence was not verified. This final generation bypasses
embedding and retrieval, and uses the existing Brain model fallback sequence.
It does not authorize tool execution, fabricate source attribution, or confirm
current data. The original provider failure remains in the task trace. If every
configured inference model is unavailable, the task truthfully remains failed.

Regression coverage includes the Japanese Ohtani question with embedding digest
failure plus source outages through both chat endpoints, malformed/empty
Wikipedia responses, and bounded inference failure.
