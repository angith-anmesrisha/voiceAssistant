"""Requests JARVIS handles itself, without asking a model.

Choosing the AI ("use Gemini from now on", "switch to the local model", "go back to
automatic") and testing the AIs ("test the AI providers", "run diagnostics"). Matching is
strict (the whole request must be the command), so "use Google to search for flights" is
still an ordinary request. Pairing a phone ("pair my phone") is handled here too, so
the pairing code is never sent to an AI or kept in the history. So are WhatsApp messages
("send hi to Mom on WhatsApp"): they go straight to the send_whatsapp tool, which needs
no AI at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AUTO = "auto"

_NAMES = {
    "groq": "groq",
    "grok": "groq",  # how speech-to-text often spells it
    "gemini": "gemini",
    "google": "gemini",
    "google gemini": "gemini",
    "openrouter": "openrouter",
    "router": "openrouter",
    "kimi": "openrouter",
    "nemotron": "openrouter",
    "ollama": "ollama",
    "llama": "ollama",
    "local": "ollama",
    "offline": "ollama",
    "qwen": "ollama",
    "auto": AUTO,
    "automatic": AUTO,
    "default": AUTO,
    "the best available": AUTO,
}

_PROVIDER_COMMAND = re.compile(
    r"""^\s*
    (?:(?:hey\s+)?jarvis[\s,]+)?
    (?:please\s+|can\s+you\s+|could\s+you\s+)?
    (?:use|switch(?:\s+over)?\s+to|change\s+to|go\s+(?:back\s+)?to|move\s+to|start\s+using
       |set\s+(?:the\s+)?(?:ai|model|provider|brain)\s+to)
    \s+(?:the\s+)?
    (?P<name>google\s+gemini|the\s+best\s+available|open\s+router|[a-z]+)
    (?:\s+(?:ai|model|models|provider|mode|brain))?
    (?:\s+(?:from\s+now\s+on|for\s+now|instead|now|again|please|only))*
    \s*[.!]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


_DIAGNOSE_COMMAND = re.compile(
    r"""^\s*(?:(?:hey\s+)?jarvis[\s,]+)?(?:please\s+|can\s+you\s+|could\s+you\s+)?
    (?:run\s+(?:a\s+)?diagnostics?|diagnose(?:\s+yourself)?|self[\s-]?test
       |(?:test|check)\s+(?:the\s+|all\s+(?:the\s+)?|your\s+|my\s+)?
        (?:ai|ais|providers?|models?|connections?|apis?)(?:\s+(?:providers?|models?))?)
    \s*[.!?]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def diagnose_command(text: str) -> bool:
    """True for "test the AI providers", "run diagnostics" and the like."""
    return _DIAGNOSE_COMMAND.match(text) is not None


def provider_command(text: str) -> str | None:
    """The provider a request asks to switch to ("auto" for automatic), else None."""
    match = _PROVIDER_COMMAND.match(text)
    if match is None:
        return None
    return _NAMES.get(re.sub(r"open\s+router", "openrouter", match.group("name").lower().strip()))


_PAIR_COMMAND = re.compile(
    r"""^\s*(?:(?:hey\s+)?jarvis[\s,]+)?(?:please\s+|can\s+you\s+|could\s+you\s+)?
    (?:pair|connect|link|set\s+up)\s+(?:my|a|the)\s+(?:phone|mobile)
    (?:\s+(?:to|with)\s+(?:you|jarvis))?(?:\s+dashboard)?\s*[.!?]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def pair_command(text: str) -> bool:
    """True for "pair my phone" and the like (the code never goes to an AI)."""
    return _PAIR_COMMAND.match(text) is not None


# --- WhatsApp: "send hi to Mom on WhatsApp" goes straight to send_whatsapp ---

_LEAD = re.compile(
    r"^\s*(?:(?:hey\s+|ok(?:ay)?\s+)?jarvis[\s,]+)?(?:please\s+|can\s+you\s+|could\s+you\s+"
    r"|would\s+you\s+|i\s+want\s+you\s+to\s+|go\s+ahead\s+and\s+)*",
    re.IGNORECASE,
)
_ON_WA = r"(?:on|via|in|through|over|using|with)\s+whats\s?app"
_SAYING = r"(?:saying|that\s+says|which\s+says|telling\s+(?:him|her|them)|that|with|:|,)"
_WHATSAPP_FORMS = [
    # send hi to Mom on WhatsApp / send a message saying hi to Mom on WhatsApp
    re.compile(
        rf"^send\s+(?:an?\s+)?(?:whats\s?app\s+)?(?:message\s+|text\s+|msg\s+)?"
        rf"(?:saying\s+|that\s+says\s+)?(?P<msg>.+?)\s+to\s+(?P<contact>.+?)\s+{_ON_WA}$",
        re.IGNORECASE,
    ),
    # send a WhatsApp (message) to Mom saying hi / WhatsApp message to Mom: hi
    re.compile(
        rf"^(?:send\s+)?(?:an?\s+)?whats\s?app(?:\s+message|\s+text|\s+msg)?\s+to\s+"
        rf"(?P<contact>.+?)\s*{_SAYING}\s*(?P<msg>.+)$",
        re.IGNORECASE,
    ),
    # message Mom on WhatsApp saying hi / text Mom on WhatsApp: hi
    re.compile(
        rf"^(?:send|text|message|msg|whats\s?app|ping)\s+(?P<contact>.+?)\s+{_ON_WA}\s*"
        rf"{_SAYING}?\s*(?P<msg>.+)$",
        re.IGNORECASE,
    ),
    # send Mom a WhatsApp message saying hi / WhatsApp Mom saying hi
    re.compile(
        rf"^(?:send|text|message|msg|whats\s?app|ping)\s+(?P<contact>.+?)\s+"
        rf"(?:an?\s+)?(?:whats\s?app\s+)?(?:message\s+|text\s+|msg\s+)?(?:{_ON_WA}\s+)?"
        rf"(?:saying|that\s+says|that)\s+(?P<msg>.+?)(?:\s+{_ON_WA})?$",
        re.IGNORECASE,
    ),
    # tell Mom on WhatsApp that I'm late / tell Mom I'm late on WhatsApp
    re.compile(
        rf"^(?:tell|ask)\s+(?P<contact>.+?)\s+{_ON_WA}\s+(?:that\s+)?(?P<msg>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?:tell|ask)\s+(?P<contact>\S+)\s+(?:that\s+)?(?P<msg>.+?)\s+{_ON_WA}$",
        re.IGNORECASE,
    ),
]
# "send hi on WhatsApp": the message, but no contact yet.
_NO_CONTACT = re.compile(
    rf"^(?:send|text|message)\s+(?:an?\s+)?(?:message\s+)?(?:saying\s+)?(?P<msg>.+?)\s+{_ON_WA}$",
    re.IGNORECASE,
)
_CONTACT_LEAD = re.compile(
    r"^(?:an?\s+)?(?:whats\s?app\s+)?(?:message\s+|text\s+|msg\s+)?(?:to\s+)?(?:my\s+)?",
    re.IGNORECASE,
)
_REPLY_LEAD = re.compile(
    r"^(?:send\s+it\s+|it'?s\s+(?:for\s+)?|it\s+goes\s+|that'?s\s+for\s+)?(?:to\s+)?(?:my\s+)?",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^(?:no|nope|cancel(?:\s+(?:it|that))?|never\s*mind|forget\s+(?:it|that)|stop|don'?t)\W*$",
    re.IGNORECASE,
)
_OPEN_FIRST = re.compile(
    r"^(?:open|go\s+to|launch)\s+whats\s?app,?\s+(?:and\s+)?(?:then\s+)?", re.I
)
_MAX_CONTACT_WORDS = 5


@dataclass(frozen=True, slots=True)
class WhatsAppRequest:
    contact: str  # "" when the request didn't say who
    message: str


def _clean_message(text: str) -> str:
    text = text.strip().strip("\"'\u201c\u201d\u2018\u2019").strip()
    return text


def _clean_contact(text: str) -> str:
    text = _CONTACT_LEAD.sub("", text.strip()).strip(" ,.:;\"'“”")
    return text if 0 < len(text.split()) <= _MAX_CONTACT_WORDS else ""


def whatsapp_command(text: str) -> WhatsAppRequest | None:
    """A WhatsApp message request with the contact and the message, else None."""
    if not re.search(r"whats\s?app", text, re.IGNORECASE):
        return None
    body = _LEAD.sub("", text).strip().rstrip(".!? ")
    opened = _OPEN_FIRST.match(body)
    if opened is not None:  # "open WhatsApp and send hi to Mom"
        body = body[opened.end() :] + " on WhatsApp"
    for form in _WHATSAPP_FORMS:
        match = form.match(body)
        if match is None:
            continue
        contact = _clean_contact(match.group("contact"))
        message = _clean_message(match.group("msg"))
        if contact and message:
            return WhatsAppRequest(contact, message)
    match = _NO_CONTACT.match(body)
    if match is not None and " to " not in f" {match.group('msg')} ":
        message = _clean_message(match.group("msg"))
        if message:
            return WhatsAppRequest("", message)
    return None


def contact_reply(text: str) -> str | None:
    """The contact named in an answer to "Who should I send it to?"; "" = cancel; None =
    not an answer (a new request)."""
    body = _LEAD.sub("", text).strip().rstrip(".!? ")
    if _CANCEL.match(body):
        return ""
    contact = _REPLY_LEAD.sub("", body).strip(" ,.:;\"'")
    contact = re.sub(rf"\s+{_ON_WA}$", "", contact, flags=re.IGNORECASE)
    if not contact or len(contact.split()) > _MAX_CONTACT_WORDS - 1:
        return None
    return contact


# --- quick capture: tasks, ideas and notes saved without asking a model ---


@dataclass(frozen=True, slots=True)
class Capture:
    kind: str  # task | idea | note
    text: str
    project: str = ""
    due: str = ""  # as said ("friday 5 pm"), parsed by the task tool


_SEP = r"\s*(?:[:\-\u2013\u2014]|,)?\s*"
_CAPTURE_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "idea",
        re.compile(
            r"^(?:(?:save|add|capture|log|note|jot\s+down|write\s+down|keep)\s+"
            r"(?:this\s+|that\s+)?(?:down\s+)?(?:as\s+)?(?:a\s+|an\s+|my\s+)?)?"
            r"(?:new\s+|quick\s+)?(?P<kind>project\s+)?idea"
            r"(?:\s+for\s+(?P<project>[\w .'-]{1,30}?))?\s*[:\-\u2013\u2014]\s*(?P<text>.+)$",
            re.IGNORECASE,
        ),
    ),
    (
        "idea",
        re.compile(
            r"^(?:save|add|capture|log|keep)\s+(?:a\s+|an\s+)?(?:new\s+)?(?P<kind>project\s+)?"
            r"idea(?:\s+for\s+(?P<project>[\w.'-]{1,30}))?\s*,?\s+(?:that\s+|about\s+)?"
            r"(?P<text>.{3,})$",
            re.IGNORECASE,
        ),
    ),
    (
        "task",
        re.compile(
            r"^(?:add|save|capture|log|create|make)\s+(?:a\s+|an\s+)?(?:new\s+)?"
            r"(?:(?P<project>[\w.'-]{1,30})\s+)??(?:task|to-?do)"
            r"(?:\s+for\s+(?P<project2>[\w .'-]{1,30}?))?" + _SEP + r"(?:to\s+)?(?P<text>.{3,})$",
            re.IGNORECASE,
        ),
    ),
    ("task", re.compile(r"^(?:to-?do|task)\s*[:\-\u2013\u2014]\s*(?P<text>.+)$", re.IGNORECASE)),
    (
        "task",
        re.compile(
            r"^(?:add|put)\s+(?P<text>.+?)\s+(?:to|on)\s+my\s+(?:(?P<project>[\w .'-]{1,30}?)\s+)?"
            r"(?:to-?do\s+list|to-?dos|task\s+list|tasks|list\s+of\s+tasks)$",
            re.IGNORECASE,
        ),
    ),
    (
        "note",
        re.compile(
            r"^(?:quick\s+note|capture|inbox|note\s+to\s+self)\s*[:\-\u2013\u2014]\s*(?P<text>.+)$",
            re.IGNORECASE,
        ),
    ),
)
_DUE_TAIL = re.compile(
    r"[,;]?\s+(?:due|by|before|for|on)\s+(?P<due>(?:next\s+|this\s+)?"
    r"(?:today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"in\s+\d+\s+(?:days?|weeks?)|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[a-z]{3,9}|[a-z]{3,9}\s+\d{1,2})"
    r"(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\s*$",
    re.IGNORECASE,
)
_TRAILING_DAY = re.compile(
    r"\s+(?P<due>today|tonight|tomorrow(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\s*$",
    re.IGNORECASE,
)


_OTHER_APP = re.compile(
    r"^(?:in|on|inside|using)\s|\b(?:in|on|into|using)\s+(?:notion|todoist|trello|jira|asana|"
    r"outlook|teams|clickup|ticktick|google\s+tasks|microsoft\s+to\s?do|whatsapp|excel|"
    r"notepad|onenote|obsidian|slack|discord)\b",
    re.IGNORECASE,
)


def capture_command(text: str) -> Capture | None:
    """A task, idea or note to save as it is ("idea: a plant-watering bot", "add a task to
    call the bank tomorrow", "add milk to my shopping tasks"), else None."""
    body = _LEAD.sub("", text).strip()
    for kind, form in _CAPTURE_FORMS:
        match = form.match(body)
        if match is None:
            continue
        groups = match.groupdict()
        words = (groups.get("text") or "").strip().strip("\"'“”").rstrip(".!")
        project = (groups.get("project") or groups.get("project2") or "").strip()
        if project.lower() in ("a", "an", "the", "new", "my", "this", "that", "quick"):
            project = ""
        due = ""
        if kind == "task":
            tail = _DUE_TAIL.search(words) or _TRAILING_DAY.search(words)
            if tail is not None:
                due = tail.group("due")
                words = words[: tail.start()].rstrip(" ,;")
        if len(words) < 2 or _OTHER_APP.search(words):
            return None  # "create a task in Notion …" is for that app
        return Capture(kind, words[:400], project.title() if project.islower() else project, due)
    return None
