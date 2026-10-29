"""Comprehensive Test Suite for Telemachus Filesystem Navigation & Laya Guard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from telemachus.fs.boundaries import (
    evaluate_tier0_command,
    evaluate_tier0_path,
    is_sensitive_secret,
    is_system_path,
    resolve_clean_path,
)
from telemachus.fs.checkpoint import create_git_checkpoint, find_git_root
from telemachus.fs import editor as editor_module
from telemachus.fs.editor import edit_file, run_command, safe_delete, write_file
from telemachus.fs.explorer import grep_text, list_dir, read_file, search_files, stat_path
from telemachus.fs.guard import LayaGuard
from telemachus.fs.models import FileActionType, GuardVerdict, TierLevel
from telemachus.fs.trash import move_to_trash
from telemachus.mcp.server import TOOLS_DEFINITIONS, handle_tool_call
from telemachus.fs import guard_log

# Guard events and trash audit lines from these tests go to a scratch folder, never to
# ~/.memory or data/trash_audit.jsonl.
_SCRATCH = None
_OLD_ENV = {}


def setUpModule():
    global _SCRATCH
    _SCRATCH = tempfile.TemporaryDirectory()
    for key, value in (("TELEMACHUS_GUARD_LOG", str(Path(_SCRATCH.name) / "guard_events.jsonl")),):
        _OLD_ENV[key] = os.environ.get(key)
        os.environ[key] = value


def tearDownModule():
    for key, value in _OLD_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if _SCRATCH is not None:
        _SCRATCH.cleanup()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", "-c", "commit.gpgsign=false",
         "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


class TestTier0Boundaries(unittest.TestCase):
    """Verifies zero-cost deterministic path and command restrictions."""

    def test_system_path_blocks(self):
        self.assertTrue(is_system_path("/System"))
        self.assertTrue(is_system_path("/System/Library"))
        self.assertTrue(is_system_path("/private/etc/hosts"))
        self.assertTrue(is_system_path("/usr/bin/python3"))

        # Scratch/temp directories must NOT be blocked
        self.assertFalse(is_system_path("/tmp"))
        self.assertFalse(is_system_path("/tmp/sample.txt"))
        self.assertFalse(is_system_path("/private/tmp/sample.txt"))

    def test_sensitive_secret_blocks(self):
        ssh_key = Path.home() / ".ssh" / "id_rsa"
        aws_creds = Path.home() / ".aws" / "credentials"
        gnupg = Path.home() / ".gnupg" / "secring.gpg"
        history = Path.home() / ".zsh_history"

        self.assertTrue(is_sensitive_secret(ssh_key))
        self.assertTrue(is_sensitive_secret(aws_creds))
        self.assertTrue(is_sensitive_secret(gnupg))
        self.assertTrue(is_sensitive_secret(history))

        # Project files are not sensitive secrets
        self.assertFalse(is_sensitive_secret(Path.cwd() / "README.md"))

    def test_evaluate_tier0_path_violations(self):
        ok, reason = evaluate_tier0_path("~/.ssh/id_rsa", is_write=False)
        self.assertFalse(ok)
        self.assertIn("sensitive security credential", reason)

        ok, reason = evaluate_tier0_path("/System/Library/CoreServices", is_write=True)
        self.assertFalse(ok)
        self.assertIn("system directory", reason)

    def test_forbidden_commands(self):
        bad_cmds = [
            "mkfs.ext4 /dev/sda1",
            "fdisk /dev/disk0",
            "dd if=/dev/zero of=/dev/rdisk1",
            "chmod -R 777 /",
            ":(){ :|:& };:",
            "rm -rf /",
            "rm -rf ~/",
        ]
        for cmd in bad_cmds:
            ok, reason = evaluate_tier0_command(cmd)
            self.assertFalse(ok, f"Command '{cmd}' should have been blocked!")

        safe_cmds = [
            "git status",
            "python3 -m unittest",
            "npm test",
            "ls -la",
            "cargo build",
        ]
        for cmd in safe_cmds:
            ok, reason = evaluate_tier0_command(cmd)
            self.assertTrue(ok, f"Safe command '{cmd}' should have passed: {reason}")


class TestTier2TrashRecovery(unittest.TestCase):
    """Verifies safe deletion via macOS Trash."""

    def setUp(self):
        self.test_file = Path("/tmp") / f"telemachus_trash_test_{os.getpid()}.txt"
        self.test_file.write_text("trash safety test payload", encoding="utf-8")

    def tearDown(self):
        if self.test_file.exists():
            self.test_file.unlink()

    def test_move_to_trash_success(self):
        self.assertTrue(self.test_file.exists())
        audit = Path(_SCRATCH.name) / "trash_audit.jsonl"
        result = move_to_trash(self.test_file, audit_log=audit)
        self.assertTrue(result.success)
        self.assertFalse(self.test_file.exists())
        self.assertIn("macOS Trash", result.message)
        kinds = [e["kind"] for e in guard_log.read_recent(5)]
        self.assertIn("TRASH", kinds)

    def test_nonexistent_trash_fails_gracefully(self):
        fake = Path("/tmp/does_not_exist_9999.xyz")
        res = move_to_trash(fake)
        self.assertFalse(res.success)


class TestGitCheckpoint(unittest.TestCase):
    """Verifies automatic git checkpointing mechanism."""

    def test_find_git_root(self):
        root = find_git_root(Path.cwd() / "telemachus" / "fs" / "guard.py")
        self.assertIsNotNone(root)
        self.assertTrue((root / ".git").is_dir())

    def test_checkpoint_execution(self):
        # A throwaway repo: a checkpoint in the project repo would add a stash entry there.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            (repo / "a.txt").write_text("one\n", encoding="utf-8")
            _git(repo, "add", "a.txt")
            _git(repo, "commit", "-q", "-m", "init")
            res = create_git_checkpoint(repo, message="Unit test checkpoint")
            self.assertTrue(res.success)
            self.assertIn("Git", res.message)
            (repo / "a.txt").write_text("two\n", encoding="utf-8")
            res = create_git_checkpoint(repo, message="Unit test checkpoint")
            self.assertTrue(res.success)
            self.assertTrue(res.checkpoint_id)
            events = [e for e in guard_log.read_recent(10) if e["kind"] == "CHECKPOINT"]
            self.assertTrue(events)
            self.assertIn(res.checkpoint_id[:7], events[0]["target"])


class TestFilesystemExplorer(unittest.TestCase):
    """Verifies directory listing, file viewing, and code search."""

    def test_list_dir(self):
        res = list_dir("telemachus/fs", max_depth=1)
        self.assertNotIn("error", res)
        self.assertGreater(res["total_returned"], 0)
        names = [e["name"] for e in res["entries"]]
        self.assertIn("guard.py", names)
        self.assertIn("explorer.py", names)
        self.assertIn("editor.py", names)

    def test_read_file_windowing(self):
        res = read_file("telemachus/fs/models.py", start_line=1, end_line=10)
        self.assertNotIn("error", res)
        self.assertEqual(res["start_line"], 1)
        self.assertEqual(res["end_line"], 10)
        self.assertIn("FileActionType", res["content"])

    def test_read_binary_file_suppressed(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01\x02\x03\xff\xfe")
            tmp_path = f.name
        try:
            res = read_file(tmp_path)
            self.assertIn("error", res)
            self.assertIn("Binary file detected", res["error"])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_search_files(self):
        res = search_files("boundaries", root_dir="telemachus/fs")
        self.assertNotIn("error", res)
        self.assertGreaterEqual(res["match_count"], 1)
        self.assertTrue(any("boundaries.py" in m["name"] for m in res["matches"]))

    def test_grep_text(self):
        res = grep_text("GuardVerdict", root_dir="telemachus/fs", file_pattern="*.py")
        self.assertNotIn("error", res)
        self.assertGreater(res["total_matches"], 0)
        self.assertTrue(any("models.py" in m["file"] for m in res["matches"]))

    def test_stat_path(self):
        res = stat_path("README.md")
        self.assertNotIn("error", res)
        self.assertTrue(res["is_file"])
        self.assertGreater(res["size_bytes"], 0)


class _AlignedBridge:
    """Stands in for the Laya worker: every request matches the stated intent.

    The editor tests check the editor (atomic edit, run, output), not the model. The real model
    scores some harmless commands near its threshold (echo in a temp folder came back as
    "destructive deletion 0.48"), which made these tests pass or fail by chance.
    """

    calls = 0

    def evaluate_record(self, action_type, target, user_intent, details=""):
        type(self).calls += 1
        return {"intent_alignment": "aligned", "alignment_confidence": 0.95,
                "blast_radius": "contained", "action_risk": "safe"}


class TestGuardedEditor(unittest.TestCase):
    """Verifies atomic editing, writing, and safe deletions."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_path = Path(self.test_dir.name) / "test_doc.txt"
        self.test_path.write_text("Hello World\nLine 2\nLine 3\n", encoding="utf-8")
        # cwd stays the temp folder (never the project repo: run_command may take a checkpoint)
        patcher = mock.patch.object(editor_module._guard, "bridge", _AlignedBridge())
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_atomic_edit_file(self):
        res = edit_file(
            str(self.test_path),
            target_content="Line 2",
            replacement_content="Line 2 Edited",
            user_intent="Update line 2 text in test document",
        )
        self.assertTrue(res["success"])
        content = self.test_path.read_text(encoding="utf-8")
        self.assertIn("Line 2 Edited", content)
        self.assertNotIn("Line 2\n", content)

    def test_edit_file_target_not_found(self):
        res = edit_file(
            str(self.test_path),
            target_content="Nonexistent line content",
            replacement_content="Replacement",
        )
        self.assertFalse(res["success"])
        self.assertIn("target_content not found", res["error"])

    def test_write_file_overwrite_protection(self):
        # Attempt overwrite without flag
        res1 = write_file(str(self.test_path), "New content", overwrite=False)
        self.assertFalse(res1["success"])
        self.assertIn("already exists", res1["error"])

        # With overwrite=True
        res2 = write_file(str(self.test_path), "New content", overwrite=True)
        self.assertTrue(res2["success"])
        self.assertEqual(self.test_path.read_text(encoding="utf-8"), "New content")

    def test_guarded_run_command_safe(self):
        before = _AlignedBridge.calls
        res = run_command("echo 'hello telemachus'", cwd=self.test_dir.name, user_intent="test echo")
        self.assertGreater(_AlignedBridge.calls, before)  # the intent check still ran
        self.assertTrue(res["success"])
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("hello telemachus", res["stdout"])

    def test_guarded_run_command_blocked(self):
        res = run_command("mkfs /dev/sda1", cwd=self.test_dir.name, user_intent="format drive")
        self.assertFalse(res["success"])
        self.assertIn("blocked by guard", res["error"])
        last = guard_log.read_recent(1)[0]
        self.assertEqual((last["kind"], last["rule"]), ("BLOCK", "command shield"))


class TestMCPServerIntegration(unittest.TestCase):
    """Verifies JSON-RPC tool schemas and dispatching for fs tools."""

    def test_fs_tools_registered(self):
        names = {t["name"] for t in TOOLS_DEFINITIONS}
        expected_fs_tools = {
            "fs_list",
            "fs_read",
            "fs_search",
            "fs_grep",
            "fs_stat",
            "fs_edit",
            "fs_write",
            "fs_delete",
            "fs_run",
            "fs_checkpoint",
        }
        for tool in expected_fs_tools:
            self.assertIn(tool, names)

    def test_fs_list_tool_call(self):
        out = handle_tool_call("fs_list", {"path": "telemachus/fs", "depth": 1})
        data = json.loads(out)
        self.assertIn("entries", data)
        self.assertGreater(len(data["entries"]), 0)

    def test_fs_read_tool_call(self):
        out = handle_tool_call("fs_read", {"path": "telemachus/fs/models.py", "start_line": 1, "end_line": 15})
        self.assertIn("File:", out)
        self.assertIn("FileActionType", out)

    def test_fs_search_tool_call(self):
        out = handle_tool_call("fs_search", {"pattern": "boundaries", "root_dir": "telemachus/fs"})
        data = json.loads(out)
        self.assertGreaterEqual(data["match_count"], 1)

    def test_fs_stat_tool_call(self):
        out = handle_tool_call("fs_stat", {"path": "README.md"})
        data = json.loads(out)
        self.assertTrue(data["is_file"])
        self.assertGreater(data["size_bytes"], 0)

    def test_fs_run_tool_call(self):
        # cwd outside any repo: "mcp" contains "cp", which makes run_command take a git checkpoint.
        with tempfile.TemporaryDirectory() as tmp:
            out = handle_tool_call("fs_run", {"command": "echo 'mcp fs test'", "cwd": tmp})
        data = json.loads(out)
        self.assertTrue(data["success"])
        self.assertIn("mcp fs test", data["stdout"])


if __name__ == "__main__":
    unittest.main()
