# Long context, YaRN and compression

Adjust lengths under Settings → Models → Local model context. Toggle YaRN and automatic compression under Chat capabilities. YaRN applies to supported MLX Ornith/Bonsai text inference; disable it for Ollama, whose API cannot change YaRN. At 80% estimated usage including response space, compression excerpts older context while preserving original history, current input and required instructions. These local controls do not apply to external APIs. Longer contexts can affect memory, latency and quality; tokens are not characters.
