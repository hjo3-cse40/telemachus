"""A scripted stand-in for LM Studio, served over real HTTP on a free local port.

It answers /api/v0/models with the models in `loaded` and /v1/chat/completions with
the next entry in `replies` (a string is returned as the message content; an int is
returned as that HTTP status). Every completion request body is kept in `requests`.
"""
from __future__ import annotations

import http.server
import json
import threading
from typing import Any, Dict, List, Union


class FakeLMStudio:
    def __init__(self):
        self.loaded: List[Dict[str, Any]] = [
            {"id": "qwen/qwen3.5-9b", "type": "vlm", "state": "loaded"},
            {"id": "text-embedding-nomic-embed-text-v1.5@q8_0", "type": "embeddings", "state": "loaded"},
            {"id": "some/other-model", "type": "llm", "state": "not-loaded"},
        ]
        self.replies: List[Union[str, int]] = []
        self.requests: List[Dict[str, Any]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/v0/models"):
                    self._send(200, {"data": fake.loaded})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(body)
                reply = fake.replies.pop(0) if fake.replies else "[]"
                if isinstance(reply, int):
                    self._send(reply, {"error": "scripted failure"})
                    return
                self._send(200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def completion_url(self) -> str:
        return f"{self.base}/v1/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def facts(*items: Dict[str, str]) -> str:
    return json.dumps(list(items), ensure_ascii=False)
