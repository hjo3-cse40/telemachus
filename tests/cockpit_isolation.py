"""Keeps cockpit tests off Sam's real data.

CockpitTUI() builds FinanceTuiPanel (finance ledger under ~/.memory/private/finances),
SafetyMemoryPanel (memory vault + its SQLite cache), the email panel and the header telemetry.
Without this, a test that builds the TUI opens and initializes the real vault databases.

Use it in a test module:

    from tests.cockpit_isolation import isolate_module
    setUpModule, tearDownModule = isolate_module()

Every path the TUI reads or creates then lives in one temporary folder.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any, Callable, List, Tuple
from unittest.mock import patch


def isolate_module() -> Tuple[Callable[[], None], Callable[[], None]]:
    """(setUpModule, tearDownModule) that point HOME and every Telemachus data path at a temp dir."""
    state: List[Any] = []

    def set_up() -> None:
        tmp = tempfile.TemporaryDirectory(prefix="telemachus-test-")
        state.append(tmp)
        root = Path(tmp.name)
        env = {
            "HOME": str(root),
            "TELEMACHUS_FINANCE_DIR": str(root / "finances"),
            "TELEMACHUS_SETUP_YAML": str(root / "finances" / "setup.yaml"),
            "TELEMACHUS_MEMORY_DIR": str(root / ".memory"),
            "TELEMACHUS_EMAIL_DB": str(root / "email_index.sqlite3"),
            "TELEMACHUS_TRASH_AUDIT": str(root / "trash_audit.jsonl"),
            "TELEMACHUS_GUARD_LOG": str(root / "guard_events.jsonl"),
        }
        patchers = [patch.dict(os.environ, env)]

        # The memory vault root is read once at import time, so the env var alone is too late.
        from telemachus.cockpit import tui as tui_mod

        real_panel = tui_mod.SafetyMemoryPanel

        def safety_panel(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("vault_root", root / ".memory")
            if kwargs.get("audit_log") is None:
                kwargs["audit_log"] = root / "trash_audit.jsonl"
            kwargs.setdefault("guard_events", root / "guard_events.jsonl")
            return real_panel(*args, **kwargs)

        patchers.append(patch.object(tui_mod, "SafetyMemoryPanel", safety_panel))
        for p in patchers:
            p.start()
            state.append(p)

    def tear_down() -> None:
        while state:
            item = state.pop()
            if hasattr(item, "stop"):
                item.stop()
            else:
                item.cleanup()

    return set_up, tear_down


_MCP_TMP: List[str] = []


def import_mcp_server() -> Any:
    """telemachus.mcp.server, imported with its email index in a temp folder.

    The module builds EmailIndexStore() at import time, which opens (and creates tables in) the
    repo's data/email_index.sqlite3, the real mail index. If another test module imported it
    first, it is returned as is.
    """
    import atexit
    import shutil
    import sys

    name = "telemachus.mcp.server"
    if name not in sys.modules:
        tmp = tempfile.mkdtemp(prefix="telemachus-mcp-")
        _MCP_TMP.append(tmp)
        if len(_MCP_TMP) == 1:
            atexit.register(lambda: [shutil.rmtree(t, ignore_errors=True) for t in _MCP_TMP])
        with patch("telemachus.email.store.find_project_root", return_value=Path(tmp)):
            import telemachus.mcp.server  # noqa: F401
    return sys.modules[name]
