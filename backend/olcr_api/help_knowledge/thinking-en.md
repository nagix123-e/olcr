# Thinking and logs

In Brain, use a local model’s Zero / Auto / MAX slider. Zero disables thinking, Auto considers the task role and request, and MAX always enables thinking with priority over internal off hints.
Expand Thinking log on an answer when the model returns a reasoning channel. Received model text and separately labelled OLCR status entries are displayed.
MAX also applies to planning, implementation, verification and structured JSON generation. MLX applies the JSON constraint after thinking ends, preserving received reasoning separately from executable JSON. Auto also recognizes short calculation checks, proofs and debugging requests. Model or connection failures and output limits can prevent completion.
Received Ollama and MLX thought output, and API reasoning summaries actually delivered by the provider, appear as they arrive. The same area shows model loading, connections, fallbacks, failure reasons, switches and elapsed waiting time. Scrolling up preserves your reading position.

API models with native thinking switches (Gemini 2.5 Flash / Flash-Lite and supported Claude manual-thinking models) also support Zero / Auto / MAX. OLCR sends native API controls; models with depth controls retain their existing effort levels. Unsupported models do not show thinking switches.
