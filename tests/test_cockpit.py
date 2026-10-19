"""Unit tests for Telemachus Terminal Cockpit (v1)."""
from pathlib import Path
import tempfile
import unittest

from telemachus.cockpit.email_panel import EmailTriagePanel
from telemachus.cockpit.launcher import CockpitLauncher
from telemachus.cockpit.safety_memory_panel import SafetyMemoryPanel
from telemachus.cockpit.telemetry import CockpitTelemetry


class TestCockpitTelemetry(unittest.TestCase):
    def setUp(self):
        self.telemetry = CockpitTelemetry()

    def test_active_model(self):
        model = self.telemetry.get_active_model()
        self.assertIsInstance(model, str)
        self.assertGreater(len(model), 0)

    def test_telemetry_caching(self):
        # First call populates cache
        m1 = self.telemetry.get_active_model(max_age=10.0)
        cache_time = self.telemetry._model_cache_time
        self.assertGreater(cache_time, 0.0)

        # Immediate second call should use cache without modifying cache_time
        m2 = self.telemetry.get_active_model(max_age=10.0)
        self.assertEqual(m1, m2)
        self.assertEqual(self.telemetry._model_cache_time, cache_time)

    def test_memory_breakdown(self):
        mem = self.telemetry.get_memory_breakdown()
        self.assertIn("total_gb", mem)
        self.assertIn("used_gb", mem)
        self.assertIn("used_pct", mem)
        self.assertGreaterEqual(mem["total_gb"], 1.0)
        self.assertGreaterEqual(mem["used_pct"], 0)
        self.assertLessEqual(mem["used_pct"], 100)

    def test_context_budget(self):
        ctx = self.telemetry.get_context_budget()
        self.assertEqual(ctx["max_tokens"], 65536)
        self.assertGreater(ctx["used_tokens"], 0)
        self.assertLessEqual(ctx["used_pct"], 100)

    def test_thinking_tokens(self):
        think = self.telemetry.get_thinking_tokens()
        self.assertIn("enabled", think)
        self.assertIn("last_turn_tokens", think)
        self.assertIn("recent_total_tokens", think)
        self.assertTrue(think["enabled"])
        self.assertGreaterEqual(think["last_turn_tokens"], 0)

    def test_single_line_header_format(self):
        hdr = self.telemetry.format_single_line_header(max_width=120)
        self.assertIn("MODEL:", hdr)
        self.assertIn("RAM:", hdr)
        self.assertIn("SPEED:", hdr)
        self.assertIn("Think:", hdr)
        self.assertLessEqual(len(hdr), 130)



class TestEmailTriagePanel(unittest.TestCase):
    def setUp(self):
        self.panel = EmailTriagePanel()

    def test_summary_stats(self):
        stats = self.panel.get_summary_stats()
        self.assertIsInstance(stats, dict)
        self.assertIn("total", stats)
        self.assertIn("urgent", stats)
        self.assertIn("action", stats)

    def test_triage_items(self):
        items = self.panel.get_triage_items(limit=5)
        self.assertIsInstance(items, list)
        if items:
            first = items[0]
            self.assertIn("badge", first)
            self.assertIn("sender", first)
            self.assertIn("subject", first)
            self.assertIn(first["badge"], ["URGENT", "ACTION", "DIGEST", "INFO"])


class TestSafetyMemoryPanel(unittest.TestCase):
    def setUp(self):
        self.panel = SafetyMemoryPanel()

    def test_guard_status(self):
        status = self.panel.get_guard_status()
        self.assertIn("protected_paths", status)
        self.assertIn("trash_substitutions", status)
        self.assertIn("~/.ssh", status["protected_paths"])
        self.assertIn("/System", status["protected_paths"])

    def test_memory_stats(self):
        stats = self.panel.get_memory_stats()
        self.assertIn("total_memories", stats)
        self.assertGreaterEqual(stats["total_memories"], 0)

    def test_recent_events(self):
        events = self.panel.get_recent_events(limit=5)
        self.assertIsInstance(events, list)
        if events:
            ev = events[0]
            self.assertIn("time", ev)
            self.assertIn("badge", ev)
            self.assertIn("type", ev)


class TestCockpitLauncher(unittest.TestCase):
    def setUp(self):
        self.launcher = CockpitLauncher(session_name="test-session")

    def test_tmux_available(self):
        avail = self.launcher.is_tmux_available()
        self.assertTrue(avail, "tmux should be available in test environment")

    def test_build_tmux_config_commands(self):
        cmds = self.launcher.build_tmux_config_commands()
        self.assertIsInstance(cmds, list)
        self.assertGreater(len(cmds), 0)
        flat = [arg for cmd in cmds for arg in cmd]
        self.assertIn("mouse", flat)
        self.assertIn("status-position", flat)
        self.assertIn("top", flat)
        self.assertTrue(any("#1a1b26" in arg for arg in flat))  # Tokyo Night background


if __name__ == "__main__":
    unittest.main()
