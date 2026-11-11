# Vision model selection

Vision keeps two independent purposes: general images (`general_image`) and UI understanding (`gui_grounding`). Each runs the model selected for that purpose. The former `vision_fallback` role, setting, credential handles and automatic low-confidence model switch have been retired. Old stored role choices are discarded during migration; downloaded model weights are retained as ordinary installed models.

Settings → Models → Models by purpose → Vision → Change opens an installed/connected model selector. Local Ollama image-capable models and connected provider models use the same selection flow. Existing API keys and ChatGPT OAuth credentials are reused. A custom compatible endpoint remains available.

Provider-declared input capabilities are respected. When an API's catalog omits them, the model remains a candidate with a compatibility-check hint; activating it requires the existing image probe to succeed. A failed probe preserves the previous selection. UI models additionally need to return the expected structured perception result.

ChatGPT OAuth uses the existing refreshed token and Responses transport with `input_text` and `input_image`, following [OpenAI's image input specification](https://developers.openai.com/api/docs/guides/images-vision?api-mode=responses).

Invalid perception formatting may be retried once with the same selected model. A low-confidence result keeps its uncertainty instead of invoking another model. OCR crop/full-image retries also keep the selected model.
