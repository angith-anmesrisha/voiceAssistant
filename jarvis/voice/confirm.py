"""Spoken confirmations: JARVIS asks out loud and you answer "yes" or "no".

Only Medium-risk steps are confirmed by voice; High risk still needs CONFIRM typed in
the window. The buttons stay up meanwhile, so a click works too. Silence, or anything
that isn't clearly a yes, counts as no.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

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
    first = description.strip().split("\n", 1)[0]  # the details stay on screen
    text = _ASIDE.sub("", first).strip().rstrip(".:?")
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


def is_echo(heard: str, said: str, threshold: float = 0.8) -> bool:
    """Whether a transcript is just JARVIS's own last words picked up by the microphone.

    Short phrases never count: "send it" after "Shall I send it?" is a real answer.
    """
    words, spoken = _words(heard).split(), _words(said).split()
    if len(words) < 4 or not spoken:
        return False
    matcher = SequenceMatcher(a=words, b=spoken, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(words) >= threshold


_STOP = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|hey|oh)\W+)?(?:jarvis\W+)?(?:please\W+)?"
    r"(?:stop|cancel|never\s*mind|forget\s+(?:it|that)|shut\s+up|be\s+quiet|quiet|"
    r"enough|that'?s\s+enough|hold\s+on|wait|pause|abort|halt|silence)"
    r"(?:\W+(?:it|that|this|now|please|talking|speaking|jarvis|everything|there))*\W*$",
    re.IGNORECASE,
)


def is_stop_command(text: str) -> bool:
    """ "Stop", "cancel that", "Jarvis, be quiet": the user wants JARVIS to stop."""
    return bool(_STOP.match(text))
