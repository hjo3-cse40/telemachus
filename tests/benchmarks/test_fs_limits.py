"""Benchmark Suite for Telemachus Filesystem: Context Limits, NIAH, and Robustness."""
from __future__ import annotations

import json
from pathlib import Path
import time
import unittest

from telemachus.fs.boundaries import evaluate_tier0_path
from telemachus.fs.explorer import grep_text, is_binary_file, list_dir, read_file, search_files
from tests.benchmarks.fs_generator import NEEDLE_KEYS, TREE_NEEDLE

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "synthetic_fs"
HAYSTACK_FILE = DATA_DIR / "haystacks" / "dense_haystack.txt"


class TestFilesystemLimitsAndNIAH(unittest.TestCase):

    def test_fs_read_truncation_limits(self):
        """Verify that fs_read enforces line windowing and byte size caps."""
        res = read_file(str(HAYSTACK_FILE), start_line=1, end_line=None)
        self.assertNotIn("error", res)
        self.assertEqual(res["total_lines"], 4000)
        # Window limit: default window should cap at 300 lines
        self.assertEqual(res["end_line"], 301)
        self.assertIn("content", res)

        # Max bytes cap verification
        large_res = read_file(str(HAYSTACK_FILE), start_line=1, end_line=1500, max_bytes=32000)
        self.assertLessEqual(len(large_res["content"].encode("utf-8")), 32100)
        self.assertIn("[truncated due to size limit]", large_res["content"])

    def test_fs_read_pagination(self):
        """Verify that pagination can traverse across line windows sequentially."""
        step = 300
        total_lines = 4000
        seen_lines = 0

        for start in range(1, 901, step):
            res = read_file(str(HAYSTACK_FILE), start_line=start, end_line=start + step - 1)
            self.assertNotIn("error", res)
            self.assertEqual(res["start_line"], start)
            self.assertEqual(res["end_line"], min(total_lines, start + step - 1))
            seen_lines += (res["end_line"] - res["start_line"] + 1)

        self.assertEqual(seen_lines, 900)

    def test_niah_intra_file_with_grep(self):
        """Test Needle-in-a-Haystack retrieval using grep_text across all depths: 0%, 25%, 50%, 75%, 100%."""
        for depth_key, needle_val in NEEDLE_KEYS.items():
            token = needle_val.split(":")[0].strip()
            t0 = time.perf_counter()
            res = grep_text(token, root_dir=str(DATA_DIR / "haystacks"))
            elapsed = time.perf_counter() - t0

            self.assertNotIn("error", res)
            self.assertEqual(res["total_matches"], 1, f"Failed needle at depth {depth_key}")
            self.assertIn(token, res["matches"][0]["line_text"])
            self.assertLess(elapsed, 0.1, f"Grep too slow: {elapsed:.4f}s")

    def test_niah_deep_tree_traversal(self):
        """Test retrieving needle buried 15 levels deep across directory hierarchy."""
        needle_token = "TREE-NEEDLE-DEEP"

        # Search by content using grep
        t0 = time.perf_counter()
        grep_res = grep_text(needle_token, root_dir=str(DATA_DIR / "deep_hierarchy"))
        elapsed = time.perf_counter() - t0

        self.assertEqual(grep_res["total_matches"], 1)
        match = grep_res["matches"][0]
        self.assertIn("odysseus_plan.txt", match["file"])
        self.assertIn("Project Odysseus", match["line_text"])

        # Search by filename
        search_res = search_files("odysseus_plan.txt", root_dir=str(DATA_DIR / "deep_hierarchy"))
        self.assertEqual(search_res["match_count"], 1)
        self.assertIn("level_15", search_res["matches"][0]["full_path"])

    def test_list_dir_noise_suppression_and_limit(self):
        """Verify that list_dir respects max entries and filters out vendor noise."""
        broad_res = list_dir(str(DATA_DIR / "broad_catalog"), limit=50)
        self.assertEqual(broad_res["total_returned"], 50)

        # Test noise suppression: node_modules should be ignored
        repo_res = list_dir(str(DATA_DIR / "mock_repo"), max_depth=4)
        file_names = [e["name"] for e in repo_res["entries"]]
        self.assertIn("main.py", file_names)
        self.assertNotIn("node_modules", file_names)
        self.assertNotIn("some_dep", file_names)

    def test_tier0_boundary_protection(self):
        """Verify that Tier 0 blocks write/edit attempts to .env and system paths."""
        env_path = DATA_DIR / "mock_repo" / ".env"
        ok, reason = evaluate_tier0_path(env_path, is_write=True)
        self.assertFalse(ok)
        self.assertIn("tier 0 violation", reason.lower())

        sys_ok, sys_reason = evaluate_tier0_path(Path("/System/Library"), is_write=False)
        self.assertFalse(sys_ok)
        self.assertIn("tier 0 violation", sys_reason.lower())

    def test_pathological_file_handling(self):
        """Verify robust handling of zero-byte, binary, latin-1, and single-line huge files."""
        edge_dir = DATA_DIR / "edge_cases"

        # Zero byte
        res_empty = read_file(str(edge_dir / "empty.txt"))
        self.assertEqual(res_empty["total_lines"], 0)
        self.assertEqual(res_empty["content"], "")

        # Binary with null bytes
        self.assertTrue(is_binary_file(edge_dir / "pseudo_text.dat"))
        res_bin = read_file(str(edge_dir / "pseudo_text.dat"))
        self.assertIn("error", res_bin)
        self.assertIn("Binary file detected", res_bin["error"])

        # Latin-1 encoded file
        res_latin = read_file(str(edge_dir / "latin1.txt"))
        self.assertNotIn("error", res_latin)
        self.assertIn("Caf", res_latin["content"])

        # Single-line huge JSON
        res_huge = read_file(str(edge_dir / "single_line_huge.json"), max_bytes=5000)
        self.assertLessEqual(len(res_huge["content"]), 5100)
        self.assertIn("[truncated due to size limit]", res_huge["content"])


if __name__ == "__main__":
    unittest.main()
