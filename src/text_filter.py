"""Turn a raw Markdown assistant reply into something pleasant to hear.

Per the plan's "smart filtering" choice:
  - strip Markdown formatting noise
  - replace code blocks with a short spoken placeholder (don't read code aloud)
  - cap very long replies and append a "see the terminal" note

Structure-aware prosody (runs while the markdown is still visible):
  - headings become their own sentence followed by a pause token that the
    engine renders as extra silence
  - list items are announced "First: ... Second: ..." (per list)
  - tables are skipped with "I shared a table."
"""
from __future__ import annotations

import re

import config

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_EMDASH_RE = re.compile(r"\s*[—–]\s*")   # em-dash and en-dash → ", "
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|\*|__|_|~~)")
_HRULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_HEADING_LINE_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,2}[.)])\s+(.*\S)\s*$")
_ORDINALS = [
    "First", "Second", "Third", "Fourth", "Fifth",
    "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
]


def _structure(text: str) -> str:
    """Structure-aware prosody, applied while the markdown is still visible.

    - headings -> their own sentence + a pause token (extra silence)
    - list items -> announced "First: ...", "Second: ..." (counter per list)
    - table blocks -> replaced by one "I shared a table."
    """
    out = []
    li = 0            # list-item counter, reset when a paragraph interrupts
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("|"):
            if not in_table:
                out.append("I shared a table.")
                in_table = True
            continue
        in_table = False

        m = _HEADING_LINE_RE.match(line)
        if m:
            li = 0
            heading = m.group(2).strip().rstrip(":")
            if heading and heading[-1] not in ".!?":
                heading += "."
            out.append(heading + config.PAUSE_TOKEN)
            continue

        m = _LIST_ITEM_RE.match(line)
        if m:
            label = _ORDINALS[li] if li < len(_ORDINALS) else "Next"
            li += 1
            item = m.group(1)
            if item and item[-1] not in ".!?:;":
                item += "."
            out.append(f"{label}: {item}")
            continue

        if stripped:
            li = 0  # a paragraph breaks the list; blank lines don't
        out.append(line)
    return "\n".join(out)


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Cut to <= limit chars, preferring a sentence boundary."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    # try to end on the last complete sentence within the window
    parts = _SENTENCE_SPLIT_RE.split(head)
    if len(parts) > 1:
        kept = " ".join(parts[:-1]).strip()
        if kept:
            return kept
    return head.rstrip()


def clean(text: str, max_chars: int = 10000) -> str:
    """Return speakable text, or '' if there is nothing worth speaking.

    max_chars <= 0 disables the length cap (speak the whole reply).
    """
    if not text:
        return ""

    # Count code blocks before removing them so we can mention them.
    code_blocks = len(_FENCE_RE.findall(text))
    text = _FENCE_RE.sub(" ", text)

    # Structure pass needs the markdown intact - run before the strippers.
    text = _structure(text)

    text = _EMDASH_RE.sub(", ", text)
    text = _IMAGE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)          # keep link label, drop URL
    text = _INLINE_CODE_RE.sub(r"\1", text)   # speak inline code as plain words
    text = _HRULE_RE.sub(" ", text)
    text = _HEADING_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _LIST_BULLET_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)

    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    text = text.strip()

    if not text and code_blocks:
        return _code_note(code_blocks)

    if not text:
        return ""

    truncated = max_chars > 0 and len(text) > max_chars
    if truncated:
        text = _truncate_at_sentence(text, max_chars)

    suffix_parts = []
    if code_blocks:
        suffix_parts.append(_code_note(code_blocks))
    if truncated:
        suffix_parts.append("See the terminal for the rest.")
    if suffix_parts:
        text = text.rstrip() + " " + " ".join(suffix_parts)

    return text.strip()


def _code_note(n: int) -> str:
    return "I shared a code block." if n == 1 else f"I shared {n} code blocks."
