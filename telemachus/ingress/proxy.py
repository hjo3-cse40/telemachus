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


class IngressProxyHandler(http.server.BaseHTTPRequestHandler):
    """Handles incoming OpenAI-compatible API calls, prunes tools, and proxies upstream."""

    protocol_version = "HTTP/1.1"
    router: IngressToolRouter = IngressToolRouter()
    upstream_host = "127.0.0.1"
    upstream_port = 1234

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
            chunk = resp.read(4096)
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

        # Re-serialize modified payload
        modified_body = json.dumps(payload).encode("utf-8")

        # Forward upstream
        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=120)
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

            # Trigger asynchronous continual memory extraction in the background
            messages = payload.get("messages", [])
            if messages and len(messages) >= 1:
                threading.Thread(
                    target=self._run_continual_extraction,
                    args=(messages,),
                    daemon=True,
                    name="ContinualMemoryExtractor",
                ).start()
        except Exception as e:
            self.send_error(502, f"Upstream LM Studio communication error: {e}")
        finally:
            conn.close()

    def _run_continual_extraction(self, messages: List[Dict[str, Any]]) -> None:
        """Asynchronously extracts durable facts from the conversation turn."""
        try:
            from telemachus.memory.extractor import MemoryExtractor
            extractor = MemoryExtractor()
            new_entries = extractor.extract_and_store(messages)
            if new_entries:
                logger.info(f"Continual extraction saved {len(new_entries)} facts: {[e.text for e in new_entries]}")
        except Exception as e:
            logger.debug(f"Continual extraction background task failed: {e}")



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
