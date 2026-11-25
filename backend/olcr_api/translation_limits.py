"""Limits specific to translation requests."""

from .text_processing import MAX_TEXT_PROCESSING_CHARS

# Older callers used this name as a total input cap. Keep it as the shared
# aggregate bound while model calls are now capped per phase instead.
MAX_TRANSLATION_CHARS = MAX_TEXT_PROCESSING_CHARS
MAX_TRANSLATION_PHASE_CHARS = 4_000
MAX_TRANSLATION_INLINE_CHARS = 20_000
