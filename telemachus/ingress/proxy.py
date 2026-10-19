"""Transparent HTTP Streaming Proxy for Dynamic Tool Pruning.

Sits between OpenCode / Agent harnesses and LM Studio on localhost:1235 -> localhost:1234.
Dynamically prunes tool schemas on POST /v1/chat/completions before passing to Qwen.
"""
from __future__ import annotations

import http.client
import http.server
import json
import logging
import os
import queue
import socket
import socketserver
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse

from telemachus.ingress.router import IngressToolRouter

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [Telemachus Ingress] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("telemachus.ingress")


# Helper requests that opencode sends next to the user's own: the session title. With
# thinking on, the 9B spent 1,000-2,700 tokens on a title, and the user's answer waited
# behind it (measured 2026-10-19: 89.9 s -> 1.4 s with thinking off for the title).
HELPER_SYSTEM_PREFIXES = ("You are a title generator",)


def is_helper_request(payload: Dict[str, Any]) -> bool:
    """A request from an opencode helper agent, not from the user's own conversation."""
    if payload.get("tools"):
        return False
    messages = payload.get("messages") or []
    system = next((m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "system"), "")
    if isinstance(system, list):
        system = " ".join(p.get("text", "") for p in system if isinstance(p, dict))
    return isinstance(system, str) and system.lstrip().startswith(HELPER_SYSTEM_PREFIXES)


# A thinking model can stream for minutes, and LM Studio may queue a request behind
# another one (with Max Concurrent Predictions = 1) after it has sent the headers.
UPSTREAM_TIMEOUT_S = 900


class IngressProxyHandler(http.server.BaseHTTPRequestHandler):
    """Handles incoming OpenAI-compatible API calls, prunes tools, and proxies upstream."""

    protocol_version = "HTTP/1.1"
    upstream_timeout = UPSTREAM_TIMEOUT_S
    router: IngressToolRouter = IngressToolRouter()
    upstream_host = "127.0.0.1"
    upstream_port = 1234

    def setup(self) -> None:
        super().setup()
        # Stream tokens as they come: without TCP_NODELAY the OS merges the many small
        # writes (Nagle) and thinking tokens arrive in bursts of a few hundred ms.
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard HTTP server logging
        pass

    def do_GET(self) -> None:
        self._proxy_passthrough("GET")

    def do_POST(self) -> None:
        if self.path.startswith("/v1/chat/completions"):
            self._handle_chat_completions()
        else:
            self._proxy_passthrough("POST")

    def _stream_response(self, resp: http.client.HTTPResponse) -> None:
        """Streams upstream response with correct HTTP/1.1 chunked framing if needed."""
        is_chunked = resp.getheader("Transfer-Encoding", "").lower() == "chunked"
        self._headers_sent = True
        self.send_response(resp.status, resp.reason)

        for k, v in resp.getheaders():
            # Skip hop-by-hop and framing headers that we manage explicitly
            if k.lower() in ("transfer-encoding", "content-length", "connection", "server"):
                continue
            self.send_header(k, v)

        if is_chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "keep-alive")
        else:
            cl = resp.getheader("Content-Length")
            if cl:
                self.send_header("Content-Length", cl)
            self.send_header("Connection", "close")

        self.end_headers()

        while True:
            # read1 returns what has arrived; read(4096) would wait for 4 KB and hold
            # back the streamed thinking and answer tokens.
            chunk = resp.read1(4096)
            if not chunk:
                break
            if is_chunked:
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            else:
                self.wfile.write(chunk)
            self.wfile.flush()

        if is_chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _proxy_passthrough(self, method: str) -> None:
        """Pipes non-chat requests (like /v1/models or /v1/embeddings) directly upstream."""
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else None

        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=30)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            headers["Host"] = f"{self.upstream_host}:{self.upstream_port}"
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self._stream_response(resp)
        except Exception as e:
            self.send_error(502, f"Upstream LM Studio connection error: {e}")
        finally:
            conn.close()

    def _extract_last_user_prompt(self, messages: List[Dict[str, Any]]) -> str:
        user_texts = []
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_texts.append(content.strip())
                elif isinstance(content, list):
                    texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    combined = " ".join(texts).strip()
                    if combined:
                        user_texts.append(combined)
            if len(user_texts) >= 2:
                break
        if not user_texts:
            return ""
        # If the latest message is a brief follow-up (<45 chars), merge with preceding message for domain context
        if len(user_texts[0]) < 45 and len(user_texts) > 1:
            return f"{user_texts[1]} {user_texts[0]}"
        return user_texts[0]

    def _handle_chat_completions(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        tools = payload.get("tools")
        original_tool_count = len(tools) if tools else 0
        pruning_meta: Dict[str, Any] = {}

        if tools and isinstance(tools, list) and len(tools) > 0:
            messages = payload.get("messages", [])
            last_prompt = self._extract_last_user_prompt(messages)

            t0 = time.perf_counter()
            pruned_tools, pruning_meta = self.router.prune_tools(last_prompt, tools)
            prune_duration_ms = (time.perf_counter() - t0) * 1000

            if not pruned_tools:
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                logger.info(
                    f"Prompt: '{last_prompt[:50]}...' -> Domain: {pruning_meta.get('domain')} "
                    f"| Pruned: {original_tool_count} -> 0 tools (Dropped) in {prune_duration_ms:.1f}ms"
                )
            else:
                payload["tools"] = pruned_tools
                logger.info(
                    f"Prompt: '{last_prompt[:50]}...' -> Domain: {pruning_meta.get('domain')} "
                    f"| Pruned: {original_tool_count} -> {len(pruned_tools)} tools {pruning_meta.get('selected_tools')} "
                    f"in {prune_duration_ms:.1f}ms"
                )

        helper = is_helper_request(payload)
        if helper and "reasoning_effort" not in payload:
            # Qwen3.5 in LM Studio: only reasoning_effort "none" turns thinking off per request.
            payload["reasoning_effort"] = "none"
            logger.info("Helper request (session title): thinking off")

        # Re-serialize modified payload
        modified_body = json.dumps(payload).encode("utf-8")

        # Forward upstream
        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=self.upstream_timeout)
        self._headers_sent = False
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
            headers["Host"] = f"{self.upstream_host}:{self.upstream_port}"
            headers["Content-Length"] = str(len(modified_body))
            if pruning_meta:
                headers["X-Telemachus-Domain"] = pruning_meta.get("domain", "")
                headers["X-Telemachus-Pruned"] = f"{original_tool_count}->{len(payload.get('tools', []))}"

            conn.request("POST", self.path, body=modified_body, headers=headers)
            resp = conn.getresponse()
            self._stream_response(resp)

            # Memory intake: only when this request starts a new user turn. Tool rounds of
            # the agent loop end with a tool or assistant message and are not queued.
            messages = payload.get("messages", [])
            # Helper requests repeat the user's message; the intake reads the real turn.
            if messages and messages[-1].get("role") == "user" and not helper:
                memory_intake_queue.submit(messages)
        except Exception as e:
            # Loud: the client may only see a cut stream, so the log must say why.
            logger.warning(f"Upstream LM Studio error ({'during' if self._headers_sent else 'before'} the response): "
                           f"{type(e).__name__}: {e}")
            if self._headers_sent:
                # A second status line inside a started response is what clients report as
                # "InvalidHTTPResponse". End the connection instead.
                self.close_connection = True
            else:
                self.send_error(502, f"Upstream LM Studio communication error: {e}")
        finally:
            conn.close()


class MemoryIntakeQueue:
    """One background thread runs the memory intake, one turn at a time.

    One worker means at most one extra LM Studio request waits behind the user's own
    requests. The queue is small; when it is full, the oldest turn is dropped and the
    drop is reported as a failure, not hidden.
    """

    def __init__(self, maxsize: int = 8):
        self._queue: "queue.Queue[List[Dict[str, Any]]]" = queue.Queue(maxsize=maxsize)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._intake = None

    def submit(self, messages: List[Dict[str, Any]]) -> None:
        """Never raises: the chat response is already sent when this runs."""
        try:
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._run, daemon=True, name="MemoryIntake")
                    self._thread.start()
                while True:
                    try:
                        self._queue.put_nowait(messages)
                        return
                    except queue.Full:
                        try:
                            dropped = self._queue.get_nowait()
                        except queue.Empty:
                            continue
                        self._report_drop(dropped)
        except Exception as e:
            logger.warning(f"Memory intake submit failed: {type(e).__name__}: {e}")

    def _report_drop(self, dropped: List[Dict[str, Any]]) -> None:
        """A dropped turn goes to failed_turns, so `python -m telemachus.memory retry` can run it."""
        try:
            from telemachus.memory.health import MemoryHealth
            from telemachus.memory.intake import MAX_DOCS_PER_TURN, _sha, message_text, previous_turn_documents
            from telemachus.memory.vault import MemoryVault
            vault = self._intake.vault if self._intake is not None else MemoryVault()
            text = message_text(dropped[-1]) if dropped else ""
            if text:
                vault.append_failed_turn(_sha("turn:" + text), text, "intake queue full; turn dropped")
            # The documents of that turn are not sent again either: keep them too.
            for label, doc in previous_turn_documents(dropped)[:MAX_DOCS_PER_TURN]:
                vault.append_failed_turn(_sha("doc:" + doc), doc, "intake queue full; turn dropped",
                                         kind="document", label=label)
            MemoryHealth(vault).fail("intake_queue", "Memory intake queue full; the oldest turn was dropped and kept for retry")
        except Exception as e:
            logger.warning(f"Memory intake queue full and the drop could not be recorded: {e}")

    def _run(self) -> None:
        while True:
            messages = self._queue.get()
            try:
                if self._intake is None:
                    from telemachus.memory.intake import MemoryIntake
                    self._intake = MemoryIntake()
                report = self._intake.process_turn(messages)
                if self._queue.empty():
                    # The queue has room again: clear an old "queue full" failure.
                    self._intake.health.ok("intake_queue")
                if report.status == "failed":
                    logger.warning(f"Memory intake FAILED: {report.reason}")
                elif report.saved or report.queued_for_review or report.document_facts:
                    logger.info(
                        f"Memory intake: saved={report.saved} review={report.queued_for_review} "
                        f"documents={len(report.document_facts)} rejected={len(report.rejected)}"
                    )
            except Exception as e:
                # An unexpected bug must still be loud: WARNING log and health.json.
                logger.warning(f"Memory intake crashed: {type(e).__name__}: {e}")
                try:
                    from telemachus.memory.health import MemoryHealth
                    from telemachus.memory.vault import MemoryVault
                    MemoryHealth(MemoryVault()).fail("intake_crash", f"{type(e).__name__}: {e}")
                except Exception:
                    pass


memory_intake_queue = MemoryIntakeQueue()



class IngressProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded HTTP Server for the Ingress Proxy."""
    allow_reuse_address = True
    daemon_threads = True


def run_proxy(
    listen_host: str = "127.0.0.1",
    listen_port: int = 1235,
    upstream_host: str = "127.0.0.1",
    upstream_port: int = 1234,
) -> IngressProxyServer:
    """Launches the Ingress Proxy server."""
    IngressProxyHandler.upstream_host = upstream_host
    IngressProxyHandler.upstream_port = upstream_port

    server = IngressProxyServer((listen_host, listen_port), IngressProxyHandler)
    logger.info(f"Ingress Proxy listening on http://{listen_host}:{listen_port} -> Upstream: {upstream_host}:{upstream_port}")
    return server


def start_ingress_proxy_background(
    listen_host: str = "127.0.0.1",
    listen_port: int = 1235,
    upstream_host: str = "127.0.0.1",
    upstream_port: int = 1234,
) -> Optional[IngressProxyServer]:
    """Starts the ingress proxy in a background daemon thread if not already running."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        if s.connect_ex((listen_host, listen_port)) == 0:
            logger.info(f"Ingress Proxy already active on http://{listen_host}:{listen_port}")
            return None

    try:
        server = run_proxy(
            listen_host=listen_host,
            listen_port=listen_port,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
        t = threading.Thread(target=server.serve_forever, daemon=True, name="TelemachusIngressProxy")
        t.start()
        sys.stderr.write(f"[Telemachus MCP] Ingress Tool Pruning Proxy active on http://{listen_host}:{listen_port}\n")
        sys.stderr.flush()
        return server
    except Exception as e:
        sys.stderr.write(f"[Telemachus MCP] Notice: Ingress Proxy start skipped or already running: {e}\n")
        sys.stderr.flush()
        return None
