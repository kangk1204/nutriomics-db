"""Sentence spans over untouched Unicode source text (offsets are code points)."""

import re

_ABBREVIATIONS = {
    "e.g",
    "i.e",
    "et al",
    "vs",
    "fig",
    "dr",
    "mr",
    "mrs",
    "prof",
    "no",
    "ref",
    "approx",
    "vol",
    "p",
}


def sentence_spans(text: str, sections: list[dict] | None = None) -> list[dict]:
    """Split conservatively without corrupting decimals, abbreviations or offsets.

    Explicit section boundaries also delimit sentences. Sources remain immutable;
    whitespace is trimmed by moving span boundaries, never by rewriting strings.
    """
    boundaries = {0, len(text)}
    for section in sections or []:
        start, end = section.get("start", 0), section.get("end", 0)
        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text):
            boundaries.update((start, end))
    for match in re.finditer(r"[.!?]+[\"'”’)]*(?=\s|$)|\n\s*\n", text):
        end = match.end()
        if text[match.start()] == ".":
            prefix = text[max(0, match.start() - 24) : match.start()].casefold()
            if any(re.search(r"(?:^|\W)" + re.escape(abbr) + r"$", prefix) for abbr in _ABBREVIATIONS):
                continue
            if re.search(r"(?:^|\s)[A-Z]$", text[: match.start()]):
                continue
        boundaries.add(end)
    result = []
    ordered = sorted(boundaries)
    for left, right in zip(ordered, ordered[1:]):
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left == right:
            continue
        # Choose the most specific containing section when a JATS parent overlaps.
        containing = [s for s in sections or [] if s.get("start", -1) <= left and s.get("end", -1) >= right]
        label = min(containing, key=lambda s: s["end"] - s["start"]).get("label", "") if containing else ""
        result.append({"text": text[left:right], "start": left, "end": right, "label": label})
    return result


def lexical_tokens(text: str):
    """Alphanumeric tokens retain original start/end; punctuation stays in source."""
    return [(m.group().casefold(), m.start(), m.end()) for m in re.finditer(r"[\w]+", text, re.UNICODE)]
