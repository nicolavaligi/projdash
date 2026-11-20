"""Tests for projdash. Everything runs in temporary directories on invented data.

    python3 -m unittest discover -s tests -v
"""

import contextlib
import datetime
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import projdash  # noqa: E402

TODAY = datetime.date(2026, 11, 20)

SAMPLE_ROWS = [
    # id, name, category, status, progress, priority, path, url, next, updated, notes, hidden
    ["alpha-api", "Alpha API", "tool", "active", "60", "1", "", "",
     "Write the pagination tests", "2026-11-18", "Backend for the alpha app", ""],
    ["alpha-web", "Alpha web client", "web", "blocked", "45", "1", "", "",
     "Wait for the design review", "2026-11-03", "", ""],
    ["beta", "Beta importer", "tool", "active", "35", "2", "", "",
     "Map the CSV columns", "2026-10-28", "", ""],
    ["shop", "Toy shop site", "web", "live", "100", "2", "", "https://shop.example.com",
     "Add the winter catalogue", "2026-11-12", "", ""],
    ["archive", "Slide archive", "personal", "paused", "20", "3", "", "",
     "Choose a naming scheme", "2026-09-14", "", ""],
    ["zine", "Quarterly zine", "writing", "idea", "0", "3", "", "", "", "2026-11-01", "", ""],
    ["parked", "Parked experiment", "tool", "paused", "70", "3", "", "",
     "Decide whether to keep it", "2026-08-02", "", "x"],
    ["study", "Queue libraries compared", "research", "done", "100", "2", "", "",
     "Reuse the retry settings", "2026-10-05", "A plain table beats a broker here", ""],
]
SAMPLE = (
    "# sample registry\n"
    "# id\tname\tcategory\n"
    "\n"
    + "\n".join("\t".join(row) for row in SAMPLE_ROWS[:4]) + "\n"
    + "\n"
    + "# second block\n"
    + "\n".join("\t".join(row) for row in SAMPLE_ROWS[4:]) + "\n"
)


class TtyIO(io.StringIO):
    def isatty(self):
        return True


class Base(unittest.TestCase):
    sample = SAMPLE

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db = os.path.join(self.dir, "projects.tsv")
        with open(self.db, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.sample)
        env = {k: v for k, v in os.environ.items()
               if k not in ("PROJDASH_DB", "PROJDASH_TODAY", "NO_COLOR")}
        env["XDG_CONFIG_HOME"] = os.path.join(self.dir, "config")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, stdout=None, db=True):
        out = stdout if stdout is not None else io.StringIO()
        err = io.StringIO()
        args = (["--db", self.db] if db else []) + list(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = projdash.main(args, today=TODAY)
        return code, out.getvalue(), err.getvalue()

    def read_db(self):
        with open(self.db, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def row(self, project_id):
        for line in self.read_db().split("\n"):
            fields = line.split("\t")
            if fields[0] == project_id:
                return fields
        self.fail("row %s not found" % project_id)

    def assert_only_registry_left(self):
        self.assertEqual(os.listdir(self.dir), ["projects.tsv"])


# ---------------------------------------------------------------- TSV layer

class RegistryParsing(unittest.TestCase):
    def test_round_trip_preserves_comments_blank_lines_and_empty_fields(self):
        registry = projdash.Registry.parse(SAMPLE)
        self.assertEqual(registry.dumps(), SAMPLE)
        self.assertEqual(len(registry.projects), 8)
        zine = registry.resolve("zine")
        self.assertEqual(zine.fields[6:9], ["", "", ""])
        self.assertEqual(zine.updated, "2026-11-01")

    def test_round_trip_of_odd_files(self):
        for text in ("", "# only a comment", "a\tA\ttool\tidea", "a\tb\r\n# c\r\n\r\nd\te\r\n",
                     "\n\n", "x\ty\t\t\t\t\t\t\t\t\t\t\textra\tcolumns\n"):
            with self.subTest(text=text):
                self.assertEqual(projdash.Registry.parse(text).dumps(), text)

    def test_short_rows_are_readable_and_get_padded_only_when_modified(self):
        registry = projdash.Registry.parse("# c\nshort\tShort row\ttool\tidea\n")
        project = registry.resolve("short")
        self.assertEqual((project.progress, project.priority, project.updated), (0, 3, ""))
        self.assertFalse(project.hidden)
        project.set("notes", "hello")
        lines = registry.dumps().split("\n")
        self.assertEqual(lines[0], "# c")
        self.assertEqual(lines[1].split("\t"),
                         ["short", "Short row", "tool", "idea", "", "", "", "", "", "",
                          "hello", ""])

    def test_bad_numbers_never_crash_a_view(self):
        registry = projdash.Registry.parse("a\tA\ttool\tactive\tlots\turgent\t\t\t\tsoon\t\t\n")
        lines = projdash.render_dashboard(registry.projects, TODAY, projdash.Style(False))
        self.assertTrue(any("A " in line for line in lines))

    def test_append_adds_missing_final_newline(self):
        registry = projdash.Registry.parse("a\tA")
        registry.append(projdash.Project.create({"id": "b", "name": "B"}))
        self.assertEqual(registry.dumps().count("\n"), 2)
        self.assertTrue(registry.dumps().startswith("a\tA\nb\tB\t"))

    def test_field_rejects_tab_and_newline(self):
        project = projdash.Registry.parse(SAMPLE).resolve("beta")
        for bad in ("a\tb", "a\nb", "a\rb"):
            with self.assertRaises(projdash.ProjdashError):
                project.set("notes", bad)


class IdResolution(unittest.TestCase):
    def setUp(self):
        self.registry = projdash.Registry.parse(SAMPLE)

    def test_exact(self):
        self.assertEqual(self.registry.resolve("beta").name, "Beta importer")

    def test_unique_prefix(self):
        self.assertEqual(self.registry.resolve("sh").id, "shop")
        self.assertEqual(self.registry.resolve("alpha-w").id, "alpha-web")

    def test_exact_wins_over_prefix(self):
        registry = projdash.Registry.parse("app\tApp\napp-two\tApp two\n")
        self.assertEqual(registry.resolve("app").id, "app")

    def test_ambiguous_prefix_lists_candidates(self):
        with self.assertRaises(projdash.ProjdashError) as caught:
            self.registry.resolve("alpha")
        self.assertIn("alpha-api, alpha-web", str(caught.exception))

    def test_missing_and_empty(self):
        for query in ("nope", "", "  "):
            with self.assertRaises(projdash.ProjdashError):
                self.registry.resolve(query)


class Validation(unittest.TestCase):
    def test_accepts_and_normalises(self):
        cases = [("status", "ACTIVE", "active"), ("progress", "70%", "70"),
                 ("progress", "0", "0"), ("progress", "100", "100"),
                 ("priority", "high", "1"), ("priority", "3", "3"),
                 ("updated", "2026-1-5", "2026-01-05"), ("hidden", "yes", "x"),
                 ("hidden", "", ""), ("notes", "  spaced  ", "spaced")]
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                self.assertEqual(projdash.validate_field(field, value), expected)

    def test_rejects(self):
        cases = [("status", "attivo"), ("status", ""), ("progress", "101"), ("progress", "-1"),
                 ("progress", "half"), ("progress", "1.5"), ("priority", "0"),
                 ("priority", "4"), ("priority", "urgent"), ("updated", "20/11/2026"),
                 ("updated", "2026-02-30"), ("hidden", "maybe"), ("id", ""),
                 ("id", "two words"), ("id", "#x"), ("name", ""), ("notes", "a\tb")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(projdash.ProjdashError):
                    projdash.validate_field(field, value)


# ------------------------------------------------------------ pure helpers

class Helpers(unittest.TestCase):
    def test_days_since(self):
        self.assertEqual(projdash.days_since("2026-11-20", TODAY), 0)
        self.assertEqual(projdash.days_since("2026-11-05", TODAY), 15)
        self.assertEqual(projdash.days_since("2026-12-01", TODAY), 0)   # future date
        self.assertIsNone(projdash.days_since("", TODAY))
        self.assertIsNone(projdash.days_since("yesterday", TODAY))

    def test_bar(self):
        self.assertEqual(projdash.bar(0), "..........")
        self.assertEqual(projdash.bar(45), "####......")
        self.assertEqual(projdash.bar(100), "##########")
        self.assertEqual(projdash.bar(250), "##########")

    def test_default_db_path(self):
        self.assertEqual(projdash.default_db_path({"PROJDASH_DB": "/data/p.tsv"}),
                         "/data/p.tsv")
        self.assertEqual(
            projdash.default_db_path({"XDG_CONFIG_HOME": "/cfg"}),
            os.path.join("/cfg", "projdash", "projects.tsv"))
        self.assertEqual(
            projdash.default_db_path({}),
            os.path.join(os.path.expanduser("~"), ".config", "projdash", "projects.tsv"))

    def test_implicit_list(self):
        self.assertEqual(projdash._implicit_list(["bakery"]), ["list", "bakery"])
        self.assertEqual(projdash._implicit_list(["--db", "x.tsv", "bakery"]),
                         ["--db", "x.tsv", "list", "bakery"])
        self.assertEqual(projdash._implicit_list(["--db", "x.tsv", "show", "a"]),
                         ["--db", "x.tsv", "show", "a"])
        self.assertEqual(projdash._implicit_list([]), [])


class SortingAndSelection(unittest.TestCase):
    def setUp(self):
        self.projects = projdash.Registry.parse(SAMPLE).projects

    def ids(self, projects):
        return [p.id for p in projects]

    def test_list_order_priority_then_status_then_progress(self):
        self.assertEqual(self.ids(projdash.sort_for_list(self.projects)), [
            "alpha-web",   # p1 blocked
            "alpha-api",   # p1 active
            "beta",        # p2 active
            "shop",        # p2 live
            "study",       # p2 done
            "parked",      # p3 paused 70
            "archive",     # p3 paused 20
            "zine",        # p3 idea
        ])

    def test_list_order_progress_breaks_ties_and_is_stable(self):
        registry = projdash.Registry.parse(
            "a\tA\tt\tactive\t10\t2\nb\tB\tt\tactive\t80\t2\nc\tC\tt\tactive\t80\t2\n")
        self.assertEqual(self.ids(projdash.sort_for_list(registry.projects)), ["b", "c", "a"])

    def test_focus_is_open_visible_high_priority_by_progress(self):
        self.assertEqual(self.ids(projdash.focus_items(self.projects)),
                         ["alpha-api", "alpha-web"])
        many = projdash.Registry.parse("".join(
            "p%d\tP%d\tt\tactive\t%d\t1\n" % (i, i, i * 10) for i in range(7))).projects
        self.assertEqual(self.ids(projdash.focus_items(many)), ["p6", "p5", "p4", "p3"])

    def test_in_progress_is_active_or_blocked_by_priority_then_progress(self):
        self.assertEqual(self.ids(projdash.in_progress_items(self.projects)),
                         ["alpha-api", "alpha-web", "beta"])

    def test_next_is_every_open_project_capped_at_eight(self):
        self.assertEqual(self.ids(projdash.next_items(self.projects)), [
            "alpha-api", "alpha-web", "shop", "beta", "parked", "archive", "zine"])
        many = projdash.Registry.parse("".join(
            "p%d\tP\tt\tidea\t0\t2\n" % i for i in range(12))).projects
        self.assertEqual(len(projdash.next_items(many)), 8)

    def test_stale_with_injected_today(self):
        stale = projdash.stale_items(self.projects, TODAY, 14)
        self.assertEqual([(p.id, age) for p, age in stale], [
            ("parked", 110), ("archive", 67), ("beta", 23), ("zine", 19), ("alpha-web", 17)])
        # strictly "more than": 17 days old is not stale at a 17-day threshold
        self.assertNotIn("alpha-web", self.ids(
            p for p, _ in projdash.stale_items(self.projects, TODAY, 17)))
        # a different today changes the answer
        earlier = datetime.date(2026, 11, 10)
        self.assertEqual(self.ids(p for p, _ in projdash.stale_items(self.projects, earlier)),
                         ["parked", "archive"])
        # done projects are never stale; hidden ones can be left out
        self.assertNotIn("study", self.ids(p for p, _ in stale))
        visible = projdash.stale_items(self.projects, TODAY, 14, include_hidden=False)
        self.assertNotIn("parked", self.ids(p for p, _ in visible))

    def test_stale_treats_missing_date_as_stale(self):
        projects = projdash.Registry.parse("a\tA\tt\tactive\t0\t2\t\t\t\t\t\t\n").projects
        self.assertEqual(projdash.stale_items(projects, TODAY), [(projects[0], None)])

    def test_filter_is_case_insensitive_substring(self):
        self.assertEqual(self.ids(projdash.filter_projects(self.projects, "ALPHA")),
                         ["alpha-api", "alpha-web"])
        self.assertEqual(self.ids(projdash.filter_projects(self.projects, "a.c")), [])


# ---------------------------------------------------------------- rendering

class Dashboard(Base):
    def test_sections_and_content(self):
        code, out, err = self.run_cli("dash")
        self.assertEqual((code, err), (0, ""))
        for title in ("DASHBOARD  Fri 20 November 2026", "TODAY'S FOCUS", "IN PROGRESS",
                      "STALE (untouched for more than 14 days)", "LIVE", "RESEARCH"):
            self.assertIn("+-- " + title, out)
        self.assertIn("2 active     1 live     1 paused     1 blocked     1 done     "
                      "(7 projects)", out)
        # open, visible: 60 45 35 100 20 0 -> 260 // 6 = 43
        self.assertIn("average progress of open work    [####......] 43%", out)
        self.assertIn("| 1  | Alpha API", out)
        self.assertIn("Write the pagination tests", out)
        self.assertIn("| high   | [######....]  60% | 2d ", out)
        self.assertIn("| medium | [###.......]  35% | 23d", out)
        self.assertIn("https://shop.example.com", out)
        self.assertIn("A plain table beats a broker here", out)
        self.assertIn("-> Reuse the retry settings", out)
        self.assertNotIn("Parked experiment", out)          # hidden

    def test_stale_section_order(self):
        _, out, _ = self.run_cli("dash")
        section = out[out.index("+-- STALE"):out.index("+-- LIVE")]
        names = ["Slide archive", "Beta importer", "Quarterly zine", "Alpha web client"]
        positions = [section.index(name) for name in names]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("| 67d ", section)

    def test_every_line_fits_the_designed_width_and_boxes_are_aligned(self):
        _, out, _ = self.run_cli("dash")
        boxed = [line for line in out.split("\n") if line.startswith(("  |", "  +"))]
        self.assertGreater(len(boxed), 30)
        self.assertEqual({len(line) for line in boxed}, {projdash.DESIGN_WIDTH})
        self.assertLessEqual(max(len(line) for line in out.split("\n")), projdash.DESIGN_WIDTH)

    def test_narrower_layout_keeps_boxes_aligned(self):
        projects = projdash.Registry.parse(SAMPLE).projects
        lines = projdash.render_dashboard(projects, TODAY, projdash.Style(False), inner=70)
        boxed = [line for line in lines if line.startswith(("  |", "  +"))]
        self.assertEqual({len(line) for line in boxed}, {76})

    def test_narrow_terminal_is_detected(self):
        with mock.patch.object(projdash.shutil, "get_terminal_size",
                               return_value=os.terminal_size((76, 24))):
            self.assertEqual(projdash.inner_width(TtyIO()), 70)
        with mock.patch.object(projdash.shutil, "get_terminal_size",
                               return_value=os.terminal_size((200, 24))):
            self.assertEqual(projdash.inner_width(TtyIO()), 80)
        with mock.patch.object(projdash.shutil, "get_terminal_size",
                               return_value=os.terminal_size((20, 24))):
            self.assertEqual(projdash.inner_width(TtyIO()), projdash.MIN_INNER)
        self.assertEqual(projdash.inner_width(io.StringIO()), 80)

    def test_empty_registry_shows_empty_messages(self):
        with open(self.db, "w", encoding="utf-8") as handle:
            handle.write("# nothing yet\n")
        code, out, _ = self.run_cli("dash")
        self.assertEqual(code, 0)
        for text in ("no open high-priority project", "nothing in progress", "nothing stale",
                     "nothing live with a URL", "no research recorded", "(0 projects)"):
            self.assertIn(text, out)
        boxed = [line for line in out.split("\n") if line.startswith(("  |", "  +"))]
        self.assertEqual({len(line) for line in boxed}, {projdash.DESIGN_WIDTH})

    def test_dash_is_the_default_command(self):
        implicit = self.run_cli()[1].split("\n")
        explicit = self.run_cli("dash")[1].split("\n")
        self.assertIn("+-- DASHBOARD", implicit[1])
        self.assertEqual(implicit[2:], explicit[2:])      # line 1 carries the clock


class Colours(Base):
    COMMANDS = (["dash"], ["list"], ["next"], ["show", "beta"], ["stale"], ["research"],
                ["md"], ["touch", "beta"], ["set", "beta", "progress", "40"])

    def test_no_escape_codes_when_not_a_tty(self):
        for argv in self.COMMANDS:
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 0)
                self.assertNotIn("\033", out + err)

    def test_escape_codes_on_a_tty(self):
        _, out, _ = self.run_cli("list", stdout=TtyIO())
        self.assertIn("\033[", out)

    def test_no_color_env_wins_on_a_tty(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            for argv in self.COMMANDS:
                with self.subTest(argv=argv):
                    _, out, _ = self.run_cli(*argv, stdout=TtyIO())
                    self.assertNotIn("\033", out)

    def test_no_color_flag_wins_on_a_tty(self):
        for argv in (["--no-color", "dash"], ["list", "--no-color"]):
            _, out, _ = self.run_cli(*argv, stdout=TtyIO())
            self.assertNotIn("\033", out)

    def test_coloured_dashboard_keeps_its_alignment(self):
        projects = projdash.Registry.parse(SAMPLE).projects
        plain = projdash.render_dashboard(projects, TODAY, projdash.Style(False))
        coloured = projdash.render_dashboard(projects, TODAY, projdash.Style(True))
        import re
        stripped = [re.sub(r"\033\[[0-9;]*m", "", line) for line in coloured]
        self.assertEqual(stripped, plain)


class ReadOnlyCommands(Base):
    def test_list(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("8 projects - 2 active - 1 live - 1 done", out)
        self.assertIn("[hidden from dashboard]", out)
        order = [out.index(i) for i in ("alpha-web ", "alpha-api ", "beta ", "shop ", "study ",
                                        "parked ", "archive ", "zine ")]
        self.assertEqual(order, sorted(order))
        self.assertIn("!! alpha-web        blocked   ####......  45%  Alpha web client", out)

    def test_list_filter_and_shortcut(self):
        code, out, _ = self.run_cli("list", "ALPHA")
        self.assertEqual(code, 0)
        self.assertIn("2 projects - 1 active", out)
        self.assertNotIn("beta", out)
        self.assertEqual(self.run_cli("alpha")[1], self.run_cli("list", "alpha")[1])
        self.assertEqual(self.run_cli("ls", "alpha")[1], self.run_cli("list", "alpha")[1])

    def test_list_filter_without_matches_is_an_error(self):
        code, out, err = self.run_cli("list", "zzz")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no project matches 'zzz'", err)

    def test_next(self):
        code, out, _ = self.run_cli("next")
        self.assertEqual(code, 0)
        self.assertLess(out.index("1. Alpha API"), out.index("2. Alpha web client"))
        self.assertIn("[no next step defined]", out)     # zine
        self.assertNotIn("Queue libraries compared", out)  # done

    def test_show(self):
        code, out, _ = self.run_cli("show", "sh")
        self.assertEqual(code, 0)
        self.assertIn("Toy shop site (shop)", out)
        self.assertIn("status     : live   progress: 100%   priority: 2 (medium)", out)
        self.assertIn("updated    : 2026-11-12   (8 days ago)", out)
        self.assertIn("url        : https://shop.example.com", out)

    def test_show_lists_recent_files_of_the_folder(self):
        folder = os.path.join(self.dir, "proj")
        os.makedirs(os.path.join(folder, "node_modules"))
        for name, day in (("old.txt", 1), ("new.txt", 9),
                          (os.path.join("node_modules", "dep.js"), 15)):
            path = os.path.join(folder, name)
            open(path, "w").close()
            stamp = datetime.datetime(2026, 11, day, 12).timestamp()
            os.utime(path, (stamp, stamp))
        self.assertEqual(self.run_cli("set", "beta", "path", folder)[0], 0)
        _, out, _ = self.run_cli("show", "beta")
        self.assertIn("[exists]", out)
        self.assertLess(out.index("2026-11-09 new.txt"), out.index("2026-11-01 old.txt"))
        self.assertNotIn("dep.js", out)
        self.run_cli("set", "beta", "path", os.path.join(self.dir, "gone"))
        self.assertIn("[MISSING]", self.run_cli("show", "beta")[1])

    def test_stale(self):
        code, out, _ = self.run_cli("stale")
        self.assertEqual(code, 0)
        self.assertIn("MORE THAN 14 DAYS", out)
        self.assertLess(out.index("parked"), out.index("archive"))
        self.assertIn(" 67 days", out)
        self.assertNotIn("alpha-api", out)
        code, out, _ = self.run_cli("stale", "100")
        self.assertIn("parked", out)
        self.assertNotIn("archive", out)
        self.assertIn("nothing stale", self.run_cli("stale", "500")[1])

    def test_research(self):
        for argv in (["research"], ["research", "list"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0)
            self.assertIn("Queue libraries compared   (2026-10-05)", out)
            self.assertIn("-> Reuse the retry settings", out)
            self.assertIn("projdash open study", out)

    def test_help_and_version(self):
        code, out, _ = self.run_cli("help", db=False)
        self.assertEqual(code, 0)
        for word in ("dash", "list", "next", "show", "set", "touch", "done", "add", "hide",
                     "unhide", "stale", "research", "scan", "open", "md", "init"):
            self.assertIn(word, out)
        self.assertEqual(self.run_cli("--help", db=False)[0], 0)
        code, out, _ = self.run_cli("--version", db=False)
        self.assertEqual((code, out.strip()), (0, "projdash " + projdash.__version__))


class Markdown(Base):
    def test_report_on_stdout(self):
        code, out, _ = self.run_cli("md")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# Project status - 2026-11-20\n\n## blocked\n"))
        headings = [line for line in out.split("\n") if line.startswith("## ")]
        self.assertEqual(headings, ["## blocked", "## active", "## live", "## paused",
                                    "## idea", "## done"])
        self.assertIn("- **Alpha API** (60%, priority 1) - _Write the pagination tests_", out)
        self.assertIn("  - https://shop.example.com", out)
        self.assertIn("- **Quarterly zine** (0%, priority 3) - _next step to be defined_", out)
        self.assertIn("  - updated: 2026-11-18", out)
        self.assertLess(out.index("Alpha API"), out.index("Beta importer"))  # by priority

    def test_report_to_a_file(self):
        target = os.path.join(self.dir, "reports", "status.md")
        code, out, _ = self.run_cli("md", target)
        self.assertEqual(code, 0)
        self.assertIn(target, out)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.run_cli("md")[1])
        self.assertEqual(os.listdir(os.path.dirname(target)), ["status.md"])

    def test_report_open(self):
        target = os.path.join(self.dir, "status.md")
        with mock.patch.object(projdash, "open_folder") as opener:
            self.assertEqual(self.run_cli("md", target, "--open")[0], 0)
        opener.assert_called_once_with(target)
        self.assertEqual(self.run_cli("md", "--open")[0], 1)

    def test_unknown_statuses_are_not_dropped(self):
        registry = projdash.Registry.parse("a\tA\tt\twaiting\t5\t2\n")
        self.assertIn("## waiting", projdash.render_markdown(registry.projects, TODAY))


# ----------------------------------------------------------------- mutation

class Mutations(Base):
    def assert_untouched_except(self, *ids):
        """Comments, blank lines, order and every other row are unchanged."""
        before = SAMPLE.split("\n")
        after = self.read_db().split("\n")
        self.assertEqual(len(before), len(after))
        for old, new in zip(before, after):
            if old.split("\t")[0] in ids:
                self.assertEqual(old.split("\t")[0], new.split("\t")[0])
            else:
                self.assertEqual(old, new)

    def test_set_each_field(self):
        cases = [("name", ["Beta", "loader"], 1, "Beta loader"),
                 ("category", ["web"], 2, "web"), ("cat", ["ops"], 2, "ops"),
                 ("status", ["paused"], 3, "paused"),
                 ("progress", ["70"], 4, "70"), ("%", ["15%"], 4, "15"),
                 ("priority", ["1"], 5, "1"), ("prio", ["low"], 5, "3"),
                 ("path", ["~/somewhere"], 6, "~/somewhere"),
                 ("url", ["https://beta.example.org"], 7, "https://beta.example.org"),
                 ("next", ["ship", "it"], 8, "ship it"),
                 ("notes", ["remember", "this"], 10, "remember this"),
                 ("note", ["again"], 10, "again"),
                 ("hidden", ["x"], 11, "x"), ("hidden", [], 11, "")]
        for field, words, column, expected in cases:
            with self.subTest(field=field):
                code, out, err = self.run_cli("set", "beta", field, *words)
                self.assertEqual((code, err), (0, ""))
                row = self.row("beta")
                self.assertEqual(len(row), 12)
                self.assertEqual(row[column], expected)
                self.assertEqual(row[9], "2026-11-20")
                self.assert_untouched_except("beta")
        self.assert_only_registry_left()

    def test_set_notes_goes_to_the_notes_column_not_the_date(self):
        self.run_cli("set", "shop", "note", "seasonal traffic")
        row = self.row("shop")
        self.assertEqual(row[10], "seasonal traffic")
        self.assertEqual(row[9], "2026-11-20")

    def test_set_updated_is_validated_and_kept(self):
        self.assertEqual(self.run_cli("set", "beta", "updated", "2026-11-02")[0], 0)
        self.assertEqual(self.row("beta")[9], "2026-11-02")

    def test_set_clears_a_field(self):
        self.assertEqual(self.run_cli("set", "shop", "url")[0], 0)
        self.assertEqual(self.row("shop")[7], "")

    def test_set_validation_errors_leave_the_file_alone(self):
        cases = [("status", "attivo"), ("progress", "101"), ("progress", "abc"),
                 ("priority", "9"), ("updated", "tomorrow"), ("hidden", "maybe"),
                 ("name", ""), ("notes", "a\tb"), ("next", "line\nbreak"),
                 ("colour", "red"), ("id", "other")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                code, out, err = self.run_cli("set", "beta", field, value)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("projdash: error: "))
                self.assertEqual(self.read_db(), SAMPLE)
        self.assert_only_registry_left()

    def test_ambiguous_and_missing_ids_change_nothing(self):
        for argv in (["set", "alpha", "progress", "10"], ["touch", "alpha"], ["done", "alpha"],
                     ["hide", "alpha"], ["unhide", "alpha"], ["show", "alpha"],
                     ["touch", "nope"], ["done", "nope"], ["show", "nope"], ["open", "nope"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertTrue("ambiguous" in err or "no project with id" in err)
                self.assertEqual(self.read_db(), SAMPLE)

    def test_touch(self):
        code, out, _ = self.run_cli("touch", "arch")
        self.assertEqual(code, 0)
        self.assertIn("archive", out)
        self.assertEqual(self.row("archive")[9], "2026-11-20")
        self.assert_untouched_except("archive")
        self.assert_only_registry_left()

    def test_done(self):
        self.assertEqual(self.run_cli("done", "beta")[0], 0)
        row = self.row("beta")
        self.assertEqual((row[3], row[4], row[9]), ("done", "100", "2026-11-20"))
        self.assert_untouched_except("beta")
        self.assertEqual(self.run_cli("close", "zine")[0], 0)
        self.assertEqual(self.row("zine")[3], "done")

    def test_hide_and_unhide(self):
        self.assertEqual(self.run_cli("hide", "shop")[0], 0)
        self.assertEqual(self.row("shop")[11], "x")
        self.assertEqual(self.row("shop")[9], "2026-11-12")     # date not touched
        self.assertNotIn("Toy shop site", self.run_cli("dash")[1])
        self.assertIn("Toy shop site", self.run_cli("list")[1])
        self.assertEqual(self.run_cli("unhide", "shop")[0], 0)
        self.assertEqual(self.row("shop")[11], "")
        self.assertIn("Toy shop site", self.run_cli("dash")[1])
        self.assert_untouched_except("shop")

    def test_add_with_flags(self):
        code, out, _ = self.run_cli(
            "add", "kiosk", "--name", "Museum kiosk", "--category", "web", "--status", "active",
            "--progress", "15", "--priority", "1", "--path", "~/code/kiosk",
            "--url", "https://kiosk.example.net", "--next", "Sketch the home screen",
            "--notes", "Touch screen only")
        self.assertEqual(code, 0)
        self.assertIn("kiosk", out)
        text = self.read_db()
        self.assertTrue(text.startswith(SAMPLE))                 # appended, nothing moved
        self.assertEqual(text[len(SAMPLE):], "\t".join([
            "kiosk", "Museum kiosk", "web", "active", "15", "1", "~/code/kiosk",
            "https://kiosk.example.net", "Sketch the home screen", "2026-11-20",
            "Touch screen only", ""]) + "\n")
        self.assert_only_registry_left()

    def test_add_defaults(self):
        self.assertEqual(self.run_cli("add", "--id", "bare")[0], 0)
        self.assertEqual(self.row("bare"), ["bare", "bare", "tool", "idea", "0", "2", "", "", "",
                                            "2026-11-20", "", ""])
        self.assertEqual(self.run_cli("new", "ghost", "--hidden")[0], 0)
        self.assertEqual(self.row("ghost")[11], "x")

    def test_add_interactive(self):
        answers = ["tracker", "Habit tracker", "", "active", "", "1", "", "", "Pick a name", ""]
        with mock.patch("builtins.input", side_effect=answers) as asked:
            code, out, _ = self.run_cli("add")
        self.assertEqual(code, 0)
        self.assertEqual(asked.call_count, 10)
        self.assertEqual(self.row("tracker"), [
            "tracker", "Habit tracker", "tool", "active", "0", "1", "", "", "Pick a name",
            "2026-11-20", "", ""])

    def test_add_interactive_asks_only_what_flags_did_not_give(self):
        with mock.patch("builtins.input", side_effect=["quick"] + [""] * 7) as asked:
            code, _, _ = self.run_cli("add", "--name", "Quick one", "--status", "paused")
        self.assertEqual(code, 0)
        self.assertEqual(asked.call_count, 8)
        self.assertEqual(self.row("quick")[1:4], ["Quick one", "tool", "paused"])

    def test_add_errors(self):
        cases = [["add", "beta"],                                 # duplicate id
                 ["add", "two words"],
                 ["add", "n1", "--status", "sleeping"],
                 ["add", "n2", "--progress", "150"],
                 ["add", "n3", "--priority", "7"],
                 ["add", "n4", "--notes", "tab\there"]]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("projdash: error:", err)
                self.assertEqual(self.read_db(), SAMPLE)
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.run_cli("add")[0], 1)          # no id typed
        self.assertEqual(self.read_db(), SAMPLE)

    def test_add_to_a_file_without_final_newline(self):
        with open(self.db, "w", encoding="utf-8") as handle:
            handle.write("a\tA\ttool\tidea\t0\t2\t\t\t\t2026-11-01\t\t")
        self.assertEqual(self.run_cli("add", "b")[0], 0)
        lines = self.read_db().split("\n")
        self.assertEqual([line.split("\t")[0] for line in lines], ["a", "b", ""])

    def test_crlf_registry_stays_crlf(self):
        with open(self.db, "w", encoding="utf-8", newline="") as handle:
            handle.write(SAMPLE.replace("\n", "\r\n"))
        self.assertEqual(self.run_cli("touch", "beta")[0], 0)
        text = self.read_db()
        self.assertEqual(text.count("\r\n"), SAMPLE.count("\n"))
        self.assertEqual(text.replace("\r\n", "\n").count("\r"), 0)


class AtomicWrite(Base):
    def test_no_temp_file_left_behind(self):
        for argv in (["set", "beta", "progress", "50"], ["touch", "beta"], ["done", "beta"],
                     ["hide", "beta"], ["unhide", "beta"], ["add", "fresh"], ["scan"]):
            self.assertEqual(self.run_cli(*argv)[0], 0)
            self.assert_only_registry_left()

    def test_failed_write_keeps_the_old_file_and_cleans_up(self):
        with mock.patch.object(projdash.os, "replace", side_effect=OSError("disk full")):
            code, _, err = self.run_cli("set", "beta", "progress", "50")
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertEqual(self.read_db(), SAMPLE)
        self.assert_only_registry_left()

    def test_atomic_write_replaces_content(self):
        target = os.path.join(self.dir, "file.txt")
        projdash.atomic_write(target, "one\n")
        projdash.atomic_write(target, "two\n")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "two\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["file.txt", "projects.tsv"])


# --------------------------------------------------------------------- scan

def make_file(path, year, month, day):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x")
    stamp = datetime.datetime(year, month, day, 12, 0).timestamp()
    os.utime(path, (stamp, stamp))


class Scan(Base):
    def setUp(self):
        super().setUp()
        work = os.path.join(self.dir, "work")
        self.fresh = os.path.join(work, "fresh")       # newest real file: 2026-11-10
        make_file(os.path.join(self.fresh, "main.py"), 2026, 11, 2)
        make_file(os.path.join(self.fresh, "src", "deep", "util.py"), 2026, 11, 10)
        for skipped in (".git", "node_modules", ".venv", "__pycache__"):
            make_file(os.path.join(self.fresh, skipped, "noise"), 2026, 11, 19)
        make_file(os.path.join(self.fresh, "src", "node_modules", "pkg", "x.js"), 2026, 11, 19)
        self.older = os.path.join(work, "older")       # folder older than the registry date
        make_file(os.path.join(self.older, "notes.txt"), 2026, 10, 1)
        self.empty = os.path.join(work, "empty")
        os.makedirs(self.empty)
        self.gone = os.path.join(work, "gone")
        rows = [
            ["fresh", "Fresh", "tool", "active", "10", "2", self.fresh, "", "", "2026-11-01", "", ""],
            ["older", "Older", "tool", "active", "10", "2", self.older, "", "", "2026-11-15", "", ""],
            ["empty", "Empty", "tool", "idea", "0", "3", self.empty, "", "", "2026-11-05", "", ""],
            ["gone", "Gone", "tool", "idea", "0", "3", self.gone, "", "", "2026-11-06", "", ""],
            ["nodate", "No date", "tool", "idea", "0", "3", self.older, "", "", "", "", ""],
            ["nopath", "No path", "tool", "idea", "0", "3", "", "", "", "2026-11-07", "", ""],
        ]
        self.text = "# scan fixture\n" + "".join("\t".join(r) + "\n" for r in rows)
        with open(self.db, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.text)

    def test_scan_updates_from_newest_file_and_skips_noise_dirs(self):
        code, out, _ = self.run_cli("scan")
        self.assertEqual(code, 0)
        self.assertEqual(self.row("fresh")[9], "2026-11-10")
        self.assertEqual(self.row("older")[9], "2026-11-15")    # never moved backwards
        self.assertEqual(self.row("empty")[9], "2026-11-05")
        self.assertEqual(self.row("gone")[9], "2026-11-06")
        self.assertEqual(self.row("nodate")[9], "2026-10-01")
        self.assertEqual(self.row("nopath")[9], "2026-11-07")
        self.assertIn("fresh -> 2026-11-10", out)
        self.assertIn("missing gone -> " + self.gone, out)
        self.assertIn("2 updated, 1 folders missing", out)
        self.assertTrue(self.read_db().startswith("# scan fixture\n"))
        self.assertEqual(sorted(os.listdir(self.dir)), ["projects.tsv", "work"])

    def test_scan_is_idempotent(self):
        self.run_cli("scan")
        first = self.read_db()
        code, out, _ = self.run_cli("scan")
        self.assertEqual(code, 0)
        self.assertIn("0 updated", out)
        self.assertEqual(self.read_db(), first)

    def test_newest_mtime(self):
        expected = datetime.datetime(2026, 11, 10, 12, 0).timestamp()
        self.assertEqual(projdash.newest_mtime(self.fresh), expected)
        self.assertIsNone(projdash.newest_mtime(self.empty))

    def test_dash_scans_once_a_day(self):
        marker = projdash.scan_marker_path(self.db)
        self.assertEqual(self.run_cli("dash")[0], 0)
        self.assertEqual(self.row("fresh")[9], "2026-11-10")
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "2026-11-20")
        # same day: a newer file is not picked up by the dashboard...
        make_file(os.path.join(self.fresh, "later.py"), 2026, 11, 17)
        self.assertEqual(self.run_cli("dash")[0], 0)
        self.assertEqual(self.row("fresh")[9], "2026-11-10")
        # ...but it is on the next day
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = projdash.main(["--db", self.db], today=datetime.date(2026, 11, 21))
        self.assertEqual(code, 0)
        self.assertEqual(self.row("fresh")[9], "2026-11-17")

    def test_dash_no_scan(self):
        self.assertEqual(self.run_cli("dash", "--no-scan")[0], 0)
        self.assertEqual(self.read_db(), self.text)
        self.assertFalse(os.path.exists(projdash.scan_marker_path(self.db)))


# --------------------------------------------------------------------- open

class Open(Base):
    def test_open_folder_and_url(self):
        folder = os.path.join(self.dir, "shop-src")
        os.makedirs(folder)
        self.run_cli("set", "shop", "path", folder)
        with mock.patch.object(projdash, "open_folder") as folders, \
                mock.patch.object(projdash, "open_url") as urls:
            code, _, _ = self.run_cli("open", "shop")
        self.assertEqual(code, 0)
        folders.assert_called_once_with(folder)
        urls.assert_called_once_with("https://shop.example.com")

    def test_open_only_http_urls_and_existing_folders(self):
        self.run_cli("set", "beta", "url", "file:///etc/hosts")
        self.run_cli("set", "beta", "path", os.path.join(self.dir, "missing"))
        with mock.patch.object(projdash, "open_folder") as folders, \
                mock.patch.object(projdash, "open_url") as urls:
            code, _, err = self.run_cli("open", "beta")
        self.assertEqual(code, 1)
        folders.assert_not_called()
        urls.assert_not_called()
        self.assertIn("nothing to open", err)

    def test_open_without_id_lists_candidates(self):
        with mock.patch.object(projdash, "open_url") as urls:
            code, out, err = self.run_cli("open")
        self.assertEqual(code, 1)
        urls.assert_not_called()
        self.assertIn("shop", err)
        self.assertNotIn("zine", err)

    def test_platform_opener(self):
        with mock.patch.object(projdash.sys, "platform", "linux"), \
                mock.patch.object(projdash.subprocess, "run") as run:
            projdash.open_folder("/some/dir")
        self.assertEqual(run.call_args[0][0], ["xdg-open", "/some/dir"])
        with mock.patch.object(projdash.sys, "platform", "darwin"), \
                mock.patch.object(projdash.subprocess, "run") as run:
            projdash.open_folder("/some/dir")
        self.assertEqual(run.call_args[0][0], ["open", "/some/dir"])
        with mock.patch.object(projdash.sys, "platform", "win32"), \
                mock.patch.object(projdash.os, "startfile", create=True) as startfile:
            projdash.open_folder("C:\\some\\dir")
        startfile.assert_called_once_with("C:\\some\\dir")
        with mock.patch.object(projdash.sys, "platform", "linux"), \
                mock.patch.object(projdash.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(projdash.ProjdashError):
                projdash.open_folder("/some/dir")


# ------------------------------------------------------- init, registry path

class InitAndRegistryLocation(Base):
    def test_init_creates_a_commented_header(self):
        target = os.path.join(self.dir, "nested", "dir", "reg.tsv")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = projdash.main(["--db", target, "init"], today=TODAY)
        self.assertEqual(code, 0)
        self.assertIn(target, out.getvalue())
        with open(target, encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(all(line.startswith("#") for line in text.splitlines()))
        for field in projdash.FIELDS:
            self.assertIn(field, text)
        for status in projdash.STATUSES:
            self.assertIn(status, text)
        self.assertEqual(projdash.Registry.parse(text).projects, [])
        self.assertEqual(os.listdir(os.path.dirname(target)), ["reg.tsv"])

        # a second init refuses to overwrite
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(projdash.main(["--db", target, "init"], today=TODAY), 1)
        self.assertIn("already exists", err.getvalue())

        # and the new registry is immediately usable
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(projdash.main(["--db", target, "add", "first"], today=TODAY), 0)
            self.assertEqual(projdash.main(["--db", target, "dash"], today=TODAY), 0)
        with open(target, encoding="utf-8") as handle:
            after = handle.read()
        self.assertTrue(after.startswith(text))
        self.assertIn("\nfirst\tfirst\ttool\tidea\t0\t2\t", after)

    def test_init_uses_xdg_config_home_by_default(self):
        code, out, _ = self.run_cli("init", db=False)
        self.assertEqual(code, 0)
        expected = os.path.join(self.dir, "config", "projdash", "projects.tsv")
        self.assertTrue(os.path.isfile(expected))
        self.assertEqual(self.run_cli("list", db=False)[0], 0)

    def test_env_variable_selects_the_registry(self):
        with mock.patch.dict(os.environ, {"PROJDASH_DB": self.db}):
            code, out, _ = self.run_cli("list", db=False)
        self.assertEqual(code, 0)
        self.assertIn("alpha-api", out)

    def test_db_flag_beats_env_and_works_after_the_command(self):
        with mock.patch.dict(os.environ, {"PROJDASH_DB": os.path.join(self.dir, "none.tsv")}):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = projdash.main(["list", "--db", self.db], today=TODAY)
        self.assertEqual(code, 0)
        self.assertIn("alpha-api", out.getvalue())

    def test_missing_registry_suggests_init(self):
        os.remove(self.db)
        for argv in (["dash"], ["list"], ["next"], ["show", "x"], ["set", "x", "name", "y"],
                     ["touch", "x"], ["done", "x"], ["add", "x"], ["hide", "x"],
                     ["unhide", "x"], ["stale"], ["research"], ["scan"], ["open", "x"],
                     ["md"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("registry not found", err)
                self.assertIn("projdash init", err)
        self.assertEqual(os.listdir(self.dir), [])

    def test_today_from_environment(self):
        with mock.patch.dict(os.environ, {"PROJDASH_TODAY": "2026-12-25"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(projdash.main(["--db", self.db, "list"]), 0)
            self.assertIn("2026-12-25", out.getvalue())
        with mock.patch.dict(os.environ, {"PROJDASH_TODAY": "christmas"}):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(projdash.main(["--db", self.db, "list"]), 1)


# --------------------------------------------------------------- exit codes

class ExitCodes(Base):
    def test_success_codes(self):
        with mock.patch.object(projdash, "open_folder"), \
                mock.patch.object(projdash, "open_url"):
            for argv in ([], ["dash"], ["today"], ["list"], ["ls"], ["list", "alpha"], ["next"],
                         ["now"], ["show", "beta"], ["s", "beta"], ["stale"], ["stale", "30"],
                         ["research"], ["scan"], ["md"], ["report"], ["open", "shop"],
                         ["o", "shop"], ["set", "beta", "progress", "40"], ["touch", "beta"],
                         ["t", "beta"], ["hide", "beta"], ["unhide", "beta"], ["add", "extra"],
                         ["done", "beta"], ["help"], ["-v", "list"]):
                with self.subTest(argv=argv):
                    self.assertEqual(self.run_cli(*argv)[0], 0)

    def test_error_codes(self):
        for argv in (["show", "nope"], ["show", "alpha"], ["set", "beta", "status", "bogus"],
                     ["set", "beta", "bogus", "1"], ["touch", "nope"], ["done", "nope"],
                     ["hide", "nope"], ["unhide", "nope"], ["add", "beta"], ["open"],
                     ["open", "zine"], ["list", "zzz"], ["zzz"], ["stale", "-3"], ["init"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertNotEqual(err, "")

    def test_usage_errors(self):
        for argv in (["show"], ["set", "beta"], ["touch"], ["done"], ["hide"], ["unhide"],
                     ["stale", "soon"], ["research", "everything"], ["--bogus-flag"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)

    def test_verbose_logs_to_stderr_without_field_values(self):
        code, out, err = self.run_cli("-v", "set", "beta", "notes", "private remark")
        self.assertEqual(code, 0)
        self.assertIn("DEBUG", err)
        self.assertNotIn("private remark", err)
        self.assertNotIn("DEBUG", out)

    def test_runs_as_a_script(self):
        import subprocess
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "projdash.py")
        env = dict(os.environ, PROJDASH_TODAY="2026-11-20")
        result = subprocess.run([sys.executable, script, "--db", self.db, "dash", "--no-scan"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TODAY'S FOCUS", result.stdout)
        self.assertNotIn("\033", result.stdout)
        result = subprocess.run([sys.executable, script, "--db", self.db + ".none"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("projdash init", result.stderr)


if __name__ == "__main__":
    unittest.main()
