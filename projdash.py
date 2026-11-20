#!/usr/bin/env python3
"""projdash - a terminal dashboard for people juggling many small projects.

One hand-editable TSV file is the whole database. The dashboard answers three
questions: what am I working on, where is each thing, and what comes next.

Standard library only. Runs on Linux, macOS and Windows with Python >= 3.9.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import heapq
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

__version__ = "0.1.0"

log = logging.getLogger("projdash")

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

FIELDS = (
    "id", "name", "category", "status", "progress", "priority",
    "path", "url", "next_step", "updated", "notes", "hidden",
)
FIELD_INDEX = {name: i for i, name in enumerate(FIELDS)}
NFIELDS = len(FIELDS)

STATUSES = ("idea", "active", "paused", "live", "done", "blocked")
# Order used by `list`: what needs attention first.
STATUS_ORDER = {"blocked": 0, "active": 1, "live": 2, "paused": 3, "idea": 4}
# Order used by the markdown report.
REPORT_ORDER = ("blocked", "active", "live", "paused", "idea", "done")
PRIORITY_LABEL = {1: "high", 2: "medium", 3: "low"}
PRIORITY_WORDS = {"high": 1, "medium": 2, "low": 3}
RESEARCH_CATEGORY = "research"
HIDDEN_MARK = "x"

STALE_DAYS = 14
UNKNOWN_DAYS = 999          # rows with a missing/invalid date count as very stale
DESIGN_WIDTH = 86           # full width of a dashboard line
DESIGN_INNER = 80           # text width inside a dashboard box
MIN_INNER = 66
INDENT = "  "
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# `set` accepts these names for each column.
FIELD_ALIASES = {
    "name": "name",
    "category": "category", "cat": "category",
    "status": "status",
    "progress": "progress", "%": "progress",
    "priority": "priority", "prio": "priority",
    "path": "path",
    "url": "url",
    "next": "next_step", "next_step": "next_step", "next-step": "next_step",
    "updated": "updated",
    "notes": "notes", "note": "notes",
    "hidden": "hidden",
}

REGISTRY_HEADER = """\
# projdash registry - one project per line, fields separated by a single TAB.
# Edit it by hand or with `projdash set`. Lines starting with # and blank
# lines are ignored by projdash and preserved when it rewrites the file.
#
# Fields, in this order (12):
#   1  id         short unique handle, no spaces (you type it in commands)
#   2  name       human-readable name
#   3  category   free text; `research` gets its own dashboard section
#   4  status     idea | active | paused | live | done | blocked
#   5  progress   0-100
#   6  priority   1 = high, 2 = medium, 3 = low
#   7  path       project folder (~ is expanded); used by `scan`, `open`, `show`
#   8  url        where it is published, if anywhere
#   9  next_step  the very next thing to do
#  10  updated    YYYY-MM-DD, last time you worked on it
#  11  notes      free text
#  12  hidden     x = keep in the registry but leave out of the dashboard
#
# Empty fields are fine, but keep all the TABs.
#
# id\tname\tcategory\tstatus\tprogress\tpriority\tpath\turl\tnext_step\tupdated\tnotes\thidden
"""


class ProjdashError(Exception):
    """An error meant for the user: printed to stderr, non-zero exit."""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def parse_date(text: str) -> Optional[_dt.date]:
    """Parse YYYY-MM-DD, returning None when it is not a valid date."""
    try:
        return _dt.datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def days_since(updated: str, today: _dt.date) -> Optional[int]:
    """Whole days between `updated` and `today`; None if the date is unusable."""
    date = parse_date(updated)
    if date is None:
        return None
    return max(0, (today - date).days)


def _to_int(text: str, default: int) -> int:
    try:
        return int(text.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return default


def expand_path(path: str) -> str:
    return os.path.expanduser(path.strip())


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def bar(progress: int) -> str:
    """Ten-character progress bar: 40 -> '####......'."""
    filled = max(0, min(10, progress // 10))
    return "#" * filled + "." * (10 - filled)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def clean_text(field: str, value: str) -> str:
    """Reject characters that would break the TSV, then trim."""
    if any(ch in value for ch in "\t\n\r"):
        raise ProjdashError(
            "%s: TAB and newline characters are not allowed inside a field" % field)
    return value.strip()


def validate_field(field: str, value: str) -> str:
    """Return the normalised value for `field` or raise ProjdashError."""
    value = clean_text(field, value)
    if field == "id":
        if not value:
            raise ProjdashError("id: must not be empty")
        if any(ch.isspace() for ch in value) or value.startswith("#"):
            raise ProjdashError("id: must not contain spaces or start with '#'")
    elif field == "name":
        if not value:
            raise ProjdashError("name: must not be empty")
    elif field == "status":
        value = value.lower()
        if value not in STATUSES:
            raise ProjdashError(
                "status: '%s' is not valid (choose from: %s)" % (value, ", ".join(STATUSES)))
    elif field == "progress":
        try:
            number = int(value.rstrip("%"))
        except ValueError:
            number = -1
        if not 0 <= number <= 100:
            raise ProjdashError("progress: '%s' is not a whole number from 0 to 100" % value)
        value = str(number)
    elif field == "priority":
        number = PRIORITY_WORDS.get(value.lower())
        if number is None:
            try:
                number = int(value)
            except ValueError:
                number = 0
        if number not in PRIORITY_LABEL:
            raise ProjdashError(
                "priority: '%s' is not valid (1 = high, 2 = medium, 3 = low)" % value)
        value = str(number)
    elif field == "updated":
        date = parse_date(value)
        if date is None:
            raise ProjdashError("updated: '%s' is not a date in YYYY-MM-DD format" % value)
        value = date.isoformat()
    elif field == "hidden":
        lowered = value.lower()
        if lowered in ("x", "yes", "true", "1"):
            value = HIDDEN_MARK
        elif lowered in ("", "no", "false", "0"):
            value = ""
        else:
            raise ProjdashError("hidden: use 'x' to hide or an empty value to show")
    return value


# --------------------------------------------------------------------------
# Registry (the TSV file)
# --------------------------------------------------------------------------

class Project:
    """One data row. Untouched rows are written back byte-for-byte."""

    __slots__ = ("fields", "raw", "dirty")

    def __init__(self, fields: Iterable[str], raw: Optional[str] = None):
        self.fields = list(fields)
        if len(self.fields) < NFIELDS:
            self.fields += [""] * (NFIELDS - len(self.fields))
        self.raw = raw
        self.dirty = raw is None

    @classmethod
    def from_line(cls, line: str) -> "Project":
        return cls(line.split("\t"), raw=line)

    @classmethod
    def create(cls, values: Dict[str, str]) -> "Project":
        """Build a new row from already validated values."""
        return cls([values.get(name, "") for name in FIELDS])

    def get(self, field: str) -> str:
        return self.fields[FIELD_INDEX[field]]

    def set(self, field: str, value: str) -> None:
        """Store a value (validated by the caller) and mark the row modified."""
        clean_text(field, value)
        self.fields[FIELD_INDEX[field]] = value
        self.dirty = True

    def to_line(self) -> str:
        if not self.dirty and self.raw is not None:
            return self.raw
        return "\t".join(self.fields)

    # Typed, forgiving accessors: a hand-edited file must never crash a view.
    @property
    def id(self) -> str:
        return self.fields[0].strip()

    @property
    def name(self) -> str:
        return self.fields[1].strip()

    @property
    def category(self) -> str:
        return self.fields[2].strip()

    @property
    def status(self) -> str:
        return self.fields[3].strip()

    @property
    def progress(self) -> int:
        return _to_int(self.fields[4], 0)

    @property
    def priority(self) -> int:
        value = _to_int(self.fields[5], 3)
        return value if value in PRIORITY_LABEL else 3

    @property
    def path(self) -> str:
        return self.fields[6].strip()

    @property
    def url(self) -> str:
        return self.fields[7].strip()

    @property
    def next_step(self) -> str:
        return self.fields[8].strip()

    @property
    def updated(self) -> str:
        return self.fields[9].strip()

    @property
    def notes(self) -> str:
        return self.fields[10].strip()

    @property
    def hidden(self) -> bool:
        return self.fields[11].strip() == HIDDEN_MARK

    @property
    def is_open(self) -> bool:
        return self.status != "done"


Line = Union[str, Project]


class Registry:
    """The registry file: data rows plus the comments and blank lines around them."""

    def __init__(self, path: Optional[str], lines: List[Line],
                 newline: str = "\n", trailing_newline: bool = True):
        self.path = path
        self.lines = lines
        self.newline = newline
        self.trailing_newline = trailing_newline

    @classmethod
    def parse(cls, text: str, path: Optional[str] = None) -> "Registry":
        newline = "\r\n" if "\r\n" in text else "\n"
        normalised = text.replace("\r\n", "\n")
        trailing = normalised.endswith("\n") or normalised == ""
        raw_lines = normalised.split("\n") if normalised else []
        if raw_lines and normalised.endswith("\n"):
            raw_lines.pop()
        lines: List[Line] = []
        for raw in raw_lines:
            if raw.startswith("#") or not raw.strip():
                lines.append(raw)
            else:
                lines.append(Project.from_line(raw))
        return cls(path, lines, newline, trailing)

    @classmethod
    def load(cls, path: str) -> "Registry":
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                text = handle.read()
        except FileNotFoundError:
            raise ProjdashError(
                "registry not found: %s\n"
                "Create it with `projdash init`, or point to an existing one "
                "with --db PATH or the PROJDASH_DB environment variable." % path)
        except IsADirectoryError:
            raise ProjdashError("registry path is a directory, not a file: %s" % path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjdashError("cannot read registry %s: %s" % (path, exc))
        registry = cls.parse(text, path)
        log.debug("loaded %d rows from %s", len(registry.projects), path)
        return registry

    def dumps(self) -> str:
        if not self.lines:
            return ""
        body = self.newline.join(
            line.to_line() if isinstance(line, Project) else line for line in self.lines)
        return body + (self.newline if self.trailing_newline else "")

    def save(self) -> None:
        if self.path is None:
            raise ProjdashError("registry has no file path")
        atomic_write(self.path, self.dumps())
        log.debug("wrote %d rows to %s", len(self.projects), self.path)

    @property
    def projects(self) -> List[Project]:
        return [line for line in self.lines if isinstance(line, Project)]

    def append(self, project: Project) -> None:
        self.lines.append(project)
        self.trailing_newline = True

    def resolve(self, query: str) -> Project:
        """Find a project by exact id, or by a prefix that matches only one id."""
        query = (query or "").strip()
        if not query:
            raise ProjdashError("missing project id")
        projects = self.projects
        for project in projects:
            if project.id == query:
                return project
        matches = [p for p in projects if p.id.startswith(query)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ProjdashError(
                "'%s' is ambiguous, it matches: %s" % (query, ", ".join(p.id for p in matches)))
        raise ProjdashError("no project with id '%s'" % query)


def atomic_write(path: str, text: str) -> None:
    """Write `text` to `path` so that readers see the old file or the new one."""
    target = os.path.realpath(path)          # keep symlinked registries symlinked
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix="." + os.path.basename(target) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(target):
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_db_path(environ: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    if env.get("PROJDASH_DB"):
        return os.path.expanduser(env["PROJDASH_DB"])
    config = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config, "projdash", "projects.tsv")


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

class Style:
    """Wraps text in ANSI codes, or returns it untouched when disabled."""

    CODES = {
        "bold": "\033[1m", "dim": "\033[2m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    }
    RESET = "\033[0m"

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def __call__(self, text: str, *names: str) -> str:
        if not self.enabled or not names or not text:
            return text
        return "".join(self.CODES[name] for name in names) + text + self.RESET


STATUS_COLOUR = {
    "live": "green", "done": "dim", "active": "cyan",
    "paused": "yellow", "blocked": "red",
}


def colour_enabled(no_color_flag: bool, stream=None, environ=None) -> bool:
    env = os.environ if environ is None else environ
    stream = sys.stdout if stream is None else stream
    if no_color_flag or env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def inner_width(stream=None) -> int:
    """Text width inside the dashboard boxes.

    The layout is designed for 86 columns. It never grows beyond that; on a
    narrower terminal the widest column of each table shrinks, down to a floor.
    Output that is not a terminal (pipes, files) always gets the designed width.
    """
    stream = sys.stdout if stream is None else stream
    try:
        if not stream.isatty():
            return DESIGN_INNER
    except (AttributeError, ValueError):
        return DESIGN_INNER
    columns = shutil.get_terminal_size(fallback=(DESIGN_WIDTH, 24)).columns
    return max(MIN_INNER, min(DESIGN_INNER, columns - (DESIGN_WIDTH - DESIGN_INNER)))


# --------------------------------------------------------------------------
# Box drawing for the dashboard
# --------------------------------------------------------------------------

Segment = Tuple[str, Tuple[str, ...]]


def _segments(cell) -> List[Segment]:
    """Normalise a cell: 'text', ('text', 'style', ...) or a list of those."""
    if isinstance(cell, str):
        return [(cell, ())]
    if isinstance(cell, tuple):
        return [(cell[0], tuple(cell[1:]))]
    result: List[Segment] = []
    for part in cell:
        result.extend(_segments(part))
    return result


class Box:
    """An ASCII table whose `flex` column absorbs any width reduction."""

    def __init__(self, widths: Sequence[int], style: Style, flex: int = 0,
                 inner: int = DESIGN_INNER):
        self.widths = list(widths)
        self.style = style
        natural = sum(self.widths) + 3 * (len(self.widths) - 1)
        self.widths[flex] = max(4, self.widths[flex] - (natural - inner))
        self.inner = sum(self.widths) + 3 * (len(self.widths) - 1)

    def rule(self) -> str:
        return INDENT + "+" + "+".join("-" * (w + 2) for w in self.widths) + "+"

    def top(self, title: str) -> str:
        plain = self.rule()[len(INDENT):]
        head = "+-- " + title + " "
        return INDENT + (head + plain[len(head):])[:len(plain)]

    def _cell(self, cell, width: int) -> str:
        out, used = [], 0
        for text, styles in _segments(cell):
            text = text[:width - used]
            used += len(text)
            out.append(self.style(text, *styles))
        return "".join(out) + " " * (width - used)

    def row(self, cells: Sequence) -> str:
        rendered = [self._cell(cell, width) for cell, width in zip(cells, self.widths)]
        return INDENT + "| " + " | ".join(rendered) + " |"

    def wide(self, cell) -> str:
        """A row that spans every column."""
        return INDENT + "| " + self._cell(cell, self.inner) + " |"


# --------------------------------------------------------------------------
# Selection and sorting rules
# --------------------------------------------------------------------------

def sort_for_list(projects: Iterable[Project]) -> List[Project]:
    """Priority first, then status (blocked, active, live, paused, idea, rest),
    then the most advanced first. Ties keep the registry order."""
    return sorted(projects, key=lambda p: (
        p.priority, STATUS_ORDER.get(p.status, 5), -p.progress))


def filter_projects(projects: Iterable[Project], needle: str) -> List[Project]:
    """Case-insensitive substring match over the whole row."""
    needle = needle.lower()
    return [p for p in projects if needle in "\t".join(p.fields).lower()]


def focus_items(projects: Iterable[Project], limit: int = 4) -> List[Project]:
    chosen = [p for p in projects if not p.hidden and p.is_open and p.priority == 1]
    return sorted(chosen, key=lambda p: -p.progress)[:limit]


def in_progress_items(projects: Iterable[Project]) -> List[Project]:
    chosen = [p for p in projects if not p.hidden and p.status in ("active", "blocked")]
    return sorted(chosen, key=lambda p: (p.priority, -p.progress))


def next_items(projects: Iterable[Project], limit: int = 8) -> List[Project]:
    chosen = [p for p in projects if p.is_open]
    return sorted(chosen, key=lambda p: (p.priority, -p.progress))[:limit]


def stale_items(projects: Iterable[Project], today: _dt.date, days: int = STALE_DAYS,
                include_hidden: bool = True) -> List[Tuple[Project, Optional[int]]]:
    """Open projects not updated for more than `days` days, stalest first.

    A missing or unreadable date counts as stale (and sorts first)."""
    result = []
    for project in projects:
        if not project.is_open or (project.hidden and not include_hidden):
            continue
        age = days_since(project.updated, today)
        if (UNKNOWN_DAYS if age is None else age) > days:
            result.append((project, age))
    result.sort(key=lambda item: -(UNKNOWN_DAYS * 1000 if item[1] is None else item[1]))
    return result


def live_items(projects: Iterable[Project]) -> List[Project]:
    return [p for p in projects if not p.hidden and p.status == "live" and p.url]


def research_items(projects: Iterable[Project], include_hidden: bool) -> List[Project]:
    chosen = [p for p in projects
              if p.category == RESEARCH_CATEGORY and (include_hidden or not p.hidden)]
    return sorted(chosen, key=lambda p: p.updated, reverse=True)


def age_text(age: Optional[int]) -> str:
    return "?" if age is None else "%dd" % age


# --------------------------------------------------------------------------
# Renderers (pure: they return lines, they never print or touch the disk)
# --------------------------------------------------------------------------

def render_dashboard(projects: Sequence[Project], today: _dt.date, style: Style,
                     inner: int = DESIGN_INNER, clock: str = "") -> List[str]:
    visible = [p for p in projects if not p.hidden]
    lines: List[str] = [""]

    # ---- header with counts ----
    counts: Dict[str, int] = {}
    for project in visible:
        counts[project.status] = counts.get(project.status, 0) + 1
    open_ones = [p for p in visible if p.is_open]
    average = sum(p.progress for p in open_ones) // len(open_ones) if open_ones else 0
    title = "DASHBOARD  " + today.strftime("%a %d %B %Y") + (" - " + clock if clock else "")
    box = Box([DESIGN_INNER], style, 0, inner)
    lines.append(box.top(title))
    lines.append(box.wide(""))
    lines.append(box.wide((
        "%d active     %d live     %d paused     %d blocked     %d done     (%d projects)" % (
            counts.get("active", 0), counts.get("live", 0), counts.get("paused", 0),
            counts.get("blocked", 0), counts.get("done", 0), len(visible)), "bold")))
    lines.append(box.wide((
        "average progress of open work    [%s] %d%%" % (bar(average), average), "dim")))
    lines.append(box.wide(""))
    lines.append(box.rule())

    # ---- today's focus ----
    lines.append("")
    box = Box([2, 26, 4, 39], style, 3, inner)
    lines.append(box.top("TODAY'S FOCUS (high priority)"))
    lines.append(box.row([("#", "dim"), ("PROJECT", "dim"), ("PROG", "dim"),
                          ("NEXT STEP", "dim")]))
    lines.append(box.rule())
    focus = focus_items(projects)
    for number, project in enumerate(focus, 1):
        lines.append(box.row([
            (str(number), "bold"), (project.name, "bold"), "%3d%%" % project.progress,
            (project.next_step or "(to be defined)", "green")]))
    if not focus:
        lines.append(box.wide("no open high-priority project"))
    lines.append(box.rule())

    # ---- in progress ----
    lines.append("")
    box = Box([24, 6, 17, 5, 16], style, 0, inner)
    lines.append(box.top("IN PROGRESS"))
    lines.append(box.row([("PROJECT", "dim"), ("PRIO", "dim"), ("PROGRESS", "dim"),
                          ("UPD.", "dim"), ("ID", "dim")]))
    lines.append(box.rule())
    current = in_progress_items(projects)
    for project in current:
        age = days_since(project.updated, today)
        is_stale = (UNKNOWN_DAYS if age is None else age) > STALE_DAYS
        lines.append(box.row([
            (project.name, "red" if project.status == "blocked" else "cyan"),
            (PRIORITY_LABEL[project.priority], "red" if project.priority == 1 else "dim"),
            [("[%s]" % bar(project.progress), "dim"), " ",
             ("%3d%%" % project.progress, "bold")],
            (age_text(age), "yellow" if is_stale else "dim"),
            (project.id, "dim")]))
    if not current:
        lines.append(box.wide("nothing in progress"))
    lines.append(box.rule())

    # ---- stale ----
    lines.append("")
    box = Box([39, 8, 8, 16], style, 0, inner)
    lines.append(box.top("STALE (untouched for more than %d days)" % STALE_DAYS))
    lines.append(box.row([("PROJECT", "dim"), ("STATUS", "dim"), ("IDLE FOR", "dim"),
                          ("ID", "dim")]))
    lines.append(box.rule())
    stale = stale_items(projects, today, STALE_DAYS, include_hidden=False)[:6]
    for project, age in stale:
        lines.append(box.row([
            (project.name, "yellow"), project.status,
            (age_text(age), "bold"), (project.id, "dim")]))
    if not stale:
        lines.append(box.wide("nothing stale"))
    lines.append(box.rule())

    # ---- live ----
    lines.append("")
    box = Box([26, 51], style, 1, inner)
    lines.append(box.top("LIVE"))
    lines.append(box.row([("PROJECT", "dim"), ("URL", "dim")]))
    lines.append(box.rule())
    live = live_items(projects)
    for project in live:
        lines.append(box.row([(project.name, "green"), (project.url, "dim")]))
    if not live:
        lines.append(box.wide("nothing live with a URL"))
    lines.append(box.rule())

    # ---- research ----
    lines.append("")
    box = Box([24, 53], style, 1, inner)
    lines.append(box.top("RESEARCH (do not forget)"))
    lines.append(box.row([("RESEARCH", "dim"), ("WHAT IT SAYS / WHAT TO DO WITH IT", "dim")]))
    lines.append(box.rule())
    research = research_items(projects, include_hidden=False)
    for project in research:
        lines.append(box.row([(project.name, "bold"), project.notes]))
        if project.next_step:
            lines.append(box.row(["", ("-> " + project.next_step, "green")]))
        if project.path:
            lines.append(box.row(["", (project.path, "dim")]))
        lines.append(box.rule())
    if not research:
        lines.append(box.wide("no research recorded"))
        lines.append(box.rule())

    lines.append("")
    lines.append(INDENT + style(
        "projdash list | show <id> | research | touch <id> (worked on it today) | help",
        "dim"))
    lines.append("")
    return lines


def render_list(projects: Sequence[Project], today: _dt.date, style: Style) -> List[str]:
    count = lambda status: sum(1 for p in projects if p.status == status)  # noqa: E731
    lines = [
        "",
        INDENT + style("PROJECTS", "bold", "blue") + "  " + style(today.isoformat(), "dim"),
        INDENT + style("%d projects - %d active - %d live - %d done" % (
            len(projects), count("active"), count("live"), count("done")), "dim"),
        "",
    ]
    for project in sort_for_list(projects):
        if project.priority == 1:
            mark = style("!!", "red")
        elif project.priority == 2:
            mark = style(" !", "yellow")
        else:
            mark = "  "
        lines.append("%s %s %s %s %3d%%  %s %s" % (
            mark,
            style("%-16s" % project.id, "bold"),
            style("%-9s" % project.status, STATUS_COLOUR.get(project.status, "magenta")),
            style(bar(project.progress), "dim"),
            project.progress,
            "%-26.26s" % project.name,
            style(project.category, "dim")))
        if project.hidden:
            lines.append("     " + style("[hidden from dashboard]", "dim"))
        if project.next_step:
            lines.append("     " + style("->", "green") + " " + project.next_step)
    lines.append("")
    lines.append(INDENT + style(
        "projdash next | projdash show <id> | projdash set <id> <field> <value> | projdash help",
        "dim"))
    lines.append("")
    return lines


def render_next(projects: Sequence[Project], style: Style) -> List[str]:
    lines = ["", INDENT + style("WHAT TO DO NOW", "bold", "green") + " "
             + style("(open projects, highest priority first)", "dim"), ""]
    items = next_items(projects)
    for number, project in enumerate(items, 1):
        lines.append("  %s %s %s" % (
            style("%d." % number, "bold"), style(project.name, "bold"),
            style("(%s %d%%)" % (project.status, project.progress), "dim")))
        lines.append("     " + style(project.next_step or "[no next step defined]", "green"))
        lines.append("     " + style("id: " + project.id, "dim"))
        lines.append("")
    if not items:
        lines += [INDENT + "nothing open", ""]
    return lines


def render_show(project: Project, today: _dt.date, style: Style) -> List[str]:
    age = days_since(project.updated, today)
    lines = [
        "",
        style(project.name, "bold", "blue") + " " + style("(%s)" % project.id, "dim"),
        "  category   : " + project.category,
        "  status     : %s   progress: %d%%   priority: %d (%s)" % (
            project.status, project.progress, project.priority,
            PRIORITY_LABEL[project.priority]),
        "  updated    : %s   %s" % (project.updated or "-", style(
            "(date unknown)" if age is None else "(%d days ago)" % age, "dim")),
    ]
    folder = expand_path(project.path) if project.path else ""
    if folder:
        exists = os.path.isdir(folder)
        lines.append("  path       : %s  %s" % (
            folder, style("[exists]", "green") if exists else style("[MISSING]", "red")))
    if project.url:
        lines.append("  url        : " + project.url)
    lines.append(INDENT + style("next step  : " + (project.next_step or "(to be defined)"),
                                "green"))
    if project.notes:
        lines.append("  notes      : " + project.notes)
    if project.hidden:
        lines.append(INDENT + style("hidden from the dashboard", "dim"))
    if folder and os.path.isdir(folder):
        recent = recent_files(folder)
        if recent:
            lines.append(INDENT + style("recently touched files:", "dim"))
            for mtime, relative in recent:
                lines.append("    %s %s" % (
                    _dt.date.fromtimestamp(mtime).isoformat(), relative))
    lines.append("")
    return lines


def render_stale(projects: Sequence[Project], today: _dt.date, days: int,
                 style: Style) -> List[str]:
    lines = ["", INDENT + style("UNTOUCHED FOR MORE THAN %d DAYS" % days, "bold", "yellow"), ""]
    items = stale_items(projects, today, days)
    for project, age in items:
        idle = "no date" if age is None else "%3d days" % age
        lines.append("  %s %s  %s %s" % (
            style("%-16s" % project.id, "bold"), style(idle, "red"),
            project.name, style("(%s)" % project.status, "dim")))
    if not items:
        lines.append(INDENT + "nothing stale")
    lines.append("")
    return lines


def render_research(projects: Sequence[Project], style: Style,
                    inner: int = DESIGN_INNER) -> List[str]:
    box = Box([76], style, 0, inner - 4)
    lines = ["", box.top("RESEARCH")]
    items = research_items(projects, include_hidden=True)
    for project in items:
        lines.append(box.wide(("%s   (%s)" % (project.name, project.updated or "-"), "bold")))
        if project.notes:
            lines.append(box.wide(project.notes))
        if project.next_step:
            lines.append(box.wide(("-> " + project.next_step, "green")))
        if project.path:
            lines.append(box.wide((project.path, "dim")))
        lines.append(box.wide(("projdash open " + project.id, "dim")))
        lines.append(box.rule())
    if not items:
        lines.append(box.wide("no research recorded"))
        lines.append(box.rule())
    lines.append("")
    return lines


def render_markdown(projects: Sequence[Project], today: _dt.date) -> str:
    out = ["# Project status - %s" % today.isoformat(), ""]
    extra = sorted({p.status for p in projects} - set(REPORT_ORDER))
    for status in list(REPORT_ORDER) + extra:
        group = sorted((p for p in projects if p.status == status), key=lambda p: p.priority)
        if not group:
            continue
        out += ["## %s" % (status or "(no status)"), ""]
        for project in group:
            out.append("- **%s** (%d%%, priority %d) - _%s_" % (
                project.name, project.progress, project.priority,
                project.next_step or "next step to be defined"))
            if project.url:
                out.append("  - %s" % project.url)
            if project.path:
                out.append("  - `%s`" % project.path)
            out.append("  - updated: %s" % (project.updated or "-"))
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# File system: scan, recent files, openers
# --------------------------------------------------------------------------

def _walk_files(folder: str):
    """Yield (mtime, path) for every file under `folder`, skipping SKIP_DIRS."""
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            full = os.path.join(root, name)
            try:
                yield os.lstat(full).st_mtime, full
            except OSError:
                continue


def newest_mtime(folder: str) -> Optional[float]:
    return max((mtime for mtime, _ in _walk_files(folder)), default=None)


def recent_files(folder: str, limit: int = 3) -> List[Tuple[float, str]]:
    newest = heapq.nlargest(limit, _walk_files(folder))
    return [(mtime, os.path.relpath(full, folder)) for mtime, full in newest]


def scan_registry(registry: Registry) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Move `updated` forward to the newest file modification in each folder.

    Returns (updated, missing): lists of (id, new date) and (id, folder).
    The registry is modified in memory; the caller saves it."""
    updated: List[Tuple[str, str]] = []
    missing: List[Tuple[str, str]] = []
    for project in registry.projects:
        if not project.path:
            continue
        folder = expand_path(project.path)
        if not os.path.isdir(folder):
            missing.append((project.id, folder))
            continue
        newest = newest_mtime(folder)
        if newest is None:
            continue
        found = _dt.date.fromtimestamp(newest)
        current = parse_date(project.updated)
        if current is None or found > current:
            project.set("updated", found.isoformat())
            updated.append((project.id, found.isoformat()))
    return updated, missing


def scan_marker_path(db_path: str) -> str:
    directory, name = os.path.split(os.path.realpath(db_path))
    return os.path.join(directory, "." + name + ".last-scan")


def auto_scan(registry: Registry, today: _dt.date) -> None:
    """Run `scan` at most once a day, silently. Never fails the dashboard."""
    if registry.path is None:
        return
    marker = scan_marker_path(registry.path)
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            if handle.read().strip() == today.isoformat():
                return
    except (OSError, UnicodeDecodeError):
        pass
    try:
        updated, _missing = scan_registry(registry)
        if updated:
            registry.save()
        log.debug("automatic scan refreshed %d rows", len(updated))
        atomic_write(marker, today.isoformat() + "\n")
    except (OSError, ProjdashError) as exc:
        log.debug("automatic scan skipped: %s", exc)


def open_url(url: str) -> None:
    webbrowser.open(url)


def open_folder(path: str) -> None:
    """Open a folder (or file) with the platform's default application."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([opener, path], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ProjdashError("cannot open %s: %s" % (path, exc))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

class Context:
    def __init__(self, db_path: str, today: _dt.date, style: Style, inner: int):
        self.db_path = db_path
        self.today = today
        self.style = style
        self.inner = inner

    def load(self) -> Registry:
        return Registry.load(self.db_path)


def emit(lines: Iterable[str]) -> None:
    for line in lines:
        print(line)


def cmd_dash(ctx: Context, args) -> int:
    registry = ctx.load()
    if not getattr(args, "no_scan", False):
        auto_scan(registry, ctx.today)
    clock = _dt.datetime.now().strftime("%H:%M")
    emit(render_dashboard(registry.projects, ctx.today, ctx.style, ctx.inner, clock))
    return EXIT_OK


def cmd_list(ctx: Context, args) -> int:
    projects = ctx.load().projects
    if args.filter:
        projects = filter_projects(projects, args.filter)
        if not projects:
            raise ProjdashError("no project matches '%s'" % args.filter)
    emit(render_list(projects, ctx.today, ctx.style))
    return EXIT_OK


def cmd_next(ctx: Context, args) -> int:
    emit(render_next(ctx.load().projects, ctx.style))
    return EXIT_OK


def cmd_show(ctx: Context, args) -> int:
    project = ctx.load().resolve(args.id)
    emit(render_show(project, ctx.today, ctx.style))
    return EXIT_OK


def cmd_set(ctx: Context, args) -> int:
    registry = ctx.load()
    project = registry.resolve(args.id)
    field = FIELD_ALIASES.get(args.field.lower())
    if field is None:
        raise ProjdashError("unknown field '%s' (choose from: %s)" % (
            args.field, ", ".join(name for name in FIELDS if name != "id")))
    value = validate_field(field, " ".join(args.value))
    project.set(field, value)
    if field != "updated":
        project.set("updated", ctx.today.isoformat())
    registry.save()
    print("%s %s: %s = %s %s" % (
        ctx.style("OK", "green"), project.id, field, value,
        ctx.style("(updated %s)" % project.updated, "dim")))
    return EXIT_OK


def cmd_touch(ctx: Context, args) -> int:
    registry = ctx.load()
    project = registry.resolve(args.id)
    project.set("updated", ctx.today.isoformat())
    registry.save()
    print("%s %s worked on today (%s)" % (
        ctx.style("OK", "green"), project.id, ctx.today.isoformat()))
    return EXIT_OK


def cmd_done(ctx: Context, args) -> int:
    registry = ctx.load()
    project = registry.resolve(args.id)
    project.set("status", "done")
    project.set("progress", "100")
    project.set("updated", ctx.today.isoformat())
    registry.save()
    print("%s %s" % (ctx.style("DONE", "green"), project.id))
    return EXIT_OK


def _set_hidden(ctx: Context, query: str, hidden: bool) -> int:
    registry = ctx.load()
    project = registry.resolve(query)
    project.set("hidden", HIDDEN_MARK if hidden else "")
    registry.save()
    if hidden:
        print("%s %s %s" % (ctx.style("HIDDEN", "yellow"), project.id, ctx.style(
            "- no longer in the dashboard (still in `projdash list`)", "dim")))
    else:
        print("%s %s %s" % (ctx.style("VISIBLE", "green"), project.id,
                            ctx.style("- back in the dashboard", "dim")))
    return EXIT_OK


def cmd_hide(ctx: Context, args) -> int:
    return _set_hidden(ctx, args.id, True)


def cmd_unhide(ctx: Context, args) -> int:
    return _set_hidden(ctx, args.id, False)


ADD_PROMPTS = (
    # field, flag attribute, prompt, default
    ("name", "name", "name", None),
    ("category", "category", "category", "tool"),
    ("status", "status", "status (%s)" % "/".join(STATUSES), "idea"),
    ("progress", "progress", "progress 0-100", "0"),
    ("priority", "priority", "priority 1-3 (1 = high)", "2"),
    ("path", "path", "local folder", ""),
    ("url", "url", "url", ""),
    ("next_step", "next_step", "next step", ""),
    ("notes", "notes", "notes", ""),
)


def _ask(prompt: str, default: Optional[str]) -> str:
    label = "%s [%s]: " % (prompt, default) if default else prompt + ": "
    try:
        return input(label).strip()
    except EOFError:
        return ""


def cmd_add(ctx: Context, args) -> int:
    registry = ctx.load()
    given_id = args.id_flag if args.id_flag is not None else args.id
    interactive = given_id is None
    if interactive:
        print("%s %s" % (ctx.style("New project", "bold"),
                         ctx.style("(press Enter to skip a field)", "dim")))
        given_id = _ask("id (short, no spaces)", None)
    values = {"id": validate_field("id", given_id or "")}
    if any(p.id == values["id"] for p in registry.projects):
        raise ProjdashError("a project with id '%s' already exists" % values["id"])
    for field, attr, prompt, default in ADD_PROMPTS:
        if field == "name":
            default = values["id"]
        raw = getattr(args, attr)
        if raw is None:
            raw = _ask(prompt, default) if interactive else ""
        values[field] = validate_field(field, raw if raw.strip() else (default or ""))
    values["updated"] = ctx.today.isoformat()
    values["hidden"] = HIDDEN_MARK if args.hidden else ""
    registry.append(Project.create(values))
    registry.save()
    print("%s %s" % (ctx.style("Added", "green"), values["id"]))
    return EXIT_OK


def cmd_stale(ctx: Context, args) -> int:
    if args.days < 0:
        raise ProjdashError("days must be zero or more")
    emit(render_stale(ctx.load().projects, ctx.today, args.days, ctx.style))
    return EXIT_OK


def cmd_research(ctx: Context, args) -> int:
    emit(render_research(ctx.load().projects, ctx.style, ctx.inner))
    return EXIT_OK


def cmd_scan(ctx: Context, args) -> int:
    registry = ctx.load()
    print("Scanning project folders...")
    updated, missing = scan_registry(registry)
    for project_id, folder in missing:
        print("  %s %s -> %s" % (ctx.style("missing", "red"), project_id, folder))
    for project_id, date in updated:
        print("  %s      %s -> %s" % (ctx.style("ok", "green"), project_id, date))
    if updated:
        registry.save()
    print("Done. %d updated, %d folders missing." % (len(updated), len(missing)))
    return EXIT_OK


def cmd_open(ctx: Context, args) -> int:
    registry = ctx.load()
    if not args.id:
        print("Usage: projdash open <id> - projects with a folder or a URL:",
              file=sys.stderr)
        print("", file=sys.stderr)
        for project in registry.projects:
            if project.path or project.url:
                print("  %-18s %-30.30s %s" % (
                    project.id, project.name, project.url or project.path), file=sys.stderr)
        return EXIT_ERROR
    project = registry.resolve(args.id)
    opened = 0
    if project.path:
        if is_url(project.path):
            open_url(project.path)
            opened += 1
        else:
            folder = expand_path(project.path)
            if os.path.isdir(folder):
                open_folder(folder)
                opened += 1
            else:
                log.warning("folder not found: %s", folder)
    if project.url:
        if is_url(project.url):
            open_url(project.url)
            opened += 1
        else:
            log.warning("url is not http(s), not opened: %s", project.url)
    if not opened:
        raise ProjdashError("nothing to open for '%s' (no existing folder, no URL)" % project.id)
    return EXIT_OK


def cmd_md(ctx: Context, args) -> int:
    report = render_markdown(ctx.load().projects, ctx.today)
    if not args.output:
        if args.open:
            raise ProjdashError("--open needs an output file: projdash md FILE --open")
        sys.stdout.write(report)
        return EXIT_OK
    output = os.path.expanduser(args.output)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        atomic_write(output, report)
    except OSError as exc:
        raise ProjdashError("cannot write report %s: %s" % (output, exc))
    print("Report: %s" % output)
    if args.open:
        open_folder(output)
    return EXIT_OK


def cmd_init(ctx: Context, args) -> int:
    path = ctx.db_path
    if os.path.exists(path):
        raise ProjdashError("registry already exists, not overwriting: %s" % path)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        atomic_write(path, REGISTRY_HEADER)
    except OSError as exc:
        raise ProjdashError("cannot create registry %s: %s" % (path, exc))
    print("Created %s" % path)
    print("Add your first project with `projdash add`, or edit the file by hand.")
    return EXIT_OK


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

# name -> (handler, aliases, help)
COMMANDS = {
    "dash": (cmd_dash, ["today"], "the dashboard (default)"),
    "list": (cmd_list, ["ls"], "every project, optionally filtered by text"),
    "next": (cmd_next, ["now"], "the 8 most urgent open projects and their next step"),
    "show": (cmd_show, ["s"], "all the details of one project"),
    "set": (cmd_set, [], "change one field of a project"),
    "touch": (cmd_touch, ["t"], "mark a project as worked on today"),
    "done": (cmd_done, ["close"], "close a project (status done, progress 100)"),
    "add": (cmd_add, ["new"], "add a project (interactive without an id)"),
    "hide": (cmd_hide, [], "keep a project in the registry but off the dashboard"),
    "unhide": (cmd_unhide, [], "put a hidden project back on the dashboard"),
    "stale": (cmd_stale, [], "open projects untouched for more than N days (default 14)"),
    "research": (cmd_research, [], "projects in the `research` category, with conclusions"),
    "scan": (cmd_scan, [], "refresh `updated` from the newest file in each project folder"),
    "open": (cmd_open, ["o"], "open the project's folder and URL"),
    "md": (cmd_md, ["report"], "markdown status report (stdout, or a file)"),
    "init": (cmd_init, [], "create an empty registry"),
    "help": (None, [], "show this help"),
}


def _command_words() -> Dict[str, str]:
    words = {}
    for name, (_handler, aliases, _help) in COMMANDS.items():
        words[name] = name
        for alias in aliases:
            words[alias] = name
    return words


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS,
                        help="registry file (default: $PROJDASH_DB, else "
                             "$XDG_CONFIG_HOME/projdash/projects.tsv)")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="never use colours (also: NO_COLOR environment variable)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="debug messages on stderr")

    parser = argparse.ArgumentParser(
        prog="projdash", parents=[common],
        description="A terminal dashboard for many small projects, backed by one TSV file.",
        epilog="Ids match exactly or by unique prefix. `projdash <text>` is a "
               "shortcut for `projdash list <text>`.")
    parser.add_argument("--version", action="version", version="projdash " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        _handler, aliases, text = COMMANDS[name]
        return sub.add_parser(name, aliases=aliases, parents=[common], help=text,
                              description=text, **kwargs)

    p = add("dash")
    p.add_argument("--no-scan", action="store_true",
                   help="skip the once-a-day automatic folder scan")
    p = add("list")
    p.add_argument("filter", nargs="?", default="", help="case-insensitive text to look for")
    add("next")
    p = add("show")
    p.add_argument("id")
    p = add("set")
    p.add_argument("id")
    p.add_argument("field", help="name, category, status, progress, priority, path, url, "
                                 "next, notes, hidden, updated")
    p.add_argument("value", nargs="*", help="new value (several words are joined; "
                                            "none clears the field)")
    p = add("touch")
    p.add_argument("id")
    p = add("done")
    p.add_argument("id")
    p = add("add")
    p.add_argument("id", nargs="?", help="short unique id; without it, questions are asked")
    p.add_argument("--id", dest="id_flag", metavar="ID", help="same as the positional id")
    p.add_argument("--name")
    p.add_argument("--category", help="default: tool")
    p.add_argument("--status", help="default: idea")
    p.add_argument("--progress", help="0-100, default 0")
    p.add_argument("--priority", help="1-3, default 2")
    p.add_argument("--path")
    p.add_argument("--url")
    p.add_argument("--next", dest="next_step", metavar="NEXT_STEP")
    p.add_argument("--notes")
    p.add_argument("--hidden", action="store_true", help="add it hidden from the dashboard")
    p = add("hide")
    p.add_argument("id")
    p = add("unhide")
    p.add_argument("id")
    p = add("stale")
    p.add_argument("days", nargs="?", type=int, default=STALE_DAYS)
    p = add("research")
    p.add_argument("action", nargs="?", choices=["list"], help=argparse.SUPPRESS)
    add("scan")
    p = add("open")
    p.add_argument("id", nargs="?", default="")
    p = add("md")
    p.add_argument("output", nargs="?", default="", metavar="FILE",
                   help="write the report here instead of printing it")
    p.add_argument("--open", action="store_true", help="open FILE once written")
    add("init")
    add("help")
    return parser


def _implicit_list(argv: List[str]) -> List[str]:
    """`projdash bakery` means `projdash list bakery`."""
    words = _command_words()
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
        elif token == "--db":
            skip = True
        elif token.startswith("-"):
            continue
        else:
            if token not in words:
                return argv[:index] + ["list"] + argv[index:]
            break
    return argv


def resolve_today(today: Optional[_dt.date] = None) -> _dt.date:
    if today is not None:
        return today
    forced = os.environ.get("PROJDASH_TODAY")
    if forced:
        date = parse_date(forced)
        if date is None:
            raise ProjdashError("PROJDASH_TODAY must be a date in YYYY-MM-DD format")
        return date
    return _dt.date.today()


def main(argv: Optional[Sequence[str]] = None, today: Optional[_dt.date] = None) -> int:
    """Run the CLI and return the process exit code."""
    argv = _implicit_list(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:                 # argparse: 0 for --help, 2 for usage errors
        return exc.code if isinstance(exc.code, int) else EXIT_OK

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="projdash: %(levelname)s: %(message)s", stream=sys.stderr, force=True)

    for stream in (sys.stdout, sys.stderr):   # never die on an unencodable character
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    command = _command_words()[args.command or "dash"]
    if command == "help":
        parser.print_help()
        return EXIT_OK

    try:
        db = getattr(args, "db", None)
        use_colour = colour_enabled(getattr(args, "no_color", False))
        if use_colour and os.name == "nt":
            os.system("")                     # switches on ANSI processing on Windows
        ctx = Context(
            db_path=os.path.expanduser(db) if db else default_db_path(),
            today=resolve_today(today),
            style=Style(use_colour),
            inner=inner_width())
        log.debug("registry: %s", ctx.db_path)
        return COMMANDS[command][0](ctx, args)
    except ProjdashError as exc:
        print("projdash: error: %s" % exc, file=sys.stderr)
        return exc.code
    except BrokenPipeError:                   # e.g. `projdash list | head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except OSError as exc:
        print("projdash: error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
