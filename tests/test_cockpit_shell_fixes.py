"""Tests for the shell / tmux review fixes: help card wrapping and scrolling, header guards and
fixed widths, 8-color clashes, small panes, the triage target, and the launcher mouse restore.
Synthetic data only."""
from __future__ import annotations

import curses
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from telemachus.cockpit import finance_view, launcher as L, shell, theme
from telemachus.cockpit.telemetry import CockpitTelemetry, HeaderInfo, _PollResult, _float_or_none, _int_or_none
from telemachus.cockpit.tui import TRIAGE_PROMPT, CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:
    from cockpit_isolation import isolate_module
setUpModule, tearDownModule = isolate_module()


def text(line) -> str:
    return theme.line_text(line)


def card_words(ov: shell.Overlay) -> str:
    """Every word inside the help card as one string: the left column first, then the right one
    (the card has two columns when its inside is at least 56 cells)."""
    inner = ov.width - 2
    rows = [text(line)[1:-1] for line in ov.lines[1:-1]]
    cols = [rows] if inner < 56 else [[r[: inner // 2] for r in rows], [r[inner // 2:] for r in rows]]
    return " ".join(w for col in cols for r in col for w in r.split())


def all_help_words(groups, width, body_height) -> str:
    """Words of the help card at every scroll position (what a user can reach with j/k)."""
    ov = shell.build_help(groups, width, shell.BODY_TOP, body_height)
    seen = [card_words(ov)]
    for scroll in range(1, ov.scroll_max + 1):
        seen.append(card_words(shell.build_help(groups, width, shell.BODY_TOP, body_height, scroll=scroll)))
    return " ".join(seen)


class TestHelpCard(unittest.TestCase):
    def test_long_texts_wrap_at_95_and_70_columns(self):
        groups = [shell.KeyGroup.of(
            "INBOX",
            shell.KeyHint("p", "preview", "longer preview of the selected email"),
            shell.KeyHint("t", "triage", "triage email in OpenCode (right pane)"),
            shell.KeyHint("j/k", "move", "move through the inbox list"),
        ), shell.KeyGroup.of("GUARD", shell.KeyHint("o", "trash", "open the Trash in Finder"))]
        for width in (94, 69):
            ov = shell.build_help(groups, width, shell.BODY_TOP, 40)
            joined = "\n".join(text(line) for line in ov.lines)
            self.assertNotIn("…", joined, width)
            words = card_words(ov)
            for hint in ("longer preview of the selected email", "triage email in OpenCode (right pane)",
                         "move through the inbox list", "open the Trash in Finder"):
                self.assertIn(hint, words, (width, hint))
            self.assertTrue(all(theme.line_width(line) == ov.width for line in ov.lines))
            self.assertEqual(ov.scroll_max, 0)

    def test_top_border_is_full_width(self):
        ov = shell.build_help([], 94, shell.BODY_TOP, 40)
        self.assertTrue(text(ov.lines[0]).endswith("─╮"))

    def test_short_pane_scrolls_instead_of_dropping_rows(self):
        # 95 x 20: the Day screen help used to lose its last rows without a sign.
        keys = finance_view.money_keys("day", False, 94)
        hints = [h.help_text for g in keys for h in g.hints]
        lay = shell.layout(20, 95)
        ov = shell.build_help(keys, lay.width, lay.body_top, lay.body_height)
        last_inner = text(ov.lines[-2])
        if ov.scroll_max:
            self.assertIn("more", last_inner)
            self.assertIn("j/k", last_inner)
        self.assertLessEqual(ov.top + len(ov.lines), lay.body_top + lay.body_height)
        reachable = all_help_words(keys, lay.width, lay.body_height)
        for h in hints:
            self.assertIn(" ".join(h.split()), reachable, h)

    def test_tiny_body_still_reaches_every_row(self):
        groups = [shell.KeyGroup.of("G", *[shell.KeyHint(str(i), f"k{i}", f"key number {i} help") for i in range(10)])]
        ov = shell.build_help(groups, 60, shell.BODY_TOP, 6)
        self.assertGreater(ov.scroll_max, 0)
        self.assertLessEqual(ov.top + len(ov.lines), shell.BODY_TOP + 6)
        reachable = all_help_words(groups, 60, 6)
        for i in range(10):
            self.assertIn(f"key number {i} help", reachable)
        clamped = shell.build_help(groups, 60, shell.BODY_TOP, 6, scroll=999)
        self.assertEqual(clamped.scroll, clamped.scroll_max)


class TestHeader(unittest.TestCase):
    def header(self, info: HeaderInfo, width: int = 94) -> str:
        return text(shell.build_header(info, width, 0.0, "14:32"))

    def test_no_model_is_not_green_ready(self):
        info = HeaderInfo(lm="no_model", ram_used_gb=19.9, ram_total_gb=24)
        for width in (94, 69):
            line = shell.build_header(info, width, 0.0, "14:32")
            self.assertNotIn("ready", text(line))
            self.assertIn("no model loaded", text(line))
            self.assertFalse(any(style.startswith("green") for _, style in line))

    def test_ctx_does_not_move_while_tokens_grow(self):
        cols = set()
        for tokens in (5, 63, 154, 1234, 45678, 999_999, 12_000_000):
            for tps in (3.0, 44.0, 120.0):
                out = self.header(HeaderInfo(lm="writing", model="qwen3.5-9b", tokens=tokens, tps=tps,
                                             ctx_used=22, ctx_max=100, ram_used_gb=7.1, ram_total_gb=24))
                cols.add(out.index("ctx"))
        self.assertEqual(len(cols), 1, cols)
        reading = self.header(HeaderInfo(lm="reading", model="qwen3.5-9b", elapsed_s=12,
                                         ctx_used=22, ctx_max=100, ram_used_gb=7.1, ram_total_gb=24))
        self.assertEqual(reading.index("ctx"), cols.pop())

    def test_padding_never_costs_a_field(self):
        for width in (50, 60, 70, 80, 94):
            for tokens in (5, 1234, 154_000):
                info = HeaderInfo(lm="writing", model="qwen3.5-9b", tokens=tokens, tps=3.0, ctx_used=22,
                                  ctx_max=100, ram_used_gb=7.1, ram_total_gb=24)
                padded = self.header(info, width)
                with patch.object(shell, "ACTIVE_TEXT_W", 0):
                    plain = self.header(info, width)
                # The padding may shorten the model name (to a fixed width), never a field or the clock.
                for word in ("ctx", "ram", "14:32"):
                    self.assertEqual(word in padded, word in plain, (width, tokens, word))
                if "qwen" not in padded:  # the name only goes to keep a field that fit beside "ready"
                    self.assertIn("ctx", padded, (width, tokens))

    def test_huge_numbers_are_clamped(self):
        self.assertEqual(shell._tok(999), "999")
        self.assertEqual(shell._tok(1234), "1.2k")
        self.assertEqual(shell._tok(45678), "46k")
        self.assertEqual(shell._tok(999_999), "1.0M")
        self.assertEqual(shell._tok(10 ** 15), "999M+")
        self.assertTrue(all(len(shell._tok(n)) <= 5 for n in (0, 9, 999, 9949, 9950, 999_499, 999_500, 10 ** 9)))
        out = self.header(HeaderInfo(lm="writing", tokens=10 ** 15, tps=1e9))
        self.assertIn("999M+ tok", out)
        self.assertIn(">999 t/s", out)


class TestTelemetryGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status = Path(self.tmp.name) / "llm_status.json"
        self.t = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models", status_path=self.status,
                                  opencode_db=Path(self.tmp.name) / "none.db")
        self.t._poll = _PollResult(reachable=True, models=["m"], polled_at=time.time())
        self.n = 0

    def write(self, raw: str):
        self.status.write_text(raw, encoding="utf-8")
        self.n += 1
        stamp = time.time() + self.n  # new mtime each time, so the cache re-reads
        os.utime(self.status, (stamp, stamp))
        return stamp

    def test_bad_start_time_never_raises(self):
        for bad in ('"x"', "[1, 2]", '{"a": 1}', "true", "-5", "1e999", '"2026-10-28T10:00:00"', "null"):
            now = self.write('{"status": "processing_prompt", "start_time": %s}' % bad)
            info = self.t.header_info(now)
            self.assertEqual(info.lm, "reading", bad)
            self.assertIsNone(info.elapsed_s, bad)
            shell.build_header(info, 94, now, "14:32")

    def test_bad_counts_are_hidden(self):
        now = self.write(json.dumps({"status": "producing_tokens", "tokens_produced": -3, "speed_tps": -1}))
        info = self.t.header_info(now)
        self.assertEqual((info.lm, info.tokens, info.tps), ("writing", None, None))
        now = self.write('{"status": "producing_tokens", "tokens_produced": Infinity, "speed_tps": Infinity}')
        info = self.t.header_info(now)
        self.assertEqual((info.tokens, info.tps), (None, None))
        self.assertNotIn("inf", text(shell.build_header(info, 94, now, "")))
        self.t.format_single_line_header()

    def test_number_helpers(self):
        self.assertIsNone(_int_or_none(True))
        self.assertIsNone(_int_or_none(float("inf")))
        self.assertEqual(_int_or_none("12"), 12)
        self.assertIsNone(_float_or_none(float("nan")))
        self.assertIsNone(_float_or_none(float("inf")))
        self.assertEqual(_float_or_none("4.5"), 4.5)


class TestEightColors(unittest.TestCase):
    def test_selected_row_never_hides_its_bar(self):
        t = theme.Theme()
        t._setup_basic(8)
        t._default_bg = -1
        for fg in theme.COLOR_NAMES + ("ink",):
            for bg in ("sel", "card", "panel", "cyan", ""):
                f, b = t.color_numbers(fg, bg)
                if b != -1:
                    self.assertNotEqual(f, b, (fg, bg))
        self.assertEqual(t.color_numbers("blue", "sel"), (curses.COLOR_CYAN, curses.COLOR_BLUE))
        self.assertEqual(t.color_numbers("blue", ""), (curses.COLOR_BLUE, -1))


class TestSmallPane(unittest.TestCase):
    def test_too_small_message_wraps(self):
        for width in (8, 20, 28):
            lines = shell.too_small(width, 9)
            self.assertTrue(all(theme.line_width(line) == width for line in lines))
            self.assertEqual(" ".join(" ".join(text(line).split()) for line in lines), shell.TOO_SMALL_TEXT)

    def test_tab_names_stay_whole_on_narrow_panes(self):
        for width in (29, 33, 39, 94):
            row, under, spans = shell.build_tabs("guard", width)
            out = text(row)
            for name in ("Home", "Money", "Memory", "Guard"):
                self.assertIn(name, out, (width, out))
            self.assertEqual(theme.line_width(under), width)
            self.assertEqual([t for _, _, t in spans], list(shell.TAB_IDS))
            x = out.index("Guard")
            self.assertEqual(shell.tab_at(spans, x), "guard")
            self.assertEqual(text(under)[x:x + 5], "━━━━━")
        self.assertIn("1 Home    2 Money", text(shell.build_tabs("home", 94)[0]))


class TestTriageTarget(unittest.TestCase):
    def test_pane_rules(self):
        pane = CockpitTUI.opencode_pane
        self.assertIsNone(pane({})[0])
        self.assertIsNone(pane({"TMUX_PANE_RIGHT": "%3"})[0])  # not inside tmux
        self.assertIsNone(pane({"TMUX": "/s,1,0"})[0])  # dashboard mode: no launcher pane
        self.assertIsNone(pane({"TMUX": "/s,1,0", "TMUX_PANE_RIGHT": "%3", "TMUX_PANE": "%3"})[0])  # itself
        self.assertIsNone(pane({"TMUX": "/s,1,0", "TMUX_PANE_RIGHT": "{bottom-right}"})[0])  # guessed
        self.assertEqual(pane({"TMUX": "/s,1,0", "TMUX_PANE_RIGHT": "%3", "TMUX_PANE": "%4"})[0], "%3")

    def test_text_depends_on_what_runs_in_the_pane(self):
        t = CockpitTUI.opencode_text
        run_cmd = f"opencode run '{TRIAGE_PROMPT}'"
        self.assertEqual(t(TRIAGE_PROMPT, "opencode"), TRIAGE_PROMPT)
        self.assertEqual(t(TRIAGE_PROMPT, "zsh", ["/bin/zsh -l"]), run_cmd)
        self.assertEqual(t(TRIAGE_PROMPT, "-zsh", ["-zsh"]), run_cmd)
        self.assertEqual(t("it's", "bash"), "opencode run 'it'\"'\"'s'")
        # The launcher's pane: `zsh -c "...; opencode; exec zsh -l"` has no job control, so tmux
        # reports zsh while the OpenCode TUI runs. The foreground process list shows it.
        launcher_fg = ["zsh -c printf 'hi'; opencode; exec \"${SHELL:-/bin/zsh}\" -l", "opencode"]
        self.assertEqual(t(TRIAGE_PROMPT, "zsh", launcher_fg), TRIAGE_PROMPT)
        self.assertEqual(t(TRIAGE_PROMPT, "zsh", ["zsh -c x", "/opt/homebrew/bin/opencode --port 0"]), TRIAGE_PROMPT)
        self.assertIsNone(t(TRIAGE_PROMPT, "zsh", ["zsh -c x", "vim notes.txt"]))  # something else runs
        self.assertIsNone(t(TRIAGE_PROMPT, "vim", ["vim"]))
        self.assertIsNone(t(TRIAGE_PROMPT, ""))

    def make_tui(self):
        tui = CockpitTUI.__new__(CockpitTUI)
        tui.notification = ""
        return tui

    def test_without_launcher_nothing_is_typed(self):
        tui = self.make_tui()
        with patch.dict("os.environ", {"TMUX": "/s,1,0", "TMUX_PANE": "%0"}, clear=True), \
                patch("subprocess.run") as run:
            os.environ.pop("TMUX_PANE_RIGHT", None)
            tui.trigger_opencode_action(TRIAGE_PROMPT)
        run.assert_not_called()
        self.assertEqual(tui.notification_kind, "warn")

    def test_types_the_prompt_literally_into_the_opencode_tui(self):
        tui = self.make_tui()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = {"tmux": "/dev/ttys009 zsh\n", "ps": "Ss+  zsh -c x; opencode\nS+   opencode\nS    sleep 9\n"}
            return subprocess.CompletedProcess(argv, 0, out.get(argv[0], "") if argv[1] != "send-keys" else "", "")

        with patch.dict("os.environ", {"TMUX": "/s,1,0", "TMUX_PANE": "%1", "TMUX_PANE_RIGHT": "%0"}), \
                patch("subprocess.run", run):
            tui.trigger_opencode_action(TRIAGE_PROMPT)
        self.assertEqual(calls[1][:3], ["ps", "-t", "ttys009"])
        self.assertEqual(calls[2], ["tmux", "send-keys", "-t", "%0", "-l", TRIAGE_PROMPT])
        self.assertEqual(calls[3], ["tmux", "send-keys", "-t", "%0", "Enter"])
        self.assertEqual(tui.notification_kind, "ok")

    def test_busy_pane_gets_nothing(self):
        tui = self.make_tui()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = "/dev/ttys009 vim\n" if argv[0] == "tmux" else "S+   vim notes\n"
            return subprocess.CompletedProcess(argv, 0, out, "")

        with patch.dict("os.environ", {"TMUX": "/s,1,0", "TMUX_PANE": "%1", "TMUX_PANE_RIGHT": "%0"}), \
                patch("subprocess.run", run):
            tui.trigger_opencode_action(TRIAGE_PROMPT)
        self.assertNotIn("send-keys", [a for c in calls for a in c])
        self.assertIn("busy", tui.notification)


class TestHelpInTheTui(unittest.TestCase):
    @patch("curses.color_pair", return_value=0)
    @patch("curses.curs_set")
    def setUp(self, *_):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (20, 95)
        with patch.object(CockpitTUI, "refresh_data"), patch.object(CockpitTUI, "_init_curses"):
            self.tui = CockpitTUI(self.stdscr)
        self.tui.telemetry = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models",
                                              status_path=Path(self.tmp.name) / "s.json",
                                              opencode_db=Path(self.tmp.name) / "none.db")
        self.puts = []
        real_put = self.tui._put

        def put(y, x, line, width):
            self.puts.append((y, x, list(line)))
            real_put(y, x, line, width)

        self.tui._put = put

    def test_help_dims_the_screen_behind_it(self):
        self.tui.current_tab = "money"
        self.tui.handle_key(ord("?"))
        self.tui.render()
        lay = shell.layout(20, 95)
        body = [(y, line) for y, x, line in self.puts if x == 0 and lay.body_top <= y < lay.body_top + lay.body_height]
        self.assertTrue(body)
        for _, line in body:
            self.assertTrue(all(style == "dim" for _, style in line), line)

    def test_j_k_scroll_a_tall_help_card(self):
        self.tui.current_tab = "money"
        self.tui._set_finance_mode("day")
        self.tui.handle_key(ord("?"))
        self.tui.render()
        if not self.tui._help_scroll_max:
            self.skipTest("help fits on this screen")
        groups, with_global = self.tui._footer_groups()
        self.assertIn("j/k scroll", text(shell.build_footer(groups, 94, with_global)))
        for _ in range(50):
            self.tui.handle_key(ord("j"))
        self.tui.render()
        self.assertEqual(self.tui.help_scroll, self.tui._help_scroll_max)
        self.tui.handle_key(ord("k"))
        self.assertEqual(self.tui.help_scroll, self.tui._help_scroll_max - 1)
        self.tui.handle_key(27)
        self.assertFalse(self.tui.help_open)
        self.assertEqual(self.tui.help_scroll, 0)

    def test_too_small_screen_draws_the_whole_message(self):
        self.stdscr.getmaxyx.return_value = (9, 29)
        self.tui.render()
        drawn = " ".join(" ".join(text(line).split()) for _, _, line in self.puts)
        self.assertEqual(drawn, shell.TOO_SMALL_TEXT)


class TestLauncherMouse(unittest.TestCase):
    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict("os.environ", {"TELEMACHUS_COCKPIT_SETTINGS": str(Path(tmp.name) / "c.json")}):
            return L.CockpitLauncher()

    def test_mouse_already_on_is_left_alone(self):
        cmds = self.make().mouse_commands("$3", "@7", True, False)
        self.assertEqual(cmds, [["set-option", "-w", "-t", "@7", L.OPT_COCKPIT_WINDOW, "1"]])

    def test_mouse_off_is_turned_on_and_given_back(self):
        cmds = self.make().mouse_commands("$3", "@7", False, False)
        self.assertIn(["set-option", "-t", "$3", "mouse", "on"], cmds)
        hook = next(c for c in cmds if c[0] == "set-hook")
        self.assertEqual(hook[:4], ["set-hook", "-t", "$3", L.MOUSE_HOOK])
        self.assertIn("set-option -u -t $3 mouse", hook[4])
        self.assertIn(f"set-hook -u -t $3 {L.MOUSE_HOOK}", hook[4])
        local = self.make().mouse_commands("$3", "@7", False, True)
        self.assertIn("set-option -t $3 mouse off", next(c for c in local if c[0] == "set-hook")[4])
        for c in cmds:
            self.assertNotIn("-g", c)

    @unittest.skipUnless(os.path.exists("/opt/homebrew/bin/tmux") or os.path.exists("/usr/local/bin/tmux")
                         or os.path.exists("/usr/bin/tmux"), "needs tmux")
    def test_real_tmux_restores_mouse_when_the_cockpit_window_closes(self):
        lch = self.make()
        sock = f"tmcap-unittest-{os.getpid()}"
        base = [lch.tmux_bin, "-L", sock, "-f", "/dev/null"]

        def tm(*args):
            return subprocess.run(base + list(args), capture_output=True, text=True, timeout=5)

        try:
            tm("new-session", "-d", "-s", "u", "-x", "80", "-y", "20")
            sid = tm("display-message", "-p", "-t", "u", "#{session_id}").stdout.strip()
            wid = tm("new-window", "-t", "u", "-P", "-F", "#{window_id}").stdout.strip()
            for cmd in lch.mouse_commands(sid, wid, False, False):
                self.assertEqual(tm(*cmd).returncode, 0, cmd)
            self.assertEqual(tm("display-message", "-p", "-t", "u", "#{mouse}").stdout.strip(), "1")
            other = tm("new-window", "-t", "u", "-P", "-F", "#{window_id}").stdout.strip()
            tm("kill-window", "-t", other)
            self.assertEqual(tm("display-message", "-p", "-t", "u", "#{mouse}").stdout.strip(), "1")
            tm("kill-window", "-t", wid)
            deadline = time.time() + 3
            while time.time() < deadline and tm("show-options", "-q", "-t", "u", "mouse").stdout.strip():
                time.sleep(0.05)
            self.assertEqual(tm("show-options", "-q", "-t", "u", "mouse").stdout.strip(), "")
            self.assertNotIn(L.MOUSE_HOOK, tm("show-hooks", "-t", "u").stdout)
        finally:
            tm("kill-server")

    def test_inside_tmux_launch_reads_the_mouse_state_first(self):
        calls = []

        def run(argv, **kw):
            calls.append(list(argv))
            if "new-window" in argv:
                return subprocess.CompletedProcess(argv, 0, "%0 @0 $0\n", "")
            if "split-window" in argv:
                return subprocess.CompletedProcess(argv, 0, "%1\n", "")
            if "#{mouse}" in argv:
                return subprocess.CompletedProcess(argv, 0, "1\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        lch = self.make()
        with patch.dict("os.environ", {"TMUX": "/tmp/x,1,0"}), patch("subprocess.run", run), \
                patch.object(lch, "is_tmux_available", return_value=True), redirect_stdout(io.StringIO()):
            self.assertEqual(lch.launch(attach=False), 0)
        self.assertNotIn(["set-option", "-t", "$0", "mouse", "on"], [c[1:] for c in calls])
        self.assertIn(["set-option", "-w", "-t", "@0", L.OPT_COCKPIT_WINDOW, "1"], [c[1:] for c in calls])


if __name__ == "__main__":
    unittest.main()
