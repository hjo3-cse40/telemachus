"""Bridge to the Laya memory intake worker (telemachus/memory/laya_worker.py).

The worker runs in the Laya venv. It uses about 2.4 GB (measured on the M5 Pro), so it
starts on first use and stops after IDLE_SECONDS without a request. Every call has a
timeout; a hung or dead worker raises LayaUnavailable instead of blocking the caller.

Measured on tests/fixtures/memory_intake_eval.json (zero-shot, no fine-tune):
- durability: close to chance. Only a low cut-off is safe (intake.DURABILITY_SKIP_BELOW).
- authorship: weak; only a very high p_own is safe to trust (intake.OWN_UPGRADE_AT).
- Hangul text: both checkpoints are at chance, so Korean text skips Laya.
The intake logs every Laya score with each decision, so the review decisions become
labelled data for a later calibration or fine-tune on the user's own messages.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("telemachus.memory.laya")

WORKER = Path(__file__).resolve().parent / "laya_worker.py"
DEFAULT_PYTHON = str(Path.home() / "laya" / ".venv" / "bin" / "python")
READY_TIMEOUT_S = 180.0
REQUEST_TIMEOUT_S = 30.0
IDLE_SECONDS = 600.0
# After a failure, do not try to start the worker again for this long. A broken install
# then costs one fast failure per turn, not a 180-second wait.
COOLDOWN_SECONDS = 600.0
HANGUL = re.compile(r"[가-힣]")


class LayaUnavailable(RuntimeError):
    """Laya could not answer. The caller must take the safe path and report it.

    cooldown=True for a broken worker (start failure, crash, timeout): the bridge then
    waits COOLDOWN_SECONDS before it starts a worker again. A per-request error from a
    healthy worker sets cooldown=False and does not block later requests.
    """

    def __init__(self, message: str, cooldown: bool = True):
        super().__init__(message)
        self.cooldown = cooldown


def laya_enabled() -> bool:
    return os.environ.get("TELEMACHUS_MEMORY_LAYA", "on").lower() not in ("0", "off", "false", "no")


class LayaMemoryGate:
    """Owns one resident worker process and talks JSON lines to it."""

    def __init__(self, python_path: Optional[str] = None, idle_seconds: float = IDLE_SECONDS):
        self.python_path = python_path or os.environ.get("LAYA_PYTHON", DEFAULT_PYTHON)
        self.idle_seconds = idle_seconds
        self.process: Optional[subprocess.Popen] = None
        self._stdout: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._idle_timer: Optional[threading.Thread] = None
        self._cooldown_until = 0.0
        self._last_error = ""

    # ----- process management -----------------------------------------------------------

    def _start(self) -> None:
        if not Path(self.python_path).is_file():
            raise LayaUnavailable(f"Laya python not found at {self.python_path} (set LAYA_PYTHON)")
        env = dict(os.environ, TELEMACHUS_LAYA_MEMORY_MODEL="english")
        self.process = subprocess.Popen(
            [self.python_path, "-I", str(WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        ready = threading.Event()
        stderr_tail: List[str] = []

        def read_stderr(proc):
            for line in proc.stderr:
                if "LAYA_MEMORY_READY" in line:
                    ready.set()
                else:
                    stderr_tail.append(line.rstrip())
                    del stderr_tail[:-20]

        def read_stdout(proc, out_q):
            for line in proc.stdout:
                out_q.put(line)

        self._stdout = queue.Queue()
        threading.Thread(target=read_stderr, args=(self.process,), daemon=True).start()
        threading.Thread(target=read_stdout, args=(self.process, self._stdout), daemon=True).start()
        deadline = time.time() + READY_TIMEOUT_S
        while not ready.wait(0.25):
            # Stop waiting at once when the worker has died (missing torch, bad venv).
            if self.process.poll() is not None or time.time() > deadline:
                time.sleep(0.2)  # let the stderr reader collect the last lines
                tail = " | ".join(stderr_tail[-3:])
                code = self.process.poll()
                self._kill()
                why = f"exited with code {code}" if code is not None else f"did not start within {READY_TIMEOUT_S:.0f}s"
                raise LayaUnavailable(f"Laya worker {why}: {tail}")
        self._start_idle_timer()

    def _start_idle_timer(self) -> None:
        def watch(proc):
            while True:
                time.sleep(min(30.0, self.idle_seconds))
                with self._lock:
                    if self.process is not proc or proc.poll() is not None:
                        return
                    if time.time() - self._last_used >= self.idle_seconds:
                        logger.info("Laya memory worker idle; stopping it to free memory.")
                        self._kill()
                        return

        self._idle_timer = threading.Thread(target=watch, args=(self.process,), daemon=True)
        self._idle_timer.start()

    def _kill(self) -> None:
        if self.process:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None

    def close(self) -> None:
        with self._lock:
            self._kill()

    # ----- requests ---------------------------------------------------------------------

    def classify(self, texts: List[str]) -> List[Dict[str, Any]]:
        """Returns one result per text: {'model', 'authorship', 'durability', 'latency_ms'}.

        Raises LayaUnavailable on any failure (not installed, start timeout, crash, bad
        output, request timeout). Never returns a partial or guessed result.
        """
        if not texts:
            return []
        with self._lock:
            if time.time() < self._cooldown_until:
                raise LayaUnavailable(
                    f"Laya is in cool-down after a failure ({self._last_error}); "
                    f"next try in {self._cooldown_until - time.time():.0f}s"
                )
            try:
                return self._classify_locked(texts)
            except LayaUnavailable as e:
                if e.cooldown:
                    self._cooldown_until = time.time() + COOLDOWN_SECONDS
                    self._last_error = str(e)[:200]
                raise

    def _classify_locked(self, texts: List[str]) -> List[Dict[str, Any]]:
        self._last_used = time.time()
        if self.process is None or self.process.poll() is not None:
            self._start()
        try:
            self.process.stdin.write(json.dumps({"records": [{"text": t} for t in texts]}) + "\n")
            self.process.stdin.flush()
            line = self._stdout.get(timeout=REQUEST_TIMEOUT_S)
        except queue.Empty:
            self._kill()
            raise LayaUnavailable(f"Laya worker gave no answer within {REQUEST_TIMEOUT_S:.0f}s")
        except Exception as e:
            self._kill()
            raise LayaUnavailable(f"Laya worker pipe failed: {e}")
        self._last_used = time.time()
        try:
            data = json.loads(line)
        except Exception:
            raise LayaUnavailable(f"Laya worker returned invalid JSON: {line[:200]}")
        if "error" in data:
            raise LayaUnavailable(f"Laya worker error: {data['error']}", cooldown=False)
        results = data.get("results") or []
        if len(results) != len(texts):
            raise LayaUnavailable(f"Laya worker returned {len(results)} results for {len(texts)} texts",
                                  cooldown=False)
        return results


_gate: Optional[LayaMemoryGate] = None
_gate_lock = threading.Lock()


def get_gate() -> LayaMemoryGate:
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = LayaMemoryGate()
        return _gate
