"""Launcher, tmux helper and CLI flag tests. No real tmux server is started."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telemachus.cockpit import __main__ as cli
from telemachus.cockpit import launcher as L
from telemachus.cockpit import tmux_helper as H

REPO = Path(__file__).resolve().parents[1]
EMOJI_RANGES = [(0x1F000, 0x1FAFF), (0x2600, 0x26FF), (0xFE0F, 0xFE0F)]


def has_emoji(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in EMOJI_RANGES)


def braces_balanced(fmt: str) -> bool:
    """Every #{ has its closing } (tmux formats nest)."""
    depth = 0
    i = 0
    while i < len(fmt):
        if fmt.startswith("#{", i):
            depth += 1
            i += 2
            continue
        if fmt[i] == "}" and depth:
            depth -= 1
        i += 1
    return depth == 0


class TempSettingsMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = self.tmp / "cfg" / "cockpit.json"
        env = {
            "HOME": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "PATH": os.environ.get("PATH", ""),
        }
        self._env = patch.dict("os.environ", env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def make(self, **kw):
        kw.setdefault("settings_path", self.settings)
        kw.setdefault("python", "/venv/bin/python")
        return L.CockpitLauncher(**kw)


# ---------------------------------------------------------------------------
# Settings file
# ---------------------------------------------------------------------------


class TestSettingsFile(TempSettingsMixin, unittest.TestCase):
    def test_defaults_when_missing(self):
        self.assertEqual(
            H.load_settings(self.settings),
            {"split_pct": H.DEFAULT_SPLIT_PCT, "stacked_pct": H.DEFAULT_STACKED_PCT, "stacked": False},
        )

    def test_default_path_uses_xdg_and_env_override(self):
        self.assertEqual(H.settings_path(), self.tmp / "xdg" / "telemachus" / "cockpit.json")
        with patch.dict("os.environ", {"TELEMACHUS_COCKPIT_SETTINGS": str(self.settings)}):
            self.assertEqual(H.settings_path(), self.settings)

    def test_broken_file_gives_defaults(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(H.load_settings(self.settings)["split_pct"], H.DEFAULT_SPLIT_PCT)
        self.settings.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(H.load_settings(self.settings)["stacked_pct"], H.DEFAULT_STACKED_PCT)

    def test_values_are_clamped(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"split_pct": 3, "stacked_pct": 99, "stacked": True}), encoding="utf-8")
        got = H.load_settings(self.settings)
        self.assertEqual((got["split_pct"], got["stacked_pct"], got["stacked"]), (H.MIN_PCT, H.MAX_PCT, True))

    def test_save_merges_and_keeps_other_keys(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"theme": "night", "split_pct": 40}), encoding="utf-8")
        self.assertTrue(H.save_settings({"split_pct": 55}, self.settings))
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data, {"theme": "night", "split_pct": 55})
        self.assertFalse(H.save_settings({"split_pct": 55}, self.settings))  # unchanged: no write
        self.assertEqual([p.name for p in self.settings.parent.iterdir()], ["cockpit.json"])  # no temp left

    def test_save_layout_round_trip(self):
        self.assertTrue(H.save_layout("1", "36", "62", self.settings))
        self.assertEqual(H.load_settings(self.settings), {"split_pct": 36, "stacked_pct": 62, "stacked": True})

    def test_save_layout_ignores_bad_values(self):
        H.save_layout("0", "40", "60", self.settings)
        self.assertFalse(H.save_layout("", "", "", self.settings))
        H.save_layout("maybe", "abc", "70", self.settings)
        self.assertEqual(H.load_settings(self.settings), {"split_pct": 40, "stacked_pct": 70, "stacked": False})

    def test_debounced_save_reads_tmux_and_checks_generation(self):
        def fake_run(argv, **kw):
            self.assertIn("-S", argv)
            self.assertIn("/tmp/sock", argv)
            self.assertEqual(kw.get("timeout"), H.TMUX_QUERY_TIMEOUT_S)
            return subprocess.CompletedProcess(argv, 0, stdout="7 0 33 58\n", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            self.assertFalse(H.save_layout_from_tmux("tmux", "/tmp/sock", "@1", "6", self.settings, delay=0))
            self.assertFalse(self.settings.exists())  # a newer layout change will save instead
            self.assertTrue(H.save_layout_from_tmux("tmux", "/tmp/sock", "@1", "7", self.settings, delay=0))
        self.assertEqual(H.load_settings(self.settings), {"split_pct": 33, "stacked_pct": 58, "stacked": False})

    def test_debounced_save_survives_dead_server(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 1)):
            self.assertFalse(H.save_layout_from_tmux("tmux", "/s", "@1", "1", self.settings, delay=0))
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "no server")):
            self.assertFalse(H.save_layout_from_tmux("tmux", "/s", "@1", "1", self.settings, delay=0))


# ---------------------------------------------------------------------------
# Layout choice and flags
# ---------------------------------------------------------------------------


class TestLayoutChoice(unittest.TestCase):
    def test_saved_values_are_used(self):
        lay = L.resolve_layout({"split_pct": 40, "stacked_pct": 60, "stacked": True})
        self.assertEqual((lay.stacked, lay.pct), (True, 60))

    def test_split_flag_applies_to_the_chosen_mode(self):
        lay = L.resolve_layout({"split_pct": 40, "stacked_pct": 60, "stacked": False}, split=55)
        self.assertEqual((lay.split_pct, lay.stacked_pct, lay.pct), (55, 60, 55))
        lay = L.resolve_layout({"split_pct": 40, "stacked_pct": 60, "stacked": False}, split=30, stacked=True)
        self.assertEqual((lay.split_pct, lay.stacked_pct, lay.pct), (40, 30, 30))

    def test_side_flag_overrides_saved_stacked(self):
        lay = L.resolve_layout({"stacked": True}, stacked=False)
        self.assertFalse(lay.stacked)
        self.assertEqual(lay.pct, H.DEFAULT_SPLIT_PCT)


class TestCliFlags(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(cli.normalize_argv(list(argv)))

    def test_no_subcommand_means_launch(self):
        self.assertEqual(cli.normalize_argv([]), ["launch"])
        self.assertEqual(cli.normalize_argv(["--split", "50"]), ["launch", "--split", "50"])
        self.assertEqual(cli.normalize_argv(["dashboard"]), ["dashboard"])
        self.assertEqual(cli.normalize_argv(["--help"]), ["--help"])

    def test_launch_flags(self):
        a = self.parse("--split", "35%", "--stacked", "--no-attach", "--socket", "tmx")
        self.assertEqual((a.split, a.stacked, a.no_attach, a.socket), (35, True, True, "tmx"))
        a = self.parse("launch", "--side")
        self.assertEqual((a.split, a.stacked, a.socket, a.session), (None, False, "telemachus", "telemachus-cockpit"))
        a = self.parse()
        self.assertIsNone(a.stacked)

    def test_bad_flags_are_rejected(self):
        with redirect_stderr_quiet():
            for argv in (["--split", "5"], ["--split", "x"], ["--stacked", "--side"]):
                with self.assertRaises(SystemExit):
                    self.parse(*argv)

    def test_launch_passes_flags_to_launcher(self):
        with patch.object(cli, "CockpitLauncher") as cls:
            cls.return_value.launch.return_value = 0
            with self.assertRaises(SystemExit):
                cli.main(["--split", "65", "--side", "--no-attach"])
        kw = cls.call_args.kwargs
        self.assertEqual((kw["split"], kw["stacked"], kw["socket_name"]), (65, False, "telemachus"))
        cls.return_value.launch.assert_called_once_with(attach=False)

    def test_telemetry_tmux_flag(self):
        out = io.StringIO()
        with patch.object(H, "status_line", return_value="STATUS"), redirect_stdout(out):
            cli.main(["telemetry", "--tmux"])
        self.assertEqual(out.getvalue().strip(), "STATUS")


class redirect_stderr_quiet:
    def __enter__(self):
        self._old = sys.stderr
        sys.stderr = io.StringIO()

    def __exit__(self, *exc):
        sys.stderr = self._old
        return False


# ---------------------------------------------------------------------------
# Command lists
# ---------------------------------------------------------------------------


class TestCommandLists(TempSettingsMixin, unittest.TestCase):
    def test_own_server_by_default(self):
        lch = self.make()
        self.assertEqual(lch.tmux_base(True)[1:], ["-L", "telemachus"])
        self.assertEqual(lch.tmux_base(False)[1:], [])
        self.assertEqual(self.make(socket_name="tmx").tmux_base()[1:], ["-L", "tmx"])

    def test_create_session_and_window(self):
        lch = self.make(session_name="cock")
        cmd = lch.create_commands(True, 190, 47)
        self.assertEqual(cmd[:4], ["new-session", "-d", "-s", "cock"])
        self.assertEqual(cmd[cmd.index("-x") + 1], "190")
        self.assertEqual(cmd[cmd.index("-y") + 1], "47")
        self.assertIn("-P", cmd)
        win = lch.create_commands(False, 190, 47)
        self.assertEqual(win[0], "new-window")
        self.assertNotIn("-d", win)

    def test_opencode_pane_keeps_a_shell(self):
        cmd = self.make().opencode_command()
        self.assertIn("opencode;", cmd)
        self.assertIn("exec", cmd)
        self.assertNotIn("Ctrl+B", cmd)
        self.assertFalse(has_emoji(cmd))

    def test_split_side_and_stacked(self):
        side = self.make(split=35, stacked=False).split_command("%4")
        self.assertEqual(side[:5], ["split-window", "-b", "-h", "-l", "35%"])
        self.assertEqual(side[side.index("-t") + 1], "%4")
        self.assertIn("TMUX_PANE_RIGHT=%4", side)
        self.assertIn("-m telemachus.cockpit dashboard", side[-1])
        stacked = self.make(split=60, stacked=True).split_command("%4")
        self.assertEqual(stacked[:5], ["split-window", "-b", "-v", "-l", "60%"])

    def test_forwarded_env_includes_email_db(self):
        env = {"TELEMACHUS_EMAIL_DB": "/x/mail.sqlite3", "TELEMACHUS_FINANCE_DIR": "/x/demo"}
        with patch.dict("os.environ", env):
            prefix = L.forwarded_env_prefix()
            dash = self.make().dashboard_command()
        self.assertIn("TELEMACHUS_EMAIL_DB=/x/mail.sqlite3", prefix)
        self.assertIn("TELEMACHUS_FINANCE_DIR=/x/demo", prefix)
        self.assertIn(prefix, dash)

    def test_server_options_are_real_and_quiet(self):
        cmds = self.make().server_option_commands()
        opts = {c[2]: c[3] for c in cmds}
        self.assertEqual(opts["mouse"], "on")
        self.assertEqual(opts["status-interval"], "2")
        flat = " ".join(" ".join(c) for c in cmds)
        for fake in ("18GB", "Qwen 3.5", "Laya MPS", "TELEMACHUS "):
            self.assertNotIn(fake, flat)
        self.assertFalse(has_emoji(flat))
        right = opts["status-right"]
        self.assertTrue(right.startswith("#("))
        self.assertIn("tmux_helper.py", right)
        self.assertIn(" -I ", right)
        self.assertIn("status)", right)
        self.assertIn("%H:%M", right)
        self.assertIn("telemachus", opts["status-left"])
        self.assertTrue(braces_balanced(opts["status-left"]))

    def test_window_options_are_window_scoped(self):
        for cmd in self.make().window_option_commands("@3"):
            self.assertIn("-w", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "@3")
            self.assertNotIn("-g", cmd)

    def test_window_options_start_from_the_layout(self):
        cmds = self.make(split=40, stacked=True).window_option_commands("@3")
        opts = {c[4]: c[5] for c in cmds if c[0] == "set-option"}
        self.assertEqual(opts[L.OPT_STACKED], "1")
        self.assertEqual(opts[L.OPT_PCT_V], "40")
        self.assertEqual(opts[L.OPT_GEN], "0")

    def test_layout_hook(self):
        lch = self.make()
        hooks = lch.layout_hook_commands()
        self.assertEqual(len(hooks), 6)
        for h in hooks:
            self.assertTrue(braces_balanced(h), h)
        self.assertTrue(hooks[0].startswith(f"set-option -wF {L.OPT_STACKED} "))
        self.assertIn("window_zoomed_flag", hooks[0])  # zoom never counts as a split size
        self.assertIn("save-layout", hooks[4])
        self.assertIn("#{socket_path}", hooks[4])
        self.assertTrue(hooks[4].startswith("run-shell -b "))  # never blocks the server
        cmds = lch.window_option_commands("@3")
        names = [c[4] for c in cmds if c[0] == "set-hook"]
        self.assertEqual(names, [f"window-layout-changed[{i}]" for i in range(6)])

    def test_tmux_quoting(self):
        self.assertEqual(L.tmux_dq('a "b" $HOME \\'), '"a \\"b\\" \\$HOME \\\\"')
        self.assertEqual(L.format_literal("/p,a#th"), "/p#,a##th")

    def test_key_bindings(self):
        cmds = self.make().key_binding_commands()
        keys = {c[2]: c[3:] for c in cmds}
        for c in cmds:
            self.assertEqual(c[:2], ["bind-key", "-n"])  # no prefix
        self.assertEqual(keys["M-Left"], ["resize-pane", "-L", "5"])
        self.assertEqual(keys["M-S-Down"], ["resize-pane", "-D", "1"])
        self.assertEqual(keys["M-h"], ["select-pane", "-t", "{top-left}"])
        self.assertEqual(keys["M-]"], ["select-pane", "-t", "{bottom-right}"])
        self.assertEqual(keys["M-z"], ["resize-pane", "-Z"])
        for n, pct in (("1", 35), ("2", 50), ("3", 65)):
            self.assertIn(f"-x {pct}%", keys[f"M-{n}"][-1])
            self.assertIn(f"-y {pct}%", keys[f"M-{n}"][-2])
        self.assertIn("even-horizontal", " ".join(keys["M-="]))
        self.assertIn("select-layout even-vertical", keys["M-Space"][-1])
        self.assertIn(L.OPT_KEEP, keys["M-Space"][-1])
        expected = {"M-Left", "M-Right", "M-Up", "M-Down", "M-S-Left", "M-S-Right", "M-S-Up", "M-S-Down",
                    "M-h", "M-l", "M-[", "M-]", "M-z", "M-1", "M-2", "M-3", "M-=", "M-Space"}
        self.assertEqual(set(keys), expected)

    def test_key_hint_is_on_the_status_bar_not_a_message(self):
        # Was: `attach ; display-message -d 8000`. tmux draws no pane output while a message shows,
        # so the dashboard stayed blank for 8 s. The hint now sits in this session's status-right.
        lch = self.make()
        argv = lch.attach_argv()
        self.assertEqual(argv[1:], ["-L", "telemachus", "attach-session", "-t", "=telemachus-cockpit"])
        set_right, clear = lch.hint_commands("$0")
        self.assertEqual(set_right[:4], ["set-option", "-t", "$0", "status-right"])
        hint = set_right[4]
        self.assertIn("alt+arrows", hint)
        self.assertNotIn("% ", hint.replace("%%", ""))  # a lone % would be eaten by strftime
        self.assertEqual(clear, ["run-shell", "-b", "-d", str(L.HINT_S), "-C", "set-option -u -t $0 status-right"])


# ---------------------------------------------------------------------------
# launch() with a fake tmux
# ---------------------------------------------------------------------------


class FakeTmux:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if "new-session" in argv or "new-window" in argv:
            return subprocess.CompletedProcess(argv, 0, "%0 @0 $0\n", "")
        if "split-window" in argv:
            return subprocess.CompletedProcess(argv, 0, "%1\n", "")
        if "list-sessions" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "no server running")
        return subprocess.CompletedProcess(argv, 0, "", "")


class TestLaunchFlow(TempSettingsMixin, unittest.TestCase):
    def run_launch(self, env_extra, attach=False):
        fake = FakeTmux()
        lch = self.make()
        lch.tmux_bin = "/usr/bin/tmux-fake"
        out = io.StringIO()
        with patch.dict("os.environ", env_extra), patch("subprocess.run", fake), \
                patch.object(lch, "is_tmux_available", return_value=True), \
                patch("os.execvp") as execvp, redirect_stdout(out):
            rc = lch.launch(attach=attach)
        return rc, fake.calls, execvp, out.getvalue()

    def test_private_server_never_touches_the_default_one(self):
        rc, calls, execvp, out = self.run_launch({}, attach=True)
        self.assertEqual(rc, 0)
        for c in calls:
            self.assertEqual(c[1:3], ["-L", "telemachus"], c)
        verbs = [c[3] for c in calls]
        self.assertLess(verbs.index("new-session"), verbs.index("split-window"))
        self.assertIn("bind-key", verbs)
        # roles: OpenCode before the split, dashboard after, then the hook runs once
        role_calls = [c for c in calls if L.ROLE in c]
        self.assertEqual([c[-1] for c in role_calls], ["opencode", "dashboard"])
        self.assertLess(calls.index(role_calls[0]), verbs.index("split-window"))
        self.assertIn(["set-hook", "-w", "-R", "-t", "@0", "window-layout-changed"], [c[3:] for c in calls])
        self.assertEqual(calls[-1][3:], ["select-pane", "-t", "%1"])
        execvp.assert_called_once()
        self.assertEqual(execvp.call_args[0][1][1:5], ["-L", "telemachus", "attach-session", "-t"])

    def test_no_attach_prints_how_to_attach(self):
        rc, _calls, execvp, out = self.run_launch({})
        self.assertEqual(rc, 0)
        execvp.assert_not_called()
        self.assertIn("tmux -L telemachus attach -t telemachus-cockpit", out)

    def test_inside_tmux_only_window_and_session_options(self):
        rc, calls, execvp, out = self.run_launch({"TMUX": "/tmp/tmux-501/default,1,0"}, attach=True)
        self.assertEqual(rc, 0)
        execvp.assert_not_called()
        for c in calls:
            self.assertNotIn("-L", c[:3])
            self.assertNotIn("bind-key", c)
            self.assertNotIn("-g", c)
            self.assertNotIn("-s", c[:5])
            self.assertNotIn("kill-session", c)
            self.assertNotIn("kill-server", c)
        self.assertEqual(calls[0][1], "new-window")
        self.assertIn(["set-option", "-t", "$0", "mouse", "on"], [c[1:] for c in calls])
        self.assertIn("new window", out)
        # no display-message: it would keep tmux from drawing the new panes
        self.assertNotIn("display-message", [c[1] for c in calls if "-p" not in c])

    def test_old_cockpit_is_replaced(self):
        fake = FakeTmux()

        def run(argv, **kw):
            if "list-sessions" in argv:
                return subprocess.CompletedProcess(argv, 0, "telemachus-cockpit\nother\n", "")
            return fake(argv, **kw)

        lch = self.make()
        with patch.dict("os.environ", {}), patch("subprocess.run", run), redirect_stdout(io.StringIO()):
            lch._clear_old_session()
        self.assertEqual(fake.calls[-1][3:], ["kill-session", "-t", "=telemachus-cockpit"])

    def test_failure_is_reported(self):
        lch = self.make()
        err = io.StringIO()
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "boom")), \
                patch.object(lch, "is_tmux_available", return_value=True), \
                patch.dict("os.environ", {}), patch("sys.stderr", err):
            self.assertEqual(lch.launch(attach=False), 1)
        self.assertIn("boom", err.getvalue())


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------


def info(**kw):
    base = dict(lm="ready", model="qwen3.5-9b", elapsed_s=None, tokens=None, tps=None,
                ram_used_gb=7.1, ram_total_gb=24.0)
    base.update(kw)
    return SimpleNamespace(**base)


class TestStatusLine(unittest.TestCase):
    def plain(self, text: str) -> str:
        import re

        return re.sub(r"#\[[^\]]*\]", "", text)

    def test_states(self):
        self.assertEqual(self.plain(H.format_status(info())), "● qwen3.5-9b ready   ram 7.1/24G")
        self.assertEqual(self.plain(H.format_status(info(lm="offline"))), "○ LM Studio offline   ram 7.1/24G")
        self.assertIn("no model loaded", self.plain(H.format_status(info(lm="no_model", model=""))))
        self.assertIn("reading prompt 12s", self.plain(H.format_status(info(lm="reading", elapsed_s=12.7))))
        w = self.plain(H.format_status(info(lm="writing", tokens=482, tps=38.4)))
        self.assertIn("writing 482 tok · 38 t/s", w)

    def test_unmeasured_values_are_hidden(self):
        s = self.plain(H.format_status(info(ram_used_gb=None, lm="writing", tokens=None, tps=None)))
        self.assertNotIn("ram", s)
        self.assertNotIn("tok", s)
        self.assertNotIn("t/s", s)

    def test_no_emoji_and_hash_escaped(self):
        s = H.format_status(info(model="m#1"))
        self.assertIn("m##1", s)
        self.assertFalse(has_emoji(s))
        self.assertTrue(s.endswith("#[default]"))

    def test_status_line_never_raises(self):
        with patch.object(H, "header_info", side_effect=RuntimeError("x")):
            self.assertIn("LM Studio", H.status_line())


class TestStatusCommandByPath(unittest.TestCase):
    """The tmux status bar runs tmux_helper.py by path; it must work without the package and fast."""

    def test_runs_fast_without_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp,
                "PATH": os.environ.get("PATH", ""),
                "TELEMACHUS_LMSTUDIO_URL": "http://127.0.0.1:9/v1/models",  # nothing listens: offline
            }
            start = time.time()
            res = subprocess.run(
                [sys.executable, "-I", str(REPO / "telemachus" / "cockpit" / "tmux_helper.py"), "status"],
                capture_output=True, text=True, env=env, timeout=10, cwd=tmp,
            )
            elapsed = time.time() - start
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("LM Studio offline", res.stdout)
            self.assertLess(elapsed, H.HARD_DEADLINE_S + 1)
            cache = Path(tmp) / ".cache" / "telemachus" / "tmux-status.json"
            self.assertTrue(cache.exists())
            self.assertFalse(json.loads(cache.read_text())["reachable"])

    def test_probe_cache_is_reused(self):
        class Tel:
            lm_base = "http://x"
            calls = 0

            def _probe_lm_studio(self):
                Tel.calls += 1
                return True, ["qwen"], 4096

        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"XDG_CACHE_HOME": tmp}):
            tel = Tel()
            now = time.time()
            self.assertEqual(H._probe_cached(tel, now), (True, ["qwen"], 4096))
            self.assertEqual(H._probe_cached(tel, now + 1), (True, ["qwen"], 4096))
            self.assertEqual(Tel.calls, 1)
            H._probe_cached(tel, now + H.PROBE_MAX_AGE_S + 1)
            self.assertEqual(Tel.calls, 2)


class TestBinScript(unittest.TestCase):
    def test_prefers_project_venv(self):
        text = (REPO / "bin" / "telemachus-cockpit").read_text(encoding="utf-8")
        self.assertIn('"$ROOT/.venv/bin/python"', text)
        self.assertIn("python3", text)
        self.assertTrue(os.access(REPO / "bin" / "telemachus-cockpit", os.X_OK))

    def test_help_runs(self):
        res = subprocess.run([str(REPO / "bin" / "telemachus-cockpit"), "launch", "--help"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("--stacked", res.stdout)


if __name__ == "__main__":
    unittest.main()
