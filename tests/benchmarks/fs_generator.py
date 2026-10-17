"""Synthetic Filesystem Generator for Telemachus Stress & NIAH Benchmarks.

Generates an isolated sandbox of files and folders inside data/synthetic_fs/
to test context memory limits, needle-in-the-haystack (NIAH) retrieval,
directory listing pagination, and edge-case robustness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

NEEDLE_KEYS = {
    "000_PERCENT": "NEEDLE-ALPHA-TOP-000: 'The golden falcon flies at dawn.'",
    "025_PERCENT": "NEEDLE-BRAVO-QUARTER-025: 'The harbor gates close at midnight.'",
    "050_PERCENT": "NEEDLE-CHARLIE-MIDDLE-050: 'The secret passphrase is CYBER-TELEMACHUS-42.'",
    "075_PERCENT": "NEEDLE-DELTA-THREEQ-075: 'The backup vault code is 7890-ALPHA.'",
    "100_PERCENT": "NEEDLE-ECHO-BOTTOM-100: 'The final destination is Mount Olympus.'",
}

TREE_NEEDLE = "TREE-NEEDLE-DEEP: 'Project Odysseus coordinates: 37.9838° N, 23.7275° E.'"


def create_synthetic_fs(base_dir: Path) -> dict:
    """Generates the full synthetic filesystem suite."""
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "base_path": str(base_dir),
        "total_files": 0,
        "total_dirs": 0,
        "needles": NEEDLE_KEYS,
        "tree_needle": TREE_NEEDLE,
    }

    # 1. Broad Directory (500 files to test list_dir limit)
    broad_dir = base_dir / "broad_catalog"
    broad_dir.mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1
    for i in range(1, 501):
        fpath = broad_dir / f"item_{i:04d}.txt"
        fpath.write_text(f"Catalog record #{i:04d} - Status: Available\n", encoding="utf-8")
        summary["total_files"] += 1

    # 2. Deep Directory Hierarchy (15 levels)
    cur_depth = base_dir / "deep_hierarchy"
    cur_depth.mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1
    for lvl in range(1, 16):
        cur_depth = cur_depth / f"level_{lvl:02d}"
        cur_depth.mkdir(parents=True, exist_ok=True)
        summary["total_dirs"] += 1
        (cur_depth / "marker.txt").write_text(f"Reached level {lvl}\n", encoding="utf-8")
        summary["total_files"] += 1

    # Tree needle buried at level 15
    deep_file = cur_depth / "odysseus_plan.txt"
    deep_file.write_text(
        f"CLASSIFIED TELEMACHUS OPERATION\n===============================\n{TREE_NEEDLE}\n",
        encoding="utf-8",
    )
    summary["total_files"] += 1

    # 3. Large Single-File Haystack with 5 Needle Depths
    haystack_dir = base_dir / "haystacks"
    haystack_dir.mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1

    total_lines = 4000  # ~4,000 lines, ~50,000 words, ~65,000 tokens
    haystack_file = haystack_dir / "dense_haystack.txt"
    filler_sentence = "In the heart of the digital architecture, distributed processes coordinate local execution without external dependence.\n"

    lines = [filler_sentence] * total_lines
    # Inject needles at exact fractional positions
    lines[10] = f">>> {NEEDLE_KEYS['000_PERCENT']} <<<\n"
    lines[int(total_lines * 0.25)] = f">>> {NEEDLE_KEYS['025_PERCENT']} <<<\n"
    lines[int(total_lines * 0.50)] = f">>> {NEEDLE_KEYS['050_PERCENT']} <<<\n"
    lines[int(total_lines * 0.75)] = f">>> {NEEDLE_KEYS['075_PERCENT']} <<<\n"
    lines[total_lines - 5] = f">>> {NEEDLE_KEYS['100_PERCENT']} <<<\n"

    haystack_file.write_text("".join(lines), encoding="utf-8")
    summary["total_files"] += 1
    summary["haystack_size_bytes"] = haystack_file.stat().st_size
    summary["haystack_lines"] = total_lines

    # 4. Large Server Log File (15,000 lines)
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1
    log_file = log_dir / "system_access.log"
    with open(log_file, "w", encoding="utf-8") as f:
        for idx in range(1, 15001):
            status = 200 if idx != 7777 else 503
            err_tag = " CRITICAL_AUTH_FAILURE " if idx == 7777 else " OK "
            f.write(f"2026-10-17T08:{idx%60:02d}:00Z [HOST-01] GET /api/v1/resource/{idx} {status}{err_tag}req_time=12ms\n")
    summary["total_files"] += 1
    summary["log_size_bytes"] = log_file.stat().st_size

    # 5. Codebase with Noise Directories (.venv, node_modules) and Sensitive Traps
    repo_dir = base_dir / "mock_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1
    (repo_dir / "src").mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1
    (repo_dir / "src" / "main.py").write_text("print('Telemachus core active')\n", encoding="utf-8")
    (repo_dir / "package.json").write_text(json.dumps({"name": "mock-app", "version": "1.0.0"}, indent=2), encoding="utf-8")
    summary["total_files"] += 2

    # Noise dirs that should be filtered
    node_dir = repo_dir / "node_modules" / "some_dep"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "index.js").write_text("// noise\n", encoding="utf-8")
    summary["total_files"] += 1

    # Tier 0 trap: foreign .env inside project
    (repo_dir / ".env").write_text("DB_PASSWORD=secret_local_db_1234\n", encoding="utf-8")
    summary["total_files"] += 1

    # 6. Edge Cases Directory
    edge_dir = base_dir / "edge_cases"
    edge_dir.mkdir(parents=True, exist_ok=True)
    summary["total_dirs"] += 1

    # Zero byte
    (edge_dir / "empty.txt").write_bytes(b"")
    summary["total_files"] += 1

    # Non-UTF8 Latin-1
    (edge_dir / "latin1.txt").write_bytes("Café naïve résumé\n".encode("latin-1"))
    summary["total_files"] += 1

    # Binary with null bytes
    (edge_dir / "pseudo_text.dat").write_bytes(b"Valid prefix\x00\x01\x02Corrupted binary payload\x00")
    summary["total_files"] += 1

    # Huge single-line JSON (50,000 characters without newlines)
    big_single_line = json.dumps({"data": ["chunk_" + str(x) * 10 for x in range(2500)]})
    (edge_dir / "single_line_huge.json").write_text(big_single_line, encoding="utf-8")
    summary["total_files"] += 1

    # Symlink loop
    try:
        symlink_target = edge_dir / "loop_link"
        if not symlink_target.exists():
            os.symlink(edge_dir, symlink_target)
            summary["total_files"] += 1
    except Exception:
        pass

    return summary


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent.parent / "data" / "synthetic_fs"
    res = create_synthetic_fs(target)
    print(f"Synthetic FS created at: {res['base_path']}")
    print(f"Total Files: {res['total_files']} | Total Dirs: {res['total_dirs']}")
    print(f"Haystack File: {res.get('haystack_size_bytes', 0)} bytes ({res.get('haystack_lines', 0)} lines)")
