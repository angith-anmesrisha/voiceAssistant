"""Which tools a request needs.

Every tool description is sent with every request, and Groq's free tier counts them
against 8K tokens a minute. So a request gets the "core" tools plus the groups its words
point at (and the groups the previous request used, for follow-ups like "now send it").
A wrong guess costs little: the model can still answer, open apps, press shortcuts and
look at the screen, and the next request brings the right group.
"""

from __future__ import annotations

import re

_GROUPS: dict[str, re.Pattern[str]] = {
    "system": re.compile(
        r"\b(battery|cpu|ram|memory usage|processor|status|performance|windows? open|"
        r"volume|louder|quieter|softer|mute|unmute|sound|brightness|brighter|dimmer|dim|"
        r"screen light|wallpaper|background|close|quit|exit|kill|shut (it|that) down|"
        r"dark mode|light mode|wi-?fi|wireless|internet)\b",
        re.IGNORECASE,
    ),
    "keys": re.compile(
        r"\b(press|key|keys|keyboard|shortcut|hotkey|ctrl|control|alt|shift|win|tab|"
        r"enter|escape|type|dictate|write (this|that|it) (in|into))\b",
        re.IGNORECASE,
    ),
    "memory": re.compile(r"\b(forget|memory|memories|remember)\b", re.IGNORECASE),
    "clipboard": re.compile(r"\b(clipboard|copied|copy|paste|pasted)\b", re.IGNORECASE),
    "power": re.compile(
        r"\b(lock|sleep|shut ?down|turn off the (pc|computer|laptop)|restart|reboot|"
        r"power off|cancel (the )?shutdown)\b",
        re.IGNORECASE,
    ),
    "files": re.compile(
        r"\b(files?|folders?|director(y|ies)|documents?|desktop|downloads?|pictures?|"
        r"photos?|music|videos?|drive|copy|move|rename|delete|remove|recycle|trash|"
        r"find|where is|open .+\.\w{2,4}|pdf|docx?|xlsx?|csv|txt|notes?|read|summari[sz]e|"
        r"save|create|make a|organi[sz]e|tidy|clean up|undo)\b",
        re.IGNORECASE,
    ),
    "ui": re.compile(
        r"\b(click|tap|button|select|choose|pick|scroll|menu|tab|field|form|fill|"
        r"send|message|text (him|her|them|my|to)|reply|chat|dm|whatsapp|telegram|"
        r"discord|slack|teams|outlook|gmail|email|mail|inside|in the app|in this app|"
        r"on this page|search (for )?.+ in|play .+ (on|in)|spotify|notepad|word|excel|"
        r"settings page)\b",
        re.IGNORECASE,
    ),
    "media": re.compile(
        r"\b(youtube|video|song|music|playlist|listen|watch|play|flights?|fly|plane|"
        r"airport|steam|games?|update)\b",
        re.IGNORECASE,
    ),
    "camera": re.compile(
        r"\b(webcam|camera|how do i look|look at me|see me|what am i (holding|wearing))\b",
        re.IGNORECASE,
    ),
    "reminders": re.compile(
        r"\b(remind|reminder|reminders|timer|alarm|wake me|in \d+ (sec|second|min|minute|"
        r"hour)s?|at \d{1,2}(:\d\d)?\s*(am|pm)?|tomorrow|tonight|later|schedule|"
        r"countdown|keep an eye|watch (the )?news|follow (the )?news|stop watching)\b",
        re.IGNORECASE,
    ),
}


def groups_for(text: str, previous: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """The tool groups (besides core) that ``text`` probably needs."""
    found = {name for name, pattern in _GROUPS.items() if pattern.search(text)}
    return found | set(previous)
