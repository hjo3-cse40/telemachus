"""Tests for the synthetic demo vault (finance seed-demo) and vault-env forwarding."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.launcher import forwarded_env_prefix
from telemachus.finance.demo import UnsafeDemoTargetError, seed_demo
from telemachus.finance.store import FinanceStore

TODAY = date(2026, 10, 28)


class TestSeedDemo(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="telemachus_demo_"))
        self.target = self.root / "demo"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_creates_fake_vault_with_history(self):
        info = seed_demo(self.target, today=TODAY)
        self.assertGreater(info["transactions"], 40)
        store = FinanceStore(root_dir=self.target)
        balances = store.get_all_balances()
        self.assertTrue(balances)
        self.assertGreater(store.get_account_balance("Assets:Checking:WellsFargo"), 0)

    def test_deterministic_for_the_same_day(self):
        a = seed_demo(self.root / "a", today=TODAY)
        b = seed_demo(self.root / "b", today=TODAY)
        self.assertEqual(a["transactions"], b["transactions"])
        self.assertEqual(
            FinanceStore(root_dir=self.root / "a").get_account_balance("Assets:Checking:WellsFargo"),
            FinanceStore(root_dir=self.root / "b").get_account_balance("Assets:Checking:WellsFargo"),
        )

    def test_cockpit_runway_works_on_demo_data(self):
        seed_demo(self.target, today=TODAY)
        panel = FinanceTuiPanel(store=FinanceStore(root_dir=self.target))
        runway = panel.get_runway(today=TODAY.isoformat())
        self.assertIsNotNone(runway)

    def test_refuses_existing_vault_without_force(self):
        seed_demo(self.target, today=TODAY)
        with self.assertRaises(FileExistsError):
            seed_demo(self.target, today=TODAY)
        seed_demo(self.target, today=TODAY, force=True)

    def test_refuses_the_real_vault_even_with_force(self):
        fake_home = self.root / "home"
        real = fake_home / ".memory" / "private" / "finances"
        with patch("telemachus.finance.demo.Path.home", return_value=fake_home):
            for bad in (real, real / "nested", fake_home / ".memory"):
                with self.assertRaises(UnsafeDemoTargetError):
                    seed_demo(bad, today=TODAY, force=True)
        self.assertFalse(real.exists())

    def test_refuses_the_env_configured_vault(self):
        with patch.dict("os.environ", {"TELEMACHUS_FINANCE_DIR": str(self.target)}):
            with self.assertRaises(UnsafeDemoTargetError):
                seed_demo(self.target, today=TODAY, force=True)


class TestForwardedEnv(unittest.TestCase):
    def test_empty_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(forwarded_env_prefix(), "")

    def test_forwards_and_quotes_vault_vars(self):
        with patch.dict("os.environ", {"TELEMACHUS_FINANCE_DIR": "/tmp/my demo"}, clear=True):
            self.assertEqual(forwarded_env_prefix(), "env TELEMACHUS_FINANCE_DIR='/tmp/my demo' ")


if __name__ == "__main__":
    unittest.main()
