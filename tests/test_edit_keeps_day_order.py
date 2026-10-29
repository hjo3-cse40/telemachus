"""Editing an entry keeps its place among the day's entries, so the Visa math does not change
order (a charge logged before a payment stays before it). Synthetic vault only."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore
from tests.test_credit_card_flow import SETUP_YAML

DAY, TODAY = "2026-10-25", "2026-10-28"


class TestEditKeepsDayOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "fin"
        root.mkdir()
        (root / "setup.yaml").write_text(SETUP_YAML, encoding="utf-8")
        self.store = FinanceStore(root_dir=root)
        apply_setup_yaml(yaml_path=root / "setup.yaml", store=self.store)
        self.panel = FinanceTuiPanel(store=self.store)

    def card(self):
        c = self.panel.get_card_summary(today=TODAY)
        return c["owed_cents"], c["charged_since_last_payment_cents"]

    def check_edit_keeps_order(self, same_batch: bool):
        if same_batch:
            groceries = self.panel.log_day_bullets(DAY, "groceries 40 visa\npay visa 40")[0]
        else:
            groceries = self.panel.log_day_bullets(DAY, "groceries 40 visa")[0]
            time.sleep(1.1)  # a later second, so created_at alone orders them
            self.panel.log_day_bullets(DAY, "pay visa 40")
        owed0, _ = self.card()
        time.sleep(1.1)
        new = self.panel.edit_transaction(groceries.id, DAY, "- groceries - 45 visa", "Visa")
        self.assertIsNotNone(new)
        owed1, since1 = self.card()
        # Charged 45 before a 40 payment: 5 still owed from before the payment, nothing charged since.
        self.assertEqual(owed1 - owed0, 500)
        self.assertEqual(since1, 0)
        self.assertIsNone(self.store.get_transaction(groceries.id))

    def test_edit_keeps_order_across_seconds(self):
        self.check_edit_keeps_order(same_batch=False)

    def test_edit_keeps_order_inside_one_batch(self):
        self.check_edit_keeps_order(same_batch=True)

    def test_moving_an_entry_to_another_day_counts_as_new_there(self):
        groceries = self.panel.log_day_bullets(DAY, "groceries 40 visa")[0]
        new = self.panel.edit_transaction(groceries.id, "2026-10-26", "- groceries - 40 visa", "Visa")
        self.assertEqual(new.date, "2026-10-26")


if __name__ == "__main__":
    unittest.main()
