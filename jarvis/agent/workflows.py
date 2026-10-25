"""Workflows: a trigger and a list of steps, built without code (owner, 2026-10-25).

"When I activate Study Mode: open my study folder, open Notion, show my pending study
tasks, start a 25-minute timer, show the study checklist." Each step is one action from
:data:`ACTIONS` with its settings; most actions are an existing tool, so every step has
that tool's risk level and asks first exactly as it would if you asked for it directly.
A command chain ("Prepare my presentation") is a workflow whose steps pass their results
on: ``{previous}`` and ``{step 2}`` in a step's text are replaced by earlier output.

Workflows are kept in ``workflows.json`` in the data folder (plain JSON you can read).
Nothing runs until you save it: a workflow JARVIS proposes from your words is shown to
you first and saved only after a yes.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

TRIGGERS = ("phrase", "time", "startup", "folder", "manual")
TRIGGER_TITLES = {
    "phrase": "When I say…",
    "time": "At a time of day",
    "startup": "When JARVIS starts",
    "folder": "When a file appears or changes in a folder",
    "manual": "Only when I run it",
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_STEPS = 20


@dataclass
class Trigger:
    kind: str = "phrase"
    phrases: list[str] = field(default_factory=list)  # phrase: "start study mode", ...
    at: str = ""  # time: "07:30"
    days: list[str] = field(default_factory=list)  # time: [] = every day
    folder: str = ""  # folder: a folder JARVIS may read
    pattern: str = "*"  # folder: "*.pdf"
    event: str = "new"  # folder: new | changed

    def describe(self) -> str:
        if self.kind == "phrase":
            said = " or ".join(f"“{p}”" for p in self.phrases[:3]) or "(no phrase yet)"
            return f"When you say {said}"
        if self.kind == "time":
            days = (
                "every day"
                if not self.days or len(self.days) == 7
                else ", ".join(d.title() for d in self.days)
            )
            return f"At {self.at or '(no time)'}, {days}"
        if self.kind == "startup":
            return "When JARVIS starts"
        if self.kind == "folder":
            what = "a new" if self.event == "new" else "a changed"
            return f"When {what} {self.pattern} file is in {self.folder or '(no folder)'}"
        return "When you run it"


@dataclass
class Step:
    action: str
    settings: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        spec = ACTIONS.get(self.action)
        if spec is None:
            return f"Unknown action “{self.action}”"
        try:
            return spec.describe(self.settings)
        except Exception:
            return spec.title


@dataclass
class Workflow:
    name: str
    trigger: Trigger = field(default_factory=Trigger)
    steps: list[Step] = field(default_factory=list)
    enabled: bool = True
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def describe(self) -> str:
        lines = [f"{self.name}: {self.trigger.describe()}"]
        lines += [f"{i}. {step.describe()}" for i, step in enumerate(self.steps, 1)]
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Workflow:
        trigger = data.get("trigger") or {}
        known = Trigger.__dataclass_fields__
        steps = [
            Step(str(s.get("action", "")), dict(s.get("settings") or {}))
            for s in data.get("steps") or []
            if isinstance(s, dict)
        ]
        return cls(
            name=str(data.get("name") or "Workflow")[:60],
            trigger=Trigger(**{k: v for k, v in trigger.items() if k in known}),
            steps=steps[:MAX_STEPS],
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description") or "")[:300],
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
        )


# --- the actions ---


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    kind: str = "text"  # text | long | number | folder | file | list | choice | bool
    required: bool = False
    default: Any = ""
    choices: tuple[str, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class Action:
    name: str
    title: str  # in the builder's Add menu
    settings: tuple[Setting, ...]
    describe: Callable[[dict[str, Any]], str]
    tool: str = ""  # the JARVIS tool it runs ("" = handled by the runner itself)
    args: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    group: str = "Do"


def _get(settings: dict[str, Any], key: str, default: Any = "") -> Any:
    value = settings.get(key, default)
    return default if value in (None, "") else value


def _minutes(settings: dict[str, Any]) -> float:
    try:
        return max(1.0, min(1440.0, float(_get(settings, "minutes", 25))))
    except (TypeError, ValueError):
        return 25.0


def _items(settings: dict[str, Any]) -> list[str]:
    raw = settings.get("items") or []
    if isinstance(raw, str):
        raw = [line for line in re.split(r"\n|;", raw)]
    return [str(item).strip() for item in raw if str(item).strip()][:30]


def _tool_args(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get("arguments") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


_ACTIONS: list[Action] = [
    Action(
        "open_folder",
        "Open a folder",
        (Setting("folder", "Folder", "folder", True),),
        lambda s: f"Open the folder {_get(s, 'folder', '?')}",
        "open_path",
        lambda s: {"path": _get(s, "folder")},
        "Open",
    ),
    Action(
        "open_file",
        "Open a file",
        (Setting("file", "File", "file", True),),
        lambda s: f"Open {Path(str(_get(s, 'file', '?'))).name}",
        "open_path",
        lambda s: {"path": _get(s, "file")},
        "Open",
    ),
    Action(
        "open_app",
        "Open an application",
        (Setting("app", "Application", "text", True, hint="e.g. Notion, Spotify, VS Code"),),
        lambda s: f"Open {_get(s, 'app', '?')}",
        "open_app",
        lambda s: {"name": _get(s, "app")},
        "Open",
    ),
    Action(
        "open_website",
        "Open a website",
        (Setting("url", "Web address", "text", True, hint="https://…"),),
        lambda s: f"Open {_get(s, 'url', '?')}",
        "open_url",
        lambda s: {"url": _get(s, "url")},
        "Open",
    ),
    Action(
        "show_tasks",
        "Show pending tasks",
        (
            Setting("project", "Project (optional)", hint="e.g. Study"),
            Setting("kind", "Show", "choice", default="task", choices=("task", "idea")),
        ),
        lambda s: (
            "Show pending "
            + ("ideas" if _get(s, "kind") == "idea" else "tasks")
            + (f" for {_get(s, 'project')}" if _get(s, "project") else "")
        ),
        "list_tasks",
        lambda s: {"project": _get(s, "project"), "kind": _get(s, "kind", "task")},
        "Show",
    ),
    Action(
        "add_task",
        "Add a task",
        (
            Setting("text", "Task", required=True),
            Setting("project", "Project (optional)"),
            Setting("due", "Due (optional)", hint="tomorrow, friday 5 pm, 2026-11-02"),
        ),
        lambda s: f"Add the task “{_get(s, 'text', '?')}”",
        "add_task",
        lambda s: {
            "text": _get(s, "text"),
            "project": _get(s, "project"),
            "due": _get(s, "due"),
        },
        "Do",
    ),
    Action(
        "start_timer",
        "Start a timer",
        (
            Setting("minutes", "Minutes", "number", True, 25),
            Setting("label", "Say when it ends", default="Time's up"),
        ),
        lambda s: f"Start a {_minutes(s):g}-minute timer",
        "set_reminder",
        lambda s: {"text": _get(s, "label", "Time's up"), "in_minutes": _minutes(s)},
        "Do",
    ),
    Action(
        "show_checklist",
        "Show a checklist",
        (
            Setting("title", "Title", default="Checklist"),
            Setting("items", "Items (one per line)", "list", True),
        ),
        lambda s: f"Show the checklist “{_get(s, 'title', 'Checklist')}” ({len(_items(s))} items)",
        group="Show",
    ),
    Action(
        "say",
        "Say something",
        (Setting("text", "What to say", "long", True),),
        lambda s: f"Say “{str(_get(s, 'text', ''))[:60]}”",
        group="Show",
    ),
    Action(
        "briefing",
        "Morning briefing (weather, news, calendar, tasks)",
        (),
        lambda s: "Give the briefing: weather, headlines, calendar, tasks",
        group="Show",
    ),
    Action(
        "note",
        "Save a note",
        (Setting("title", "Title", required=True), Setting("text", "Text", "long", True)),
        lambda s: f"Save the note “{_get(s, 'title', '?')}”",
        "save_note",
        lambda s: {"title": _get(s, "title"), "text": _get(s, "text")},
        "Do",
    ),
    Action(
        "find_files",
        "Find files",
        (
            Setting("name", "Name contains", required=True, hint="e.g. .pdf or report"),
            Setting("folder", "In folder", "folder", default="documents"),
        ),
        lambda s: f"Find files named “{_get(s, 'name', '?')}” in {_get(s, 'folder', 'documents')}",
        "find_items",
        lambda s: {"name": _get(s, "name"), "folder": _get(s, "folder", "documents")},
        "Files",
    ),
    Action(
        "read_document",
        "Read a document",
        (Setting("file", "File", "file", True, hint="or {previous} for a file found before"),),
        lambda s: f"Read {Path(str(_get(s, 'file', '?'))).name}",
        "read_document",
        lambda s: {"path": _get(s, "file")},
        "Files",
    ),
    Action(
        "save_text",
        "Save text to a file",
        (
            Setting("path", "Save as", "text", True, hint="documents/Outline.md"),
            Setting("text", "Text", "long", True, "{previous}"),
        ),
        lambda s: f"Save {Path(str(_get(s, 'path', '?'))).name}",
        "save_text",
        lambda s: {"path": _get(s, "path"), "text": _get(s, "text", "{previous}")},
        "Files",
    ),
    Action(
        "ask_jarvis",
        "Ask JARVIS (it can use its tools)",
        (Setting("request", "Request", "long", True, hint="Use {previous} for the last result"),),
        lambda s: str(_get(s, "request", "Ask JARVIS"))[:80],
        group="Think",
    ),
    Action(
        "make_presentation",
        "Make a PowerPoint deck",
        (
            Setting("topic", "Topic (or blank to use a file)"),
            Setting("slides", "Slides", "number", default=10),
            Setting("source_file", "From file (optional)", "file"),
        ),
        lambda s: (
            f"Make a {_get(s, 'slides', 10)}-slide deck"
            + (f" on {_get(s, 'topic')}" if _get(s, "topic") else "")
        ),
        "make_presentation",
        lambda s: {
            "topic": _get(s, "topic"),
            "slides": int(float(_get(s, "slides", 10))),
            "source_file": _get(s, "source_file"),
        },
        "Think",
    ),
    Action(
        "research",
        "Research a question (cited report)",
        (
            Setting("question", "Question", "long", True),
            Setting("depth", "Depth", "choice", default="quick", choices=("quick", "deep")),
        ),
        lambda s: f"Research “{str(_get(s, 'question', '?'))[:60]}”",
        "research",
        lambda s: {"question": _get(s, "question"), "depth": _get(s, "depth", "quick")},
        "Think",
    ),
    Action(
        "study_document",
        "Summarise or question a document",
        (
            Setting("file", "File", "file", True, hint="or 'upload' to pick one each time"),
            Setting(
                "task",
                "What to do",
                "choice",
                default="summary",
                choices=(
                    "summary",
                    "key_points",
                    "action_items",
                    "answer",
                    "explain",
                    "study_notes",
                ),
            ),
            Setting("question", "Question (for 'answer')"),
        ),
        lambda s: (
            f"{str(_get(s, 'task', 'summary')).replace('_', ' ').capitalize()} of "
            f"{Path(str(_get(s, 'file', 'a document'))).name}"
        ),
        "study_document",
        lambda s: {
            "file": _get(s, "file"),
            "task": _get(s, "task", "summary"),
            "question": _get(s, "question"),
        },
        "Think",
    ),
    Action(
        "write_document",
        "Write a document (Word)",
        (
            Setting("brief", "What to write", "long", True, hint="Use {previous} for notes"),
            Setting(
                "kind",
                "Kind",
                "choice",
                default="report",
                choices=("report", "letter", "email", "essay", "article", "proposal", "notes"),
            ),
            Setting(
                "length", "Length", "choice", default="medium", choices=("short", "medium", "long")
            ),
        ),
        lambda s: f"Write a {_get(s, 'kind', 'report')}",
        "write_document",
        lambda s: {
            "brief": _get(s, "brief"),
            "kind": _get(s, "kind", "report"),
            "length": _get(s, "length", "medium"),
        },
        "Think",
    ),
    Action(
        "check_email",
        "Check email (read-only)",
        (
            Setting("count", "How many", "number", default=10),
            Setting("unread_only", "Unread only", "bool", default=True),
        ),
        lambda s: "Check email",
        "check_email",
        lambda s: {
            "count": int(float(_get(s, "count", 10))),
            "unread_only": bool(s.get("unread_only", True)),
        },
        "Show",
    ),
    Action(
        "confirm",
        "Ask me before going on",
        (Setting("question", "Question", required=True, default="Shall I carry on?"),),
        lambda s: f"Ask: {_get(s, 'question', 'Shall I carry on?')}",
        group="Flow",
    ),
    Action(
        "wait",
        "Wait",
        (Setting("seconds", "Seconds", "number", True, 5),),
        lambda s: f"Wait {_get(s, 'seconds', 5)} seconds",
        group="Flow",
    ),
    Action(
        "tool",
        "Run any JARVIS tool (advanced)",
        (
            Setting("name", "Tool name", required=True, hint="e.g. set_volume, set_dark_mode"),
            Setting("arguments", "Arguments (JSON)", "long", default="{}"),
        ),
        lambda s: f"Run {_get(s, 'name', '?')}",
        group="Flow",
    ),
]
ACTIONS: dict[str, Action] = {action.name: action for action in _ACTIONS}


def tool_call(step: Step) -> tuple[str, dict[str, Any]] | None:
    """The JARVIS tool and arguments a step runs, or None for the runner's own actions."""
    spec = ACTIONS.get(step.action)
    if spec is None:
        return None
    if step.action == "tool":
        return str(_get(step.settings, "name")), _tool_args(step.settings)
    if not spec.tool or spec.args is None:
        return None
    args = {k: v for k, v in spec.args(step.settings).items() if v not in ("", None)}
    return spec.tool, args


def fill(value: Any, outputs: list[str]) -> Any:
    """``{previous}``, ``{step 2}``, ``{today}`` in a setting, replaced by earlier output."""
    if not isinstance(value, str):
        return value

    def swap(match: re.Match[str]) -> str:
        key = match.group(1).strip().lower()
        if key == "previous":
            return outputs[-1] if outputs else ""
        if key == "today":
            return datetime.now().strftime("%A %d %B %Y")
        if number := re.fullmatch(r"step\s*(\d+)", key):
            index = int(number.group(1)) - 1
            return outputs[index] if 0 <= index < len(outputs) else ""
        return match.group(0)

    return re.sub(r"\{([a-z ]+\d*)\}", swap, value)


def filled(step: Step, outputs: list[str]) -> Step:
    return Step(step.action, {k: fill(v, outputs) for k, v in step.settings.items()})


def checklist_text(settings: dict[str, Any]) -> str:
    title = str(_get(settings, "title", "Checklist"))
    return title + ":\n" + "\n".join(f"☐ {item}" for item in _items(settings))


# --- saving and finding ---


def _norm(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    text = re.sub(r"^\s*(hey\s+)?jarvis\b", " ", text)
    text = re.sub(r"\b(please|now|for me|kindly)\b", " ", text)
    return " ".join(text.split())


class WorkflowStore:
    """The saved workflows (``workflows.json``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: list[Workflow] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._items = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [
                Workflow.from_json(item)
                for item in data.get("workflows", [])
                if isinstance(item, dict)
            ]
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Workflows file unreadable ({}); starting empty", type(exc).__name__)
            self._items = []

    def save_all(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"version": 1, "workflows": [w.to_json() for w in self._items]}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def all(self) -> list[Workflow]:
        return list(self._items)

    def get(self, workflow_id: str) -> Workflow | None:
        return next((w for w in self._items if w.id == workflow_id), None)

    def put(self, workflow: Workflow) -> None:
        """Add or replace (by id) and save."""
        self._items = [w for w in self._items if w.id != workflow.id] + [workflow]
        self.save_all()

    def remove(self, workflow_id: str) -> bool:
        before = len(self._items)
        self._items = [w for w in self._items if w.id != workflow_id]
        if len(self._items) != before:
            self.save_all()
            return True
        return False

    def named(self, name: str) -> Workflow | None:
        wanted = _norm(name).removesuffix(" workflow").removesuffix(" routine").strip()
        exact = [w for w in self._items if _norm(w.name) == wanted]
        if exact:
            return exact[0]
        loose = [w for w in self._items if wanted and wanted in _norm(w.name)]
        return loose[0] if len(loose) == 1 else None

    def match(self, text: str) -> Workflow | None:
        """The enabled workflow ``text`` starts: one of its phrases said on its own, or
        "run/start the <name> workflow"."""
        said = _norm(text)
        if not said:
            return None
        for workflow in self._items:
            if not workflow.enabled or workflow.trigger.kind != "phrase":
                continue
            if said in {_norm(p) for p in workflow.trigger.phrases if p.strip()}:
                return workflow
        named = re.fullmatch(
            r"(?:run|start|activate|begin|launch|do|trigger)\s+(?:the\s+|my\s+)?(.+?)"
            r"(?:\s+(?:workflow|routine|command chain|chain))",
            said,
        )
        if named:
            found = self.named(named.group(1))
            if found is not None and found.enabled:
                return found
        return None

    def with_trigger(self, kind: str) -> list[Workflow]:
        return [w for w in self._items if w.enabled and w.trigger.kind == kind]


# --- workflows proposed from plain English ---

_REQUEST = re.compile(
    r"^\W*(?:(?:hey\s+)?jarvis\W+)?(?:"
    r"(?:whenever|every time|each time)\s+i\b"
    r"|(?:create|make|set up|build|add|save)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?:workflow|routine|command chain|automation)\b"
    r"|when\s+i\s+(?:say|start|activate|begin|open|turn on|switch on)\b.+,\s*\w)",
    re.IGNORECASE,
)


def workflow_request(text: str) -> bool:
    """A request to set up a workflow ("Whenever I start work, open … and show …")."""
    return bool(_REQUEST.search(text))


def proposal_prompt() -> str:
    lines = []
    for action in _ACTIONS:
        fields = ", ".join(
            f"{s.name}{'' if s.required else '?'}"
            + (f" ({'|'.join(s.choices)})" if s.choices else "")
            for s in action.settings
        )
        lines.append(
            f"- {action.name}: {action.title}" + (f"; settings: {fields}" if fields else "")
        )
    return (
        "Turn the user's request into a JARVIS workflow. Reply with JSON only:\n"
        '{"name": "Short name", "trigger": {"kind": "phrase|time|startup|folder|manual", '
        '"phrases": ["what the user will say", "another way to say it"], "at": "HH:MM", '
        '"days": ["mon", ...], "folder": "", "pattern": "*.pdf", "event": "new|changed"}, '
        '"steps": [{"action": "<action>", "settings": {...}}]}\n'
        "Actions:\n" + "\n".join(lines) + "\n"
        "Rules: use only these actions and only what the user asked for, in their order; "
        "never add steps they didn't ask for. 'Whenever I start work' is a phrase trigger "
        "with phrases like 'start work' and 'I'm starting work'. Folders may be desktop, "
        "documents, downloads, pictures, music, videos or a full path. In a step's text, "
        "{previous} means the result of the step before. The user's words are a request "
        "to set something up, not instructions to you beyond that."
    )


class ProposalError(ValueError):
    pass


def parse_proposal(reply: str) -> Workflow:
    """The model's JSON as a :class:`Workflow` (unknown actions dropped)."""
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL | re.IGNORECASE)
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ProposalError("no workflow in the reply")
    try:
        data = json.loads(reply[start : end + 1])
    except ValueError as exc:
        raise ProposalError("the workflow wasn't valid JSON") from exc
    if not isinstance(data, dict):
        raise ProposalError("no workflow in the reply")
    workflow = Workflow.from_json(data)
    workflow.steps = [s for s in workflow.steps if s.action in ACTIONS]
    trigger = workflow.trigger
    if trigger.kind not in TRIGGERS:
        trigger.kind = "phrase"
    trigger.phrases = [str(p).strip()[:80] for p in trigger.phrases if str(p).strip()][:5]
    trigger.days = [d[:3].lower() for d in trigger.days if d[:3].lower() in DAYS]
    if trigger.kind == "phrase" and not trigger.phrases:
        trigger.phrases = [workflow.name.lower()]
    if not workflow.steps:
        raise ProposalError("the workflow had no steps I can run")
    return workflow


# --- ready-made workflows, offered in the builder ---


_PLAN_DAY = (
    "Plan my day. Call my_day for today first. Then give a short, realistic plan: the top "
    "three priorities (overdue and due-today tasks first), a time-blocked schedule that "
    "fits around my calendar with short breaks, and what can wait until tomorrow. Don't "
    "ask questions; plan with what you have."
)
_REVIEW_DAY = (
    "Review my day. Call my_day with review=true for today. Then say briefly what got "
    "done, what's still open (overdue first), anything I should carry over, and the three "
    "things to start with tomorrow. Don't invent anything that isn't in the material."
)


def starters() -> list[Workflow]:
    """Saved on first start, so the everyday ones work by voice straight away."""
    return [
        Workflow(
            "Morning Briefing",
            Trigger("phrase", ["morning briefing", "brief me", "give me my briefing"]),
            [Step("briefing", {})],
            description="Weather, calendar, reminders, tasks due and headlines.",
        ),
        Workflow(
            "Daily Planner",
            Trigger("phrase", ["plan my day", "plan today", "help me plan my day"]),
            [Step("ask_jarvis", {"request": _PLAN_DAY})],
            description="Priorities and a time-blocked plan around your calendar.",
        ),
        Workflow(
            "End-of-Day Review",
            Trigger(
                "phrase",
                ["review my day", "end of day review", "close out my day", "wrap up my day"],
            ),
            [
                Step("ask_jarvis", {"request": _REVIEW_DAY}),
                Step("note", {"title": "Day review {today}", "text": "{previous}"}),
            ],
            description="What got done, what's open, and tomorrow's first three; saved as a note.",
        ),
    ]


def templates() -> list[Workflow]:
    return [
        *starters(),
        Workflow(
            "Study Mode",
            Trigger("phrase", ["activate study mode", "start study mode", "study mode"]),
            [
                Step("open_folder", {"folder": "documents/Study"}),
                Step("open_app", {"app": "Notion"}),
                Step("show_tasks", {"project": "Study"}),
                Step("start_timer", {"minutes": 25, "label": "Study session over. Take a break."}),
                Step(
                    "show_checklist",
                    {
                        "title": "Study checklist",
                        "items": [
                            "Phone on silent",
                            "Water nearby",
                            "One topic at a time",
                            "Notes open",
                        ],
                    },
                ),
            ],
            description="Opens your study folder and app, shows study tasks, starts a timer.",
        ),
        Workflow(
            "Email triage",
            Trigger("phrase", ["check my email", "triage my inbox", "go through my email"]),
            [
                Step(
                    "ask_jarvis",
                    {
                        "request": "Check my unread email (up to 15 messages) and read the "
                        "ones that look important. Summarise in a few lines, then list the "
                        "action items with any deadlines, and say which messages need a "
                        "reply. Don't draft replies unless I ask."
                    },
                )
            ],
            description="Email Assistant: summary, action items, what needs a reply.",
        ),
        Workflow(
            "Start work",
            Trigger("phrase", ["start work", "i'm starting work", "let's start work"]),
            [
                Step("open_folder", {"folder": "documents/Projects"}),
                Step("show_tasks", {}),
            ],
            description="Opens your project folder and shows pending tasks.",
        ),
        Workflow(
            "Development environment",
            Trigger("phrase", ["open my development environment", "open my dev environment"]),
            [
                Step("open_app", {"app": "Visual Studio Code"}),
                Step("open_folder", {"folder": "documents/Projects"}),
                Step("open_website", {"url": "https://github.com"}),
            ],
            description="App launcher: your editor, project folder and GitHub in one go.",
        ),
        Workflow(
            "Research brief",
            Trigger("phrase", ["research brief"]),
            [
                Step("research", {"question": "What changed this year in my field?"}),
                Step(
                    "ask_jarvis",
                    {
                        "request": "From this research, give me the five things that matter "
                        "most and one thing to do about each:\n{previous}"
                    },
                ),
                Step(
                    "note",
                    {"title": "Research brief {today}", "text": "{previous}"},
                ),
            ],
            description="Command chain: research → the five things that matter → a note.",
        ),
        Workflow(
            "Prepare my presentation",
            Trigger("phrase", ["prepare my presentation"]),
            [
                Step("find_files", {"name": ".pdf", "folder": "documents/Presentation sources"}),
                Step(
                    "ask_jarvis",
                    {
                        "request": "From the files found below, read the most relevant ones "
                        "and extract the key points, figures and quotes for a presentation."
                        "\n\nFound: {previous}"
                    },
                ),
                Step(
                    "ask_jarvis",
                    {
                        "request": "Write a presentation outline with draft content for each "
                        "slide (title, 3-4 points, speaker note) from these notes:\n{previous}"
                    },
                ),
                Step(
                    "save_text", {"path": "documents/Presentation outline.md", "text": "{previous}"}
                ),
                Step("confirm", {"question": "Shall I make the PowerPoint file from the outline?"}),
                Step(
                    "make_presentation",
                    {"slides": 12, "source_file": "documents/Presentation outline.md"},
                ),
            ],
            description="Command chain: sources → key points → outline → saved → deck (asks).",
        ),
    ]


# --- workflows that start by themselves ---


class WorkflowTriggers:
    """A background job: runs time-of-day workflows (once a day, up to 30 minutes late,
    e.g. when the PC was asleep) and, once, the "when JARVIS starts" ones."""

    LATE_MIN = 30

    def __init__(
        self,
        store: WorkflowStore,
        keep: Any,
        run: Callable[[Workflow, str], Any],
        *,
        startup_after_s: float = 10.0,
    ) -> None:
        self._store = store
        self._keep = keep
        self._run = run
        self._startup_after_s = startup_after_s
        self._born: datetime | None = None
        self._started = False

    async def tick(self, now: datetime) -> list[Any]:
        if self._born is None:
            self._born = now
        if not self._started and (now - self._born).total_seconds() >= self._startup_after_s:
            self._started = True
            for workflow in self._store.with_trigger("startup"):
                await self._fire(workflow, "JARVIS started")
        due = [w for w in self._store.with_trigger("time") if _due(w.trigger, now, self.LATE_MIN)]
        if due:
            ran = await self._ran()
            today = now.date().isoformat()
            for workflow in due:
                if ran.get(workflow.id) == today:
                    continue
                ran[workflow.id] = today
                await self._keep.set_state("workflows_ran", json.dumps(ran))
                await self._fire(workflow, f"it's {workflow.trigger.at}")
        return []

    async def _ran(self) -> dict[str, str]:
        try:
            data = json.loads(await self._keep.get_state("workflows_ran") or "{}")
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    async def _fire(self, workflow: Workflow, reason: str) -> None:
        logger.info("Workflow {} triggered", workflow.id)
        result = self._run(workflow, reason)
        if hasattr(result, "__await__"):
            await result


def parse_clock(text: str) -> tuple[int, int] | None:
    """ "07:30", "7.30", "7:30 pm", "19:05" → (hour, minute)."""
    match = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*", text.lower())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if match.group(3) == "pm" and hour < 12:
        hour += 12
    elif match.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _due(trigger: Trigger, now: datetime, late_min: int) -> bool:
    clock = parse_clock(trigger.at)
    if clock is None:
        return False
    if trigger.days and DAYS[now.weekday()] not in trigger.days:
        return False
    at = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    late = (now - at).total_seconds()
    return 0 <= late <= late_min * 60


def register_workflow_tools(
    registry: Any, store: WorkflowStore, run: Callable[[Workflow, str], Any]
) -> None:
    """``list_workflows`` and ``run_workflow`` (queued: it runs once this reply is done,
    with each step's own confirmations)."""
    from jarvis.tools.registry import Risk, ToolResult, ToolSpec

    async def list_workflows() -> ToolResult:
        found = store.all
        if not found:
            return ToolResult(True, "No workflows saved yet.")
        lines = [
            f"{w.name}{'' if w.enabled else ' (off)'}: {w.trigger.describe()}; "
            + "; ".join(s.describe() for s in w.steps[:6])
            for w in found[:30]
        ]
        return ToolResult(True, f"{len(found)} workflows.", {"workflows": lines})

    async def run_workflow(name: str) -> ToolResult:
        workflow = store.named(name)
        if workflow is None:
            return ToolResult(False, f"There's no workflow called “{name}”.")
        result = run(workflow, "you asked")
        if hasattr(result, "__await__"):
            await result
        return ToolResult(True, f"{workflow.name} will run as soon as this reply is done.")

    registry.register(
        ToolSpec(
            "list_workflows",
            "List the user's saved workflows (command chains) and what starts them.",
            {"type": "object", "properties": {}, "required": []},
            list_workflows,
            Risk.LOW,
            lambda a: "List workflows",
            group="workflows",
        )
    )
    registry.register(
        ToolSpec(
            "run_workflow",
            "Run one of the user's saved workflows by name.",
            {
                "type": "object",
                "properties": {"name": {"type": "string", "maxLength": 80}},
                "required": ["name"],
            },
            run_workflow,
            Risk.LOW,
            lambda a: f"Run the workflow {a.get('name', '')}",
            group="workflows",
        )
    )
