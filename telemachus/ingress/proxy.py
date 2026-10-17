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

    def _proxy_passthrough(self, method: str) -> None:
        """Pipes non-chat requests (like /v1/models or /v1/embeddings) directly upstream."""
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else None

        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=30)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
            headers["Host"] = f"{self.upstream_host}:{self.upstream_port}"
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                self.send_header(k, v)
            self.end_headers()

            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.wfile.flush()
        except Exception as e:
            self.send_error(502, f"Upstream LM Studio connection error: {e}")
        finally:
            conn.close()

    def _extract_last_user_prompt(self, messages: List[Dict[str, Any]]) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    # Multi-part content
                    texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    return " ".join(texts)
        return ""

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
                # Remove tools field so model doesn't generate empty tool calls
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
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ["host", "content-length"]}
            headers["Host"] = f"{self.upstream_host}:{self.upstream_port}"
            headers["Content-Length"] = str(len(modified_body))
            if pruning_meta:
                headers["X-Telemachus-Domain"] = pruning_meta.get("domain", "")
                headers["X-Telemachus-Pruned"] = f"{original_tool_count}->{len(payload.get('tools', []))}"

            conn.request("POST", self.path, body=modified_body, headers=headers)
            resp = conn.getresponse()

            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                self.send_header(k, v)
            self.end_headers()

            # Stream response back (supporting both SSE stream and full JSON)
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            self.send_error(502, f"Upstream LM Studio communication error: {e}")
        finally:
            conn.close()


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
