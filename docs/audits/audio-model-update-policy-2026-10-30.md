# Audio model update policy — 2026-10-30

Current defaults remain LiquidAI/LFM2.5-Audio-1.5B (English) and LiquidAI/LFM2.5-Audio-1.5B-JP (Japanese). Do not replace their runtime, weights or user selections when a new model is downloaded.

Downloaded candidate: LiquidAI/LFM3-Audio-2B-MLX-bf16, revision e9a4a12b6fbd1924d4a5dccd0b3417a2bf41eff0, in models/laya/hub. Architecture: lfm3_audio. Installed mlx-vlm 0.6.3 has no lfm3 implementation; the official model card marks MLX support in progress. Download completion is not runtime readiness. The current mlx-audio LFM2AudioModel adapter must not load this architecture.

Proposed update mechanism (design, not implemented):

- Versioned model manifest keyed by purpose and language, with upstream repository, pinned revision, architecture, runtime version range, supported operations, artifact integrity data and resource requirements.
- Distinct states: discovered, downloaded, runtime compatible, verified, recommended. Never promote on model name or size alone.
- Explicit user choices are pinned. Recommended-default followers may receive a tested recommendation; activation occurs only after download and compatibility checks succeed.
- Keep independent adapters and caches for LFM2.5 and LFM3. Preserve LFM2.5 as a selectable supported model regardless of the recommendation.
- Verify transcription and synthesis separately: real English clips, accuracy, first-result latency, real-time factor, memory, cancellation, repeated runs and Japanese regression checks.
- Apply selection atomically between sessions; record previous selection and support rollback. Report failures clearly rather than claiming LFM3 was used when LFM2.5 ran.
- Show update candidates and let the user approve activation; do not overwrite personal configuration or remove old weights automatically.

Sources: https://huggingface.co/LiquidAI/LFM3-Audio-2B-MLX-bf16 and https://huggingface.co/LiquidAI/LFM3-Audio-2B
