# Local model selection with stale fallback candidates

The model picker sent the current fallback sequence alongside a primary-model change. `BrainModels.save` required every fallback to be present and available in the latest catalog, including entries the user had not changed. A temporarily disconnected API/provider, retired catalog entry or unavailable Ollama server could therefore reject a valid local selection with `Choose distinct available models for the fallback sequence`.

Saving now preserves existing selections, effort values and fallback candidates when they become unavailable or disappear from the catalog. Availability checks apply to newly selected primary models and newly added fallback candidates. Fallback shape, size, uniqueness, new-model availability and changed-effort validation remain enforced. The ordered fallback sequence is preserved rather than silently removed or replaced.

The actual installed-model inventory was checked read-only with a disposable database: Ollama Ornith 1.5 9B, MLX Ornith 1.5 9B 8-bit and MLX Ternary Bonsai 2 27B all report available. The user's global configuration contains API fallback candidates, consistent with the failing validation shown in the screenshot; the user database was not changed.

Tests cover switching to each of the three local models while offline/missing provider selections and fallbacks remain saved, actual local-provider dispatch with mocked inference, per-conversation isolation, removing offline entries, rejection of new unavailable/unknown/duplicate candidates, and retention of unchanged effort settings. The API returns 200 for those valid local switches. The picker test verifies that selecting a local model preserves the existing fallback sequence.

The frontend build and signed debug bundle are updated with build ID `olcr-local-model-selection-20261028`. Reopening the updated app applies the change; a running user instance is not terminated.
