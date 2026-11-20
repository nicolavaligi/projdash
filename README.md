# projdash

A terminal dashboard for people juggling many small projects.

It answers three questions every morning: **what am I working on, where is
each thing, and what comes next?** The whole database is one TSV file that you
can also edit by hand.

projdash is for freelancers, solo developers, researchers and anyone with more
side projects than memory: ten or forty things in different states, each with a
folder somewhere, maybe a URL, and one next step that you would otherwise
forget. It is not a task manager and it does not want to be one: it tracks one
line per project, not a backlog.

- Python 3.9+, standard library only, a single file
- Linux, macOS and Windows
- No network, no account, no telemetry

## What it looks like

`projdash --db projects.example.tsv`, with the example file from this repository:

```text
  +-- DASHBOARD  Fri 20 November 2026 - 09:15 ---------------------------------------+
  |                                                                                  |
  | 2 active     1 live     1 paused     1 blocked     1 done     (7 projects)       |
  | average progress of open work    [####......] 43%                                |
  |                                                                                  |
  +----------------------------------------------------------------------------------+

  +-- TODAY'S FOCUS (high priority) +------+-----------------------------------------+
  | #  | PROJECT                    | PROG | NEXT STEP                               |
  +----+----------------------------+------+-----------------------------------------+
  | 1  | Invoice parser             |  60% | Handle multi-page PDF invoices          |
  | 2  | Recipe box app             |  45% | Wait for the app store review, then fix |
  +----+----------------------------+------+-----------------------------------------+

  +-- IN PROGRESS -----------+--------+-------------------+-------+------------------+
  | PROJECT                  | PRIO   | PROGRESS          | UPD.  | ID               |
  +--------------------------+--------+-------------------+-------+------------------+
  | Invoice parser           | high   | [######....]  60% | 2d    | invoice-parser   |
  | Recipe box app           | high   | [####......]  45% | 17d   | recipe-app       |
  | Garden sensor logger     | medium | [###.......]  35% | 23d   | garden-sensors   |
  +--------------------------+--------+-------------------+-------+------------------+

  +-- STALE (untouched for more than 14 days) ---------+----------+------------------+
  | PROJECT                                 | STATUS   | IDLE FOR | ID               |
  +-----------------------------------------+----------+----------+------------------+
  | Family photo archive                    | paused   | 67d      | photo-archive    |
  | Garden sensor logger                    | active   | 23d      | garden-sensors   |
  | Monthly newsletter                      | idea     | 19d      | newsletter       |
  | Recipe box app                          | blocked  | 17d      | recipe-app       |
  +-----------------------------------------+----------+----------+------------------+

  +-- LIVE --------------------+-----------------------------------------------------+
  | PROJECT                    | URL                                                 |
  +----------------------------+-----------------------------------------------------+
  | Bakery website             | https://bakery.example.com                          |
  +----------------------------+-----------------------------------------------------+

  +-- RESEARCH (do not forget) ------------------------------------------------------+
  | RESEARCH                 | WHAT IT SAYS / WHAT TO DO WITH IT                     |
  +--------------------------+-------------------------------------------------------+
  | Static site generators c | Plain templates beat heavy frameworks under 20 pages  |
  |                          | -> Reuse the build setup for the bakery site          |
  |                          | ~/notes/static-gen-study                              |
  +--------------------------+-------------------------------------------------------+

  projdash list | show <id> | research | touch <id> (worked on it today) | help
```

In a terminal the dashboard is coloured (blocked in red, stale in yellow, next
steps in green). The sample was produced with `PROJDASH_TODAY=2026-11-20`, so
the day counts match the dates in the example file.

## Quick start

```sh
pip install git+https://github.com/nicolavaligi/projdash
# or simply copy projdash.py somewhere on your PATH: it has no dependencies

projdash init                       # creates ~/.config/projdash/projects.tsv
projdash add                        # answers a few questions
projdash add blog --name "Blog redesign" --status active --priority 1 \
    --path ~/code/blog --next "Pick the fonts"
projdash                            # the dashboard
```

To look around before creating anything, clone the repository and run
`python3 projdash.py --db projects.example.tsv`.

Day to day:

```sh
projdash                            # what is going on
projdash next                       # what to do now
projdash set blog progress 40       # ids match by unique prefix: `set bl ...` works too
projdash set blog next Write the about page
projdash touch blog                 # "I worked on it today"
projdash done blog
```

## Commands

| Command | What it does |
|---|---|
| `projdash` / `projdash dash` | The dashboard. Once a day it also runs a silent `scan` (`--no-scan` skips it). |
| `projdash list [text]` | Every project, including hidden ones, sorted by priority, then status, then progress. `text` filters case-insensitively on the whole row. `projdash text` is a shortcut. |
| `projdash next` | The 8 most urgent open projects, each with its next step. |
| `projdash show <id>` | Everything about one project, whether its folder exists, and its 3 most recently modified files. |
| `projdash set <id> <field> <value>` | Change one field and set `updated` to today. Fields: `name`, `category`, `status`, `progress`, `priority`, `path`, `url`, `next`, `notes`, `hidden`, `updated`. Values are validated; several words are joined, no value clears the field. |
| `projdash touch <id>` | Set `updated` to today. |
| `projdash done <id>` | Status `done`, progress 100. |
| `projdash add [id] [--name ...]` | Add a project. Without an id it asks questions; with an id it uses the flags (`--name --category --status --progress --priority --path --url --next --notes --hidden`) and asks nothing. |
| `projdash hide <id>` / `unhide <id>` | Keep a project in the registry but leave it out of the dashboard. |
| `projdash stale [days]` | Open projects untouched for more than `days` days (default 14), stalest first. |
| `projdash research` | Projects in the `research` category, with their conclusions. |
| `projdash scan` | Move each project's `updated` forward to the newest file modification time in its folder. Skips `.git`, `node_modules`, `.venv`, `__pycache__`. |
| `projdash open <id>` | Open the project folder in the file manager and its URL in the browser. |
| `projdash md [FILE] [--open]` | Markdown status report grouped by status: printed, or written to `FILE`. |
| `projdash init` | Create an empty registry with a commented header. Never overwrites. |

Ids match exactly, or by a prefix that identifies one project only. An
ambiguous prefix is an error that lists the candidates; nothing is changed.

Options, accepted before or after the command:

| Option | Meaning |
|---|---|
| `--db PATH` | Registry file to use. |
| `--no-color` | No colours. Colours are also off when output is not a terminal or `NO_COLOR` is set. |
| `-v`, `--verbose` | Debug messages on stderr. They contain file paths and row counts, never field values. |
| `--version`, `-h` | Version, help. |

The registry is looked up in this order: `--db PATH`, the `PROJDASH_DB`
environment variable, `$XDG_CONFIG_HOME/projdash/projects.tsv`, and finally
`~/.config/projdash/projects.tsv`.

Exit codes: `0` success, `1` error (unknown or ambiguous id, invalid value,
missing registry, filter with no match), `2` wrong command line. Errors go to
stderr.

The dashboard is designed for 86 columns. It never grows wider; on a narrower
terminal the widest column of each table shrinks (down to 72 columns in total).

## The TSV format

One project per line, 12 fields separated by a single TAB. Lines starting with
`#` and blank lines are ignored, and preserved whenever projdash rewrites the
file, so you can group and annotate your projects.

| # | Field | Content |
|---|---|---|
| 1 | `id` | Short unique handle, no spaces. |
| 2 | `name` | Human-readable name. |
| 3 | `category` | Free text. `research` gets its own dashboard section. |
| 4 | `status` | `idea`, `active`, `paused`, `live`, `done`, `blocked`. |
| 5 | `progress` | 0-100. |
| 6 | `priority` | 1 = high, 2 = medium, 3 = low. |
| 7 | `path` | Project folder; `~` is expanded. |
| 8 | `url` | Where it is published, if anywhere. |
| 9 | `next_step` | The very next thing to do. |
| 10 | `updated` | `YYYY-MM-DD`. |
| 11 | `notes` | Free text. For research: the conclusion. |
| 12 | `hidden` | `x` = keep in the registry, leave out of the dashboard. |

Empty fields are fine as long as the TABs are there. A field cannot contain a
TAB or a line break; projdash refuses such values. Hand-edited mistakes (a
missing column, a word where a number belongs) never crash a view: they are
read as empty, 0% or low priority.

See [`projects.example.tsv`](projects.example.tsv).

## Why a TSV and not a database

- **You can read and fix it without the tool.** Any editor, `grep`, `cut`,
  `sort`, a spreadsheet. If projdash disappeared tomorrow, your data would
  still be a plain table.
- **It diffs.** One line per project means a meaningful history if you keep the
  file under (private) version control or in a synced folder.
- **The scale does not need more.** This is tens of rows, maybe a few hundred.
  A database would add a schema, migrations and a binary file to solve a
  problem that does not exist here.
- **TSV rather than CSV** because names, notes and next steps are full of
  commas and quotes and almost never contain a TAB, so no quoting rules are
  needed and a line can always be split by hand.

The cost is that there is no locking: projdash writes atomically (a temporary
file in the same directory, then a rename), so the file is never left half
written, but two simultaneous edits mean the last one wins. For a personal
file that is the right trade.

## Why Python

projdash started as a Bash and awk script. That version depended on BSD
`date -j`, BSD `stat -f` and the macOS `open` command, so it only ran on
macOS, and rewriting the TSV through awk was fragile with empty fields and with
values containing backslashes.

Python's standard library solves all three problems with no dependencies:

- **Portability**: `datetime`, `os.walk`, `webbrowser` and `os.replace` behave
  the same on Linux, macOS and Windows.
- **Safe TSV handling**: rows are split and joined explicitly, empty fields
  stay empty, untouched lines are written back byte for byte.
- **Testability**: selection, sorting and rendering are pure functions with an
  injectable "today", covered by a test suite that runs in under a second.

Python 3.9 is the floor because it is what current LTS distributions and macOS
ship; nothing newer is needed.

## Privacy & data

- Your data is **one local file**. projdash never opens a network connection
  and has no telemetry, analytics or update check. The only things that leave
  the process are the ones you ask for: `open` hands a URL to your browser and
  a folder to your file manager.
- The registry is likely to contain client names, private URLs and notes.
  **Keep it out of public repositories.** The default location
  (`~/.config/projdash/projects.tsv`) is outside any repository on purpose, and
  this project's `.gitignore` excludes `projects.tsv`.
- `scan` and `show` read file modification times (and, for `show`, file names)
  inside the folders you listed. They never read file contents.
- Besides the registry, projdash writes one small marker file next to it
  (`.projects.tsv.last-scan`, containing a date) and the reports you ask for
  with `md FILE`.
- Debug output (`-v`) is limited to file paths and counts.

If the registry holds other people's personal data, it is your file and your
responsibility: store it and back it up accordingly.

## Development

```sh
git clone https://github.com/nicolavaligi/projdash
cd projdash
python3 -m unittest discover -s tests -v
```

The tests use only temporary directories and invented projects. No third-party
packages are needed, for running or for testing. CI runs the suite on Python
3.9 and 3.13.

`PROJDASH_TODAY=YYYY-MM-DD` overrides the current date, which is handy for
reproducible output and for trying the dashboard on old data.

Code layout, all in `projdash.py`: registry parsing and atomic writing,
validation, selection and sorting rules, renderers that return lines of text,
file-system helpers (`scan`, openers), then the commands and the argument
parser.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Nicola Valigi.
