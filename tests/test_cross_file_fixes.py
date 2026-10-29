"""Cross-file fixes: setup.yaml error on Money and Home, panel line preview, savings -> checking
sign, card-paid custom bill, UTF-8 memory search, non-ASCII card purchases, lazy MCP index."""
from datetime import date
import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telemachus.cockpit import finance_modals as fm  # noqa: E402
from telemachus.cockpit import home_view, memory_view  # noqa: E402
from telemachus.cockpit.finance_view import FinanceViewData, hero_lines  # noqa: E402
from telemachus.finance.import_text import parse_bullet_line  # noqa: E402


def _text(lines):
    return "\n".join("".join(seg[0] for seg in line) for line in lines)


ERR = "setup.yaml line 4: bad indent"


class SetupErrorShown(unittest.TestCase):
    def test_money_hero_shows_the_error(self):
        d = FinanceViewData(None, 0, 0, 0, 0, [], [], None, None, setup_error=ERR)
        out = _text(hero_lines(d, 90))
        self.assertIn("▲ " + ERR, out)
        self.assertIn("fix setup.yaml to see the money until payday", out)
        self.assertNotIn("No paycheck plan", out)
        styles = {seg[1] for line in hero_lines(d, 90) for seg in line if seg[0].strip()}
        self.assertEqual(styles, {"yellow"})

    def test_hero_wraps_at_narrow_width(self):
        d = FinanceViewData(None, 0, 0, 0, 0, [], [], None, None, setup_error=ERR)
        lines = hero_lines(d, 40)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(sum(len(s[0]) for s in ln) <= 40 for ln in lines))

    def test_no_plan_hint_without_error(self):
        d = FinanceViewData(None, 0, 0, 0, 0, [], [], None, None)
        self.assertIn("No paycheck plan yet", _text(hero_lines(d, 90)))

    def test_home_money_block_shows_the_error(self):
        m = {"available": False, "mask": False, "setup_error": ERR}
        out = _text(home_view.money_block(m, 90, date(2026, 10, 28)))
        self.assertIn("MONEY", out)
        self.assertIn("▲ " + ERR, out)
        self.assertNotIn("No finance data yet", out)


class _Panel:
    def __init__(self):
        self.matched = []

    def preview_bullet_line(self, line):
        from telemachus.finance.import_text import preview_bullet_line

        out = preview_bullet_line(line)
        if out.get("ok") and out.get("category_label") == "Discretionary" and "phone" in line:
            out["category_label"] = "Phone"
        return out

    def match_bill(self, item):
        self.matched.append(item.get("payee"))

    def log_day_bullets(self, date_str, text):
        return []


class LinePreviewFixes(unittest.TestCase):
    def test_panel_preview_names_custom_bill(self):
        self.assertEqual(fm.preview_line("phone bill 80", panel=_Panel()).category, "Phone")
        self.assertNotEqual(fm.preview_line("phone bill 80").category, "Phone")

    def test_panel_without_dict_falls_back(self):
        p = fm.preview_line("groceries 25", panel=mock.MagicMock())
        self.assertTrue(p.ok)
        self.assertEqual(p.amount_cents, -2500)

    def test_savings_to_checking_reads_as_money_in(self):
        p = fm.preview_line("savings to checking 100")
        self.assertEqual(p.kind, "transfer")
        self.assertTrue(p.to_checking)
        self.assertEqual(p.amount_cents, 10000)
        into = fm.preview_line("savings 100")
        self.assertFalse(into.to_checking)
        self.assertEqual(into.amount_cents, -10000)

    def test_totals_split_to_savings_and_to_checking(self):
        good = [fm.preview_line("savings to checking 100"), fm.preview_line("savings 50")]
        out = _text([fm.FinanceModals._totals(good, 200)])
        self.assertIn("$50.00 to savings", out)
        self.assertIn("$100.00 to checking", out)

    def test_direct_visa_path_matches_bill(self):
        panel = _Panel()
        panel.store = mock.MagicMock()
        panel.store.log_batch_transactions.return_value = []
        modals = fm.FinanceModals(lambda: panel)
        p = fm.LinePreview(text="phone bill 80", ok=True, kind="spend", account="Visa", amount_cents=-8000,
                           defaulted=True)
        with mock.patch.object(fm, "preview_line", return_value=fm.LinePreview(text="", ok=False)):
            modals._log_lines("2026-10-28", [p])
        self.assertEqual(panel.matched, ["phone bill"])
        items = panel.store.log_batch_transactions.call_args[0][0]
        self.assertEqual(items[0]["from_account"], "Liabilities:CreditCard:Visa")


class MemorySearchUtf8(unittest.TestCase):
    def test_accents_and_korean_are_typed(self):
        class D:
            facts = []
            memory_stats = {}
            searcher = None

        st = {"searching": True, "query": ""}
        for ch in "café 커피".encode("utf-8"):
            memory_view.handle_key(ch, st, D())
        self.assertEqual(st["query"], "café 커피")
        for ch in "\U0001F600".encode("utf-8"):  # emoji: dropped
            memory_view.handle_key(ch, st, D())
        self.assertEqual(st["query"], "café 커피")


class NonAsciiCardPurchase(unittest.TestCase):
    def test_korean_description_with_visa_is_a_purchase(self):
        for line in ("커피 4.50 visa", "커피 visa 4.50", "커피 paid visa 4.50", "café 4 visa"):
            item = parse_bullet_line(line)
            self.assertEqual(item["kind"], "spend", line)
            self.assertEqual(item["account_short"], "Visa", line)

    def test_plain_card_payments_stay_payments(self):
        for line in ("visa 200", "paid visa 200", "pay visa 300", "visa payment 200", "credit card 300"):
            self.assertEqual(parse_bullet_line(line)["kind"], "card_payment", line)


class McpIndexIsLazy(unittest.TestCase):
    def test_import_does_not_open_the_index(self):
        with mock.patch("telemachus.email.store.EmailIndexStore.__init__", side_effect=AssertionError("opened")):
            mod = importlib.import_module("telemachus.mcp.server")
            importlib.reload(mod)
        self.assertIsNone(mod._INDEX_STORE)


if __name__ == "__main__":
    unittest.main()
