"""Tests for the Golden Hour theme: the theme registry, the 255-pair cap and the art pair pool,
the city scene (art_city), the Home layout with the city, the s key, the launcher and tmux colors,
and the cockpit-only OpenCode theme. Synthetic data only; every settings file is in a temp folder."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from tests.cockpit_isolation import isolate_module

from telemachus.cockpit import art_city, home_view, launcher, shell, theme, tmux_helper, view_common
from telemachus.cockpit.home_view import HomeData, build_home
from telemachus.cockpit.launcher import CockpitLauncher
from telemachus.cockpit.theme import line_text, line_width

setUpModule, tearDownModule = isolate_module()

REPO = Path(__file__).resolve().parents[1]
GOLDEN_SLOTS = {"cyan": 215, "text": 223, "muted": 181, "dim": 96, "green": 150, "yellow": 222, "red": 203,
                "blue": 147, "magenta": 177, "ink": 235}


class FakeCurses:
    """Just enough curses for theme.Theme: records init_pair calls; color_pair(n) = n << 8."""

    COLOR_PAIRS = 32767  # what the macOS ncurses reports
    COLORS = 256
    A_BOLD, A_UNDERLINE, A_REVERSE, A_DIM = 1 << 21, 1 << 17, 1 << 18, 1 << 20

    class error(Exception):
        pass

    def __init__(self) -> None:
        self.pairs = {}

    def has_colors(self):
        return True

    def start_color(self):
        pass

    def use_default_colors(self):
        pass

    def can_change_color(self):
        return False

    def init_pair(self, n, fg, bg):
        if not 0 < n < self.COLOR_PAIRS:
            raise self.error("bad pair")
        self.pairs[n] = (fg, bg)

    def color_pair(self, n):
        return n << 8


class ThemeCase(unittest.TestCase):
    """Every test starts in Tokyo Night with curses off, and ends there (THEME is a module singleton)."""

    def setUp(self):
        theme.THEME.__init__()
        theme.use_theme("tokyo-night")

    def tearDown(self):
        theme.THEME.__init__()
        theme.use_theme("tokyo-night")

    def start_curses(self, name):
        fake = FakeCurses()
        p = patch.object(theme, "curses", fake)
        p.start()
        self.addCleanup(p.stop)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TELEMACHUS_COLORS", None)
        theme.use_theme(name)
        theme.THEME.init()
        return fake


# ---------------------------------------------------------------------------
# Theme registry
# ---------------------------------------------------------------------------


class TestThemeRegistry(ThemeCase):
    def test_golden_hour_swaps_palette_in_place(self):
        palette_ref, bg_ref = theme.PALETTE, theme.BACKGROUNDS
        self.assertEqual(theme.use_theme("golden-hour"), "golden-hour")
        self.assertIs(theme.PALETTE, palette_ref)  # same dict: modules that imported it see the change
        self.assertIs(theme.BACKGROUNDS, bg_ref)
        self.assertEqual({k: theme.nearest_xterm(v) for k, v in theme.PALETTE.items()}, GOLDEN_SLOTS)
        self.assertEqual(theme.BACKGROUNDS["sel"], "#5f005f")
        self.assertEqual(theme.PINNED_256, {})
        self.assertEqual(theme.theme_name(), "golden-hour")

    def test_golden_hour_colors_are_exact_xterm_slots(self):
        for name, value in list(theme.THEMES["golden-hour"]["palette"].items()) + list(theme.THEMES["golden-hour"]["backgrounds"].items()):
            r, g, b = theme._hex_rgb(value)
            self.assertEqual(theme._xterm_rgb(theme.nearest_xterm(value)), (r, g, b), name)

    def test_tokyo_night_comes_back_unchanged(self):
        before = (dict(theme.PALETTE), dict(theme.BACKGROUNDS), dict(theme.PINNED_256))
        theme.use_theme("golden-hour")
        theme.use_theme("tokyo-night")
        self.assertEqual((dict(theme.PALETTE), dict(theme.BACKGROUNDS), dict(theme.PINNED_256)), before)
        self.assertEqual(theme.PINNED_256, {"text": 189, "sel": 17})

    def test_unknown_name_falls_back_to_the_default(self):
        self.assertEqual(theme.use_theme("bogus"), theme.DEFAULT_THEME)
        self.assertEqual(theme.DEFAULT_THEME, "golden-hour")

    def test_text_roles_stay_readable_on_the_glass(self):
        def lum(hx):
            out = []
            for v in theme._hex_rgb(hx):
                v /= 255
                out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
            return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

        glass = "#191d27"  # Terminal.app Clear Dark
        p = theme.THEMES["golden-hour"]["palette"]
        for role in ("text", "muted", "cyan", "green", "yellow", "red", "blue", "magenta"):
            a, b = lum(p[role]), lum(glass)
            self.assertGreaterEqual((max(a, b) + 0.05) / (min(a, b) + 0.05), 4.5, role)

    def test_art_follows_the_theme_and_the_color_mode(self):
        self.assertFalse(theme.art_enabled())
        theme.use_theme("golden-hour")
        self.assertTrue(theme.art_enabled())  # curses not started: layouts still leave room
        theme.THEME.mode = "8"
        self.assertFalse(theme.art_enabled())  # 8 colors: no city

    def test_names_match_the_standalone_tmux_helper(self):
        self.assertEqual(set(tmux_helper.THEME_NAMES), set(theme.THEME_NAMES))
        self.assertEqual(tmux_helper.DEFAULT_THEME, theme.DEFAULT_THEME)
        self.assertEqual(tuple(tmux_helper.SKY_MODES), art_city.SKY_MODES)
        self.assertEqual(tmux_helper.DEFAULT_SKY, art_city.DEFAULT_SKY)


# ---------------------------------------------------------------------------
# Pair cap and the art pool
# ---------------------------------------------------------------------------


class TestPairs(ThemeCase):
    def test_pairs_stop_at_255_whatever_color_pairs_says(self):
        fake = self.start_curses("tokyo-night")
        self.assertEqual(theme.THEME._max_pairs, theme.MAX_PAIRS)
        got = [theme.THEME._pair(16 + i % 240, 16 + i // 240) for i in range(400)]  # 400 distinct combos
        self.assertEqual(max(fake.pairs), 255)
        self.assertTrue(all(0 <= n <= 255 for n in got))
        self.assertIn(0, got)  # past the cap: default colors, never a wrapped pair number

    def test_text_styles_leave_the_top_of_the_range_to_the_art(self):
        self.start_curses("golden-hour")
        self.assertTrue(theme.THEME.art_ready())
        got = [theme.THEME._pair(16 + i, 17 + i) for i in range(200)]
        self.assertLess(max(got), theme.ART_FIRST_PAIR)

    def test_art_pool_recycles_combos_the_frame_does_not_use(self):
        fake = self.start_curses("golden-hour")
        T = theme.THEME
        T.begin_art_frame()
        first = [(16 + i, 100 + i % 50) for i in range(100)]
        nums1 = {k: T.art_attr(*k) >> 8 for k in first}
        self.assertTrue(all(theme.ART_FIRST_PAIR <= n <= 255 for n in nums1.values()))
        self.assertEqual(len(set(nums1.values())), 100)  # one pair per combo
        T.begin_art_frame()
        second = [(200 + i % 40, 30 + i) for i in range(100)]
        nums2 = {k: T.art_attr(*k) >> 8 for k in second}
        self.assertEqual(len(set(nums2.values())), 100)
        self.assertTrue(all(theme.ART_FIRST_PAIR <= n <= 255 for n in nums2.values()))
        # every pair now holds a combo of this frame: none was given to two combos at once
        current = {n: k for k, n in nums2.items()}
        for n, k in current.items():
            self.assertEqual(fake.pairs[n], k)

    def test_art_pool_never_takes_a_pair_the_current_frame_uses(self):
        self.start_curses("golden-hour")
        T = theme.THEME
        T.begin_art_frame()
        combos = [(16 + i % 240, 17 + (i * 3) % 230) for i in range(160)]
        nums = [T.art_attr(*k) >> 8 for k in combos]
        size = 256 - theme.ART_FIRST_PAIR
        self.assertEqual(sum(1 for n in nums if n), size)  # the pool fills up ...
        self.assertEqual(nums[size:], [0] * (160 - size))  # ... then this frame gets default colors

    def test_no_art_pairs_in_tokyo_night_or_8_colors(self):
        self.start_curses("tokyo-night")
        self.assertEqual(theme.THEME.art_attr(200, 17), 0)
        theme.use_theme("golden-hour")
        theme.THEME.mode = "8"
        self.assertEqual(theme.THEME.art_attr(200, 17), 0)


# ---------------------------------------------------------------------------
# The city
# ---------------------------------------------------------------------------


def _combos(cells):
    return {(fg, bg) for row in cells for _, fg, bg in row}


class TestCity(unittest.TestCase):
    def test_cells_are_half_blocks_with_valid_slots(self):
        cells = art_city.render(48, 16, 6.0, 17.9)
        self.assertEqual(len(cells), 16)
        self.assertTrue(all(len(r) == 48 for r in cells))
        for row in cells:
            for ch, fg, bg in row:
                self.assertIn(ch, ("▀", " "))
                self.assertTrue(16 <= fg <= 255 and 16 <= bg <= 255)
                if ch == "▀":
                    self.assertNotEqual(fg, bg)  # theme.py would recolor fg == bg
                else:
                    self.assertEqual(fg, bg)

    def test_same_input_same_frame_and_it_moves(self):
        a = art_city.render(48, 16, 6.0, 17.9)
        self.assertEqual(a, art_city.render(48, 16, 6.0, 17.9))
        b = art_city.render(48, 16, 6.125, 17.9)
        self.assertNotEqual(a, b)
        changed = sum(1 for ra, rb in zip(a, b) for ca, cb in zip(ra, rb) if ca != cb)
        self.assertLess(changed, 48 * 16 // 3)  # calm motion: a few cells per frame

    def test_the_sky_follows_the_hour(self):
        self.assertEqual(art_city.palette_at(13.0)["key"], "day")
        self.assertEqual(art_city.palette_at(18.0)["key"], "golden")
        self.assertEqual(art_city.palette_at(22.5)["key"], "night")
        self.assertEqual(art_city.palette_at(19.5)["key"], "dusk")
        self.assertEqual(art_city.palette_at(13.0)["sky"][0], 67)
        self.assertEqual(art_city.palette_at(18.0)["sky"][0], 61)
        self.assertEqual(art_city.palette_at(22.5)["sky"][0], 16)
        self.assertEqual(art_city.palette_at(18.0)["sky"], art_city.KEYFRAMES["golden"]["sky"])

    def test_night_has_stars_and_more_lit_windows_than_day(self):
        night = art_city.render(48, 16, 3.0, 22.5)
        day = art_city.render(48, 16, 3.0, 13.0)
        pixels = lambda cells: [c for row in cells for _, fg, bg in row for c in (fg, bg)]  # noqa: E731
        self.assertIn(255, pixels(night))  # a star
        self.assertNotIn(255, pixels(day))
        lit = lambda cells: sum(1 for c in pixels(cells) if c in (221, 229))  # noqa: E731
        self.assertGreater(lit(night), lit(day) + 10)

    def test_clock_mode_uses_local_time(self):
        lt = time.localtime()
        now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 18, 30, 0, 0, 0, -1))
        self.assertAlmostEqual(art_city.hour_for("clock", now), 18.5)
        self.assertEqual(art_city.hour_for("golden", now), 17.9)
        self.assertEqual(art_city.hour_for("night", now), 22.5)

    def test_s_key_cycle(self):
        order = ["clock"]
        for _ in range(5):
            order.append(art_city.next_sky(order[-1]))
        self.assertEqual(order, ["clock", "golden", "dusk", "night", "day", "clock"])
        self.assertEqual(art_city.next_sky("bogus"), "clock")

    def test_combos_fit_the_art_pool_at_every_hour(self):
        pool = 256 - theme.ART_FIRST_PAIR
        worst = 0
        for size in ((48, 16), (71, 22), (71, 14)):
            for q in range(0, 24 * 4):
                for t in (0.0, 7.3, 31.9):
                    worst = max(worst, len(_combos(art_city.render(size[0], size[1], t, q / 4))))
        self.assertLess(worst, pool - 20)

    def test_frame_snapshot_matches_the_reviewed_board(self):
        # The scene is a port of the Golden Hour board (checked cell by cell against it). These
        # hashes pin two frames; update them only when the art changes on purpose.
        def digest(cells):
            return hashlib.sha1(json.dumps(cells).encode()).hexdigest()[:12]

        self.assertEqual(digest(art_city.render(48, 16, 6.0, 17.9)), SNAPSHOT_GOLDEN)
        self.assertEqual(digest(art_city.render(71, 13, 37.25, 22.5)), SNAPSHOT_NIGHT)


SNAPSHOT_GOLDEN = "5507ffb45900"  # 48 x 16, t 6.0, 17:54
SNAPSHOT_NIGHT = "7f1b9a7e8e13"  # 71 x 13, t 37.25, 22:30


# ---------------------------------------------------------------------------
# Home layout and keys
# ---------------------------------------------------------------------------


class TestHomeLayout(ThemeCase):
    def test_wide_pane_gives_the_city_a_window_on_the_right(self):
        theme.use_theme("golden-hour")
        f = build_home(HomeData(), {}, 149, 10)
        self.assertEqual(f.art, ("window", home_view.ART_WINDOW_COLS))
        content = 149 - home_view.ART_WINDOW_COLS - home_view.ART_GAP
        self.assertTrue(f.lines)
        self.assertTrue(all(line_width(l) == content for l in f.lines))

    def test_narrow_tall_pane_gets_a_strip_under_the_blocks(self):
        theme.use_theme("golden-hour")
        f = build_home(HomeData(), {}, 71, 38)
        kind, row, rows = f.art
        self.assertEqual(kind, "strip")
        self.assertGreater(row, len(f.lines))  # one blank row between the blocks and the city
        self.assertEqual(row + rows, 38)  # down to the last body row
        self.assertGreaterEqual(rows, home_view.ART_STRIP_MAX_ROWS)
        self.assertTrue(any(line_text(l).strip() for l in f.lines))

    def test_short_narrow_pane_has_no_city(self):
        theme.use_theme("golden-hour")
        self.assertIsNone(build_home(HomeData(), {}, 71, 20).art)

    def test_tokyo_night_home_is_unchanged(self):
        f = build_home(HomeData(), {}, 149, 10)
        self.assertIsNone(f.art)
        self.assertTrue(all(line_width(l) == 149 for l in f.lines))

    def test_s_key_and_footer_hint_only_with_the_city(self):
        d = HomeData()
        self.assertIsNone(home_view.handle_key(ord("s"), {}, d))
        self.assertNotIn("sky", str(home_view.home_keys(d)))
        theme.use_theme("golden-hour")
        self.assertEqual(home_view.handle_key(ord("s"), {}, d), "sky")
        self.assertIn("sky", [h.label for g in home_view.home_keys(d) for h in g.hints])

    def test_empty_state_hint_wraps_in_a_narrow_column(self):
        lines = view_common.empty_state("No memories yet.", 40, extra="Facts appear here after your first OpenCode session.")
        texts = [line_text(l).strip() for l in lines]
        self.assertEqual(texts, ["No memories yet.", "Facts appear here after your first", "OpenCode session."])
        wide = view_common.empty_state("No memories yet.", 90, extra="Facts appear here after your first OpenCode session.")
        self.assertEqual(len(wide), 2)


# ---------------------------------------------------------------------------
# TUI: sky key, art rect, drawing the city
# ---------------------------------------------------------------------------


class TestTuiCity(ThemeCase):
    def make_tui(self):
        from telemachus.cockpit.tui import CockpitTUI

        tui = CockpitTUI.__new__(CockpitTUI)
        tui.notification = ""
        tui.current_tab = "home"
        tui.sky_mode = "clock"
        tui._art_t0 = time.time()
        tui.stdscr = MagicMock()
        return tui

    def test_sky_command_cycles_saves_and_tells(self):
        tui = self.make_tui()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cockpit.json"
            path.write_text(json.dumps({"split_pct": 40}), encoding="utf-8")
            with patch.dict(os.environ, {"TELEMACHUS_COCKPIT_SETTINGS": str(path)}):
                tui.run_command("sky")
                self.assertEqual(tui.sky_mode, "golden")
                self.assertEqual(tui.notification, "Sky: golden hour")
                self.assertEqual(json.loads(path.read_text()), {"split_pct": 40, "sky": "golden"})
                for _ in range(4):
                    tui.run_command("sky")
                self.assertEqual(tui.sky_mode, "clock")
                self.assertTrue(tui.notification.startswith("Sky: my clock ("))
                self.assertEqual(tmux_helper.load_look(path)["sky"], "clock")

    def test_window_rect_keeps_the_last_column_and_shortens_the_chrome(self):
        self.start_curses("golden-hour")
        tui = self.make_tui()
        lay = shell.layout(16, 150)
        rect, chrome = tui._art_geometry(shell.Frame(lines=[], art=("window", 48)), lay)
        self.assertEqual(rect, (0, 149 - 48, 16, 48))
        self.assertEqual(rect[1] + rect[3], lay.width)  # never column 150 (curses would wrap)
        self.assertEqual(chrome, 149 - 48 - home_view.ART_GAP)
        tui._art_rect, tui._chrome_w = rect, chrome
        self.assertEqual(tui._chrome_width(lay), chrome)
        self.assertTrue(tui._in_art(5, 120))
        self.assertFalse(tui._in_art(5, 90))

    def test_strip_rect_sits_in_the_body(self):
        self.start_curses("golden-hour")
        tui = self.make_tui()
        lay = shell.layout(44, 72)
        rect, chrome = tui._art_geometry(shell.Frame(lines=[], art=("strip", 20, 18)), lay)
        self.assertEqual(rect, (lay.body_top + 20, 0, 18, lay.width))
        self.assertIsNone(chrome)

    def test_no_rect_without_256_colors(self):
        tui = self.make_tui()
        theme.use_theme("golden-hour")  # curses not started: mode "none"
        rect, chrome = tui._art_geometry(shell.Frame(lines=[], art=("window", 48)), shell.layout(16, 150))
        self.assertEqual((rect, chrome), (None, None))

    def test_draw_art_fills_the_rect_exactly_with_pool_pairs(self):
        fake = self.start_curses("golden-hour")
        tui = self.make_tui()
        tui.sky_mode = "golden"
        tui._art_rect = (0, 101, 16, 48)
        tui._draw_art(time.time())
        rows = {}
        for call in tui.stdscr.addstr.call_args_list:
            y, x, text, attr = call[0]
            rows.setdefault(y, []).append((x, len(text)))
            self.assertGreaterEqual(attr >> 8, theme.ART_FIRST_PAIR)
        self.assertEqual(sorted(rows), list(range(16)))
        for y, runs in rows.items():
            runs.sort()
            self.assertEqual(runs[0][0], 101)
            self.assertEqual(sum(n for _, n in runs), 48)
            for (x1, n1), (x2, _) in zip(runs, runs[1:]):
                self.assertEqual(x1 + n1, x2)  # runs touch: no gaps, no overlap
        self.assertTrue(all(theme.ART_FIRST_PAIR <= n <= 255 for n in fake.pairs if n >= theme.ART_FIRST_PAIR))

    def test_art_only_frames_wait_while_the_help_card_is_open(self):
        tui = self.make_tui()
        tui._art_rect = (0, 101, 16, 48)
        tui.help_open = True
        with patch.object(type(tui), "_draw_art") as draw:
            tui._update_art_only(time.time())
        draw.assert_not_called()


# ---------------------------------------------------------------------------
# Launcher, tmux colors, settings
# ---------------------------------------------------------------------------


class TestLauncherTheme(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "cockpit.json"
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TELEMACHUS_THEME", None)

    def make(self, **kw):
        return CockpitLauncher(settings_path=self.settings, python="/usr/bin/python3", **kw)

    def test_default_is_golden_hour_and_settings_can_switch_it(self):
        self.assertEqual(self.make().theme, "golden-hour")
        self.settings.write_text(json.dumps({"theme": "tokyo-night"}), encoding="utf-8")
        self.assertEqual(self.make().theme, "tokyo-night")

    def test_env_beats_settings_and_the_flag_beats_both(self):
        self.settings.write_text(json.dumps({"theme": "tokyo-night"}), encoding="utf-8")
        with patch.dict(os.environ, {"TELEMACHUS_THEME": "golden-hour"}):
            self.assertEqual(self.make().theme, "golden-hour")
            self.assertEqual(self.make(theme="tokyo-night").theme, "tokyo-night")
        self.assertEqual(self.make(theme="nope").theme, "tokyo-night")  # a bad flag is ignored

    def test_opencode_gets_the_theme_config_only_in_golden_hour(self):
        cmd = self.make(theme="golden-hour").opencode_command()
        cfg = REPO / "telemachus" / "cockpit" / "opencode_tui.json"
        self.assertIn(f"OPENCODE_TUI_CONFIG={cfg} opencode", cmd)
        self.assertIn("38;5;215", cmd)  # the greeting in amber
        plain = self.make(theme="tokyo-night").opencode_command()
        self.assertNotIn("OPENCODE_TUI_CONFIG", plain)
        self.assertIn("opencode;", plain)

    def test_dashboard_gets_the_same_theme_and_settings_file(self):
        cmd = self.make(theme="tokyo-night").dashboard_command()
        self.assertIn("TELEMACHUS_THEME=tokyo-night", cmd)
        self.assertIn(f"TELEMACHUS_COCKPIT_SETTINGS={self.settings}", cmd)
        self.assertIn("TELEMACHUS_THEME", launcher.FORWARDED_ENV_VARS)

    def test_tmux_colors_follow_the_theme(self):
        gold = {c[2]: c[3] for c in self.make(theme="golden-hour").server_option_commands() if len(c) == 4}
        self.assertEqual(gold["status-style"], "bg=#262626,fg=#ffd7af")
        self.assertIn("status --theme golden-hour", gold["status-right"])
        self.assertIn("▓▒░", gold["status-left"])
        tokyo = {c[2]: c[3] for c in self.make(theme="tokyo-night").server_option_commands() if len(c) == 4}
        self.assertEqual(tokyo["status-style"], "bg=#1a1b26,fg=#c0caf5")
        win = {c[4]: c[5] for c in self.make(theme="golden-hour").window_option_commands("@1") if len(c) == 6}
        self.assertEqual(win["pane-border-style"], "fg=#875f87")
        self.assertEqual(win["pane-active-border-style"], "fg=#ffaf5f")

    def test_the_theme_flag_is_remembered(self):
        lch = self.make(theme="tokyo-night")
        lch.remember_theme()
        self.assertEqual(tmux_helper.load_look(self.settings)["theme"], "tokyo-night")
        self.make().remember_theme()  # no flag: nothing written
        self.assertEqual(tmux_helper.load_look(self.settings)["theme"], "tokyo-night")


class TestTmuxHelperLook(unittest.TestCase):
    def test_load_look_ignores_bad_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            self.assertEqual(tmux_helper.load_look(p), {"theme": "", "sky": "clock"})
            p.write_text(json.dumps({"theme": "neon", "sky": "noon"}), encoding="utf-8")
            self.assertEqual(tmux_helper.load_look(p), {"theme": "", "sky": "clock"})
            p.write_text(json.dumps({"theme": "tokyo-night", "sky": "dusk"}), encoding="utf-8")
            self.assertEqual(tmux_helper.load_look(p), {"theme": "tokyo-night", "sky": "dusk"})
            self.assertEqual(tmux_helper.theme_name(p, env={}), "tokyo-night")
            self.assertEqual(tmux_helper.theme_name(p, env={"TELEMACHUS_THEME": "golden-hour"}), "golden-hour")
            self.assertEqual(tmux_helper.theme_name(Path(d) / "none.json", env={}), "golden-hour")

    def test_status_colors_switch_only_with_the_flag(self):
        info = MagicMock(model="", lm="offline", ram_used_gb=7.1, ram_total_gb=24, tokens=None, tps=None, elapsed_s=None)
        saved = dict(tmux_helper._COLORS)
        try:
            self.assertIn(tmux_helper.C["muted"], tmux_helper.format_status(info))  # no flag: Tokyo Night
            tmux_helper.use_status_theme("golden-hour")
            self.assertIn("#d7afaf", tmux_helper.format_status(info))
            tmux_helper.use_status_theme("bogus")  # unknown: unchanged
            self.assertIn("#d7afaf", tmux_helper.format_status(info))
        finally:
            tmux_helper._COLORS.clear()
            tmux_helper._COLORS.update(saved)


# ---------------------------------------------------------------------------
# OpenCode theme files
# ---------------------------------------------------------------------------


class TestOpenCodeTheme(unittest.TestCase):
    KEYS = (
        "primary secondary accent error warning success info text textMuted background backgroundPanel backgroundElement "
        "border borderActive borderSubtle diffAdded diffRemoved diffContext diffHunkHeader diffHighlightAdded "
        "diffHighlightRemoved diffAddedBg diffRemovedBg diffContextBg diffLineNumber diffAddedLineNumberBg "
        "diffRemovedLineNumberBg markdownText markdownHeading markdownLink markdownLinkText markdownCode "
        "markdownBlockQuote markdownEmph markdownStrong markdownHorizontalRule markdownListItem "
        "markdownListEnumeration markdownImage markdownImageText markdownCodeBlock syntaxComment syntaxKeyword "
        "syntaxFunction syntaxVariable syntaxString syntaxNumber syntaxType syntaxOperator syntaxPunctuation"
    ).split()

    def test_theme_file_is_complete_and_uses_256_color_numbers(self):
        data = json.loads((REPO / ".opencode" / "themes" / "golden-hour.json").read_text(encoding="utf-8"))
        defs, th = data["defs"], data["theme"]
        self.assertEqual(len(self.KEYS), 50)
        self.assertEqual(set(th), set(self.KEYS))
        self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in defs.values()))
        resolve = lambda v: defs[v] if isinstance(v, str) and v in defs else v  # noqa: E731
        for key, value in th.items():
            v = resolve(value)
            self.assertTrue(v == "none" or (isinstance(v, int) and 0 <= v <= 255), (key, value))
        # the same colors as the dashboard
        self.assertEqual(resolve(th["primary"]), GOLDEN_SLOTS["cyan"])
        self.assertEqual(resolve(th["text"]), GOLDEN_SLOTS["text"])
        self.assertEqual(resolve(th["textMuted"]), GOLDEN_SLOTS["muted"])
        self.assertEqual(resolve(th["border"]), GOLDEN_SLOTS["dim"])
        self.assertEqual(th["background"], "none")  # the glass stays

    def test_cockpit_tui_config_picks_that_theme(self):
        cfg = json.loads((REPO / launcher.OPENCODE_TUI_CONFIGS["golden-hour"]).read_text(encoding="utf-8"))
        self.assertEqual(cfg["theme"], "golden-hour")
        self.assertTrue((REPO / ".opencode" / "themes" / f"{cfg['theme']}.json").is_file())


if __name__ == "__main__":
    unittest.main()
