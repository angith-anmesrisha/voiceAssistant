"""Spoken confirmations: JARVIS asks out loud and you answer "yes" or "no".

Only Medium-risk steps are confirmed by voice; High risk still needs CONFIRM typed in
the window. The buttons stay up meanwhile, so a click works too. Silence, or anything
that isn't clearly a yes, counts as no.
"""

from __future__ import annotations

import re

_YES = re.compile(
    r"\b(yes|yeah|yea|yep|yup|sure|ok|okay|go ahead|do it|go for it|please do|confirm|"
    r"confirmed|affirmative|correct|absolutely|of course|send it|haan|han|ha|ji)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\b(no|nope|nah|don'?t|do not|cancel|stop|never ?mind|wait|hold on|negative|"
    r"not now|abort|nahi|nahin|illa|venda)\b",
    re.IGNORECASE,
)
_ASIDE = re.compile(r"\s*\([^)]*\)")


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def spoken_question(description: str) -> str:
    """ "Type “hi” into “Search” (this sends it)" -> "Shall I type “hi” into “Search”?"."""
    text = _ASIDE.sub("", description).strip().rstrip(".")
    if not text:
        return "Shall I go ahead?"
    return "Shall I " + text[0].lower() + text[1:] + "?"


def parse_yes_no(heard: str, *, asked: str = "") -> bool | None:
    """True for a clear yes, False for a clear no, None when unclear.

    A transcript that is only the end of JARVIS's own question (the mic hearing the
    speakers) is not an answer.
    """
    words = _words(heard)
    if not words:
        return None
    if asked and words in _words(asked):
        return None
    no, yes = bool(_NO.search(words)), bool(_YES.search(words))
    if no:
        return False  # "no", "yes, no wait": any no wins
    return True if yes else None
