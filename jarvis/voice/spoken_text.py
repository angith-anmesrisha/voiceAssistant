"""Turning a written reply into something worth hearing.

Replies are written for the screen, so before they are spoken: Markdown, tables, emoji
and code go, links become the site's name, file paths become the file's name, and each
line or list item becomes its own sentence so the voice pauses between them.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_LINK = re.compile(r"!?\[([^\]\n]*)\]\(([^)\s]*)\)")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>()\"']+", re.IGNORECASE)
_WIN_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*([^\\\s]+)")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LINE_START = re.compile(r"^\s*(#{1,6}\s+|>\s*|[-*+•]\s+|\d{1,2}[.)]\s+)")
_EMPHASIS = re.compile(r"(\*\*|__|\*|~~)")
_ARROWS = re.compile(r"\s*(->|=>|→|⇒)\s*")
_SENTENCE_END = (".", "!", "?", ":", ";", "…")


def without_thinking(text: str) -> str:
    """Drop a model's <think>...</think> reasoning, which is never shown or spoken."""
    return _THINK.sub("", text).strip()


def site_name(url: str) -> str:
    """ "https://www.bbc.co.uk/news/x" -> "bbc.co.uk"."""
    host = urlsplit(url if "://" in url else "http://" + url).hostname or ""
    return host.removeprefix("www.") or "the link"


def _line(line: str) -> str:
    if _TABLE_RULE.match(line):
        return ""
    if line.strip().startswith("|") or line.count("|") >= 2:
        line = ", ".join(cell.strip() for cell in line.strip().strip("|").split("|"))
    line = _LINE_START.sub("", line)
    line = _EMPHASIS.sub("", line)
    line = re.sub(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])", "", line)  # _italic_, not snake_case
    line = line.strip()
    if line and not line.endswith(_SENTENCE_END):
        line += "."
    return line


def _symbols(text: str) -> str:
    """Drop emoji, ticks and other pictographs; they would be read out by name or as noise."""
    return "".join(
        " " if unicodedata.category(ch) in ("So", "Cs", "Co") or ch in "️‍" else ch for ch in text
    )


def speakable(text: str) -> str:
    """A reply as it should be spoken: plain sentences, no markup, links or code."""
    text = _THINK.sub(" ", text)
    text, fences = _FENCE.subn("\n", text)
    text = _LINK.sub(lambda m: m.group(1) or site_name(m.group(2)), text)
    text = _URL.sub(lambda m: site_name(m.group(0).rstrip(".,;:!?")), text)
    text = _WIN_PATH.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE.sub(lambda m: m.group(1) if len(m.group(1)) <= 40 else "that", text)
    text = _ARROWS.sub(" to ", text)
    text = re.sub(r"\s*°\s*C\b", " degrees", text)
    text = re.sub(r"\s*°\s*F\b", " degrees Fahrenheit", text).replace("°", " degrees")
    text = _symbols(text)
    lines = [_line(line) for line in text.splitlines()]
    spoken = " ".join(line for line in lines if line)
    spoken = re.sub(r"[#`<>{}\[\]|\\^=]+", " ", spoken)
    if fences:
        spoken += " I've put the code on screen."
    spoken = re.sub(r"\s+([.,!?;:])", r"\1", spoken)
    spoken = re.sub(r"([.!?])\.+", r"\1", spoken)
    return re.sub(r"\s+", " ", spoken).strip()
