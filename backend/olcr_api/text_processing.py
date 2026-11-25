"""Shared sentence-aware phase processing for bounded text operations."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Sequence, TypeVar


MAX_TEXT_PHASE_CHARS = 5_000
MAX_TEXT_PHASE_OUTPUT_TOKENS = 4_000
LONG_TEXT_CONFIRMATION_CHARS = 200_000
MAX_TEXT_PROCESSING_CHARS = 50_000_000

_SENTENCE_END = re.compile(r"(?:[。！？!?]+|(?<!\d)\.(?=\s|$))[」』”’\"')\]]*")


@dataclass(frozen=True)
class TextPhase:
    """A phase's content and the source whitespace that followed its boundary."""

    text: str
    separator_after: str = ""


T = TypeVar("T")


def _choose_cut(text: str, maximum: int) -> int:
    window = text[:maximum]
    minimum = max(1, maximum // 2)

    sentence_ends = [match.end() for match in _SENTENCE_END.finditer(window)
                     if match.end() >= minimum]
    if sentence_ends:
        return sentence_ends[-1]

    paragraph_ends = [match.end() for match in re.finditer(r"\r?\n(?:[ \t]*\r?\n)+", window)
                      if match.end() >= minimum]
    if paragraph_ends:
        return paragraph_ends[-1]

    whitespace_ends = [match.start() for match in re.finditer(r"\s+", window)
                       if match.start() >= minimum]
    if whitespace_ends:
        return whitespace_ends[-1]
    return maximum


def split_text_into_phases(text: str, max_chars: int = MAX_TEXT_PHASE_CHARS) -> list[TextPhase]:
    """Split text near ``max_chars``, preferring sentence and paragraph boundaries.

    Separators are kept outside phase content so provider wrappers that trim
    responses do not collapse the source's blank lines or sentence spacing.
    If a single unbroken sentence is longer than the phase size, split it at
    the hard limit to keep every provider request bounded.
    """
    if not isinstance(text, str):
        raise TypeError("Text to process must be a string")
    if len(text) > MAX_TEXT_PROCESSING_CHARS:
        raise ValueError("TEXT_PROCESSING_INPUT_TOO_LARGE")
    if max_chars < 1:
        raise ValueError("Phase size must be positive")
    if not text:
        return []

    phases: list[TextPhase] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = _choose_cut(remaining, max_chars)
        separator_end = cut
        while separator_end < len(remaining) and remaining[separator_end].isspace():
            separator_end += 1
        if cut <= 0:
            cut = max_chars
            separator_end = cut
        phases.append(TextPhase(remaining[:cut], remaining[cut:separator_end]))
        remaining = remaining[separator_end:]
    if remaining:
        phases.append(TextPhase(remaining))
    return phases


def process_text_phases(
    text: str,
    processor: Callable[[str], T],
    *,
    max_chars: int = MAX_TEXT_PHASE_CHARS,
    process_many: Callable[[Sequence[str]], Sequence[T]] | None = None,
    combine: Callable[[Sequence[T], Sequence[TextPhase]], T] | None = None,
) -> T | list[T]:
    """Process phases in order and combine results while retaining separators.

    ``process_many`` can keep an operation-specific runtime warm while it
    consumes the same ordered phases. It must return one result per phase and
    process them sequentially. Simple processors use the default sequential
    loop here.
    """
    phases = split_text_into_phases(text, max_chars=max_chars)
    if process_many is not None:
        outputs = list(process_many([phase.text for phase in phases]))
    else:
        outputs = [processor(phase.text) for phase in phases]
    if len(outputs) != len(phases):
        raise ValueError("A text processor must return one result per phase")
    if combine is not None:
        return combine(outputs, phases)
    if all(isinstance(output, str) for output in outputs):
        return "".join(str(output) + phase.separator_after
                       for output, phase in zip(outputs, phases))
    return outputs
