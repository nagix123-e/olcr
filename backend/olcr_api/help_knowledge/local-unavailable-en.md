# Unavailable local models

Check that the installed language model is discovered by its runtime in the model list. Ollama and MLX are different runtimes. Selecting a newly unavailable model is rejected while preserving the prior configuration; a failed selection does not mean the model was deleted. Inspect the model ID, runtime and error. Missing models are not silently downloaded as replacements.
