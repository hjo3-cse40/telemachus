"""The ingress proxy queues the memory intake for new user turns only."""
import http.client
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_lmstudio import FakeLMStudio, facts  # noqa: E402

from telemachus.ingress import proxy as proxy_mod  # noqa: E402
from telemachus.memory.cache import MemoryCache  # noqa: E402
from telemachus.memory.extractor import MemoryExtractor  # noqa: E402
from telemachus.memory.intake import MemoryIntake  # noqa: E402
from telemachus.memory.vault import MemoryVault  # noqa: E402


class TestProxyIntake(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test_proxy_intake_")
        self.lm = FakeLMStudio()
        self.vault = MemoryVault(self.dir)
        self.queue = proxy_mod.MemoryIntakeQueue()
        self.queue._intake = MemoryIntake(
            vault=self.vault, cache=MemoryCache(vault=self.vault),
            extractor=MemoryExtractor(completion_url=self.lm.completion_url), use_laya=False,
        )
        self._orig_queue = proxy_mod.memory_intake_queue
        proxy_mod.memory_intake_queue = self.queue
        self.submitted = []
        real_submit = self.queue.submit
        self.queue.submit = lambda messages: (self.submitted.append(messages), real_submit(messages))
        self.server = proxy_mod.run_proxy(listen_port=0, upstream_port=self.lm.port)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        proxy_mod.memory_intake_queue = self._orig_queue
        self.lm.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, messages):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"model": "qwen/qwen3.5-9b", "messages": messages})
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def wait_for(self, predicate, seconds=5.0):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_user_turn_runs_intake_and_tool_round_does_not(self):
        self.lm.replies = [
            "Sure.",  # the chat answer the proxy forwards
            facts({"text": "User prefers short emails.", "quote": "I prefer short emails", "category": "preference"}),
        ]
        status, _ = self.post([{"role": "user", "content": "I prefer short emails."}])
        self.assertEqual(status, 200)
        self.assertTrue(self.wait_for(lambda: len(self.vault.load_facts()) == 1), "intake did not save the fact")

        before = len(self.lm.requests)
        self.lm.replies = ["Done."]
        status, _ = self.post([
            {"role": "user", "content": "I prefer short emails."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "telemachus_fs_list", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
        ])
        self.assertEqual(status, 200)
        time.sleep(0.5)
        self.assertEqual(len(self.lm.requests), before + 1, "a tool round must cost only the forwarded request")
        self.assertEqual(len(self.submitted), 1, "the proxy must not queue tool rounds")


if __name__ == "__main__":
    unittest.main()


class TestRouterMemoryQuestions(unittest.TestCase):
    TOOL_NAMES = ["telemachus_email_list", "telemachus_email_show", "telemachus_email_triage",
                  "telemachus_email_search_index", "telemachus_email_draft", "telemachus_fs_list",
                  "telemachus_fs_read", "telemachus_memory_search", "telemachus_memory_source",
                  "telemachus_memory_save", "read", "bash"]

    def _router(self, embeddings: bool):
        import tempfile as _tf
        from telemachus.ingress.router import IngressToolRouter
        router = IngressToolRouter(cache_path=Path(_tf.mkdtemp()) / "emb.json")
        if embeddings:
            # Email tools look most similar to the prompt, as in the real failure.
            def fake_embed(text):
                t = text.lower()
                if "email" in t or "remember" in t:
                    return [1.0, 0.0, 0.0]
                if t.startswith("tool '"):
                    return [0.0, 1.0, 0.0]
                return [0.0, 0.0, 1.0]  # a prompt unrelated to every tool
            router._query_embedding = fake_embed
        else:
            router._query_embedding = lambda text: None
        return router

    def test_memory_question_with_email_word_keeps_memory_tools(self):
        tools = [{"type": "function", "function": {"name": n, "description": n.replace("_", " ")}} for n in self.TOOL_NAMES]
        for embeddings in (True, False):
            pruned, meta = self._router(embeddings).prune_tools("what do you remember about my email preferences?", tools)
            names = {t["function"]["name"] for t in pruned}
            self.assertIn("telemachus_memory_search", names, (embeddings, meta))
            self.assertIn("telemachus_memory_source", names, (embeddings, meta))

    def test_general_looking_memory_question_keeps_memory_tools(self):
        tools = [{"type": "function", "function": {"name": n, "description": n.replace("_", " ")}} for n in self.TOOL_NAMES]
        pruned, meta = self._router(True).prune_tools("tell me what you know about me", tools)
        self.assertIn("telemachus_memory_search", {t["function"]["name"] for t in pruned}, meta)

    def test_plain_question_still_drops_tools(self):
        tools = [{"type": "function", "function": {"name": n, "description": n.replace("_", " ")}} for n in self.TOOL_NAMES]
        pruned, meta = self._router(True).prune_tools("what is 17 times 23?", tools)
        self.assertEqual(pruned, [])


class TestProxyUpstreamStall(unittest.TestCase):
    def test_stall_after_headers_does_not_write_a_second_response(self):
        import socket
        import socketserver as _ss

        class Stalling(_ss.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                                     b"Transfer-Encoding: chunked\r\n\r\n")
                chunk = b'data: {"choices":[{"delta":{"reasoning_content":"Thinking"}}]}\n\n'
                self.request.sendall(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                time.sleep(3)  # longer than the proxy's upstream timeout in this test

        upstream = _ss.ThreadingTCPServer(("127.0.0.1", 0), Stalling)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        old_timeout = proxy_mod.IngressProxyHandler.upstream_timeout
        proxy_mod.IngressProxyHandler.upstream_timeout = 1
        server = proxy_mod.run_proxy(listen_port=0, upstream_port=upstream.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "assistant", "content": "x"}]})
            s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=10)
            s.sendall((f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       f"Content-Length: {len(body)}\r\n\r\n{body}").encode())
            raw = b""
            while True:
                part = s.recv(65536)
                if not part:
                    break
                raw += part
            s.close()
            self.assertEqual(raw.count(b"HTTP/1.1 "), 1, raw[:400])
            self.assertIn(b"Thinking", raw)
        finally:
            proxy_mod.IngressProxyHandler.upstream_timeout = old_timeout
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()


class TestProxyNoDelay(unittest.TestCase):
    def test_client_socket_has_tcp_nodelay(self):
        import socket
        seen = []
        original = proxy_mod.IngressProxyHandler.setup

        def recording_setup(handler):
            original(handler)
            seen.append(handler.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

        proxy_mod.IngressProxyHandler.setup = recording_setup
        server = proxy_mod.run_proxy(listen_port=0, upstream_port=9)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", "/v1/models")
            conn.getresponse().read()
            conn.close()
            self.assertTrue(seen and seen[0], "streamed tokens must not be merged by Nagle's algorithm")
        finally:
            proxy_mod.IngressProxyHandler.setup = original
            server.shutdown()
            server.server_close()


class TestHelperRequests(TestProxyIntake):
    TITLE = {"role": "system", "content": "You are a title generator. You output ONLY a thread title. Nothing else."}

    def test_title_request_gets_thinking_off_and_no_intake(self):
        self.lm.replies = ["17 x 23"]
        status, _ = self.post([self.TITLE, {"role": "user", "content": "Generate a title for this conversation:\n"},
                               {"role": "user", "content": "what is 17 times 23?"}])
        self.assertEqual(status, 200)
        self.assertEqual(self.lm.requests[-1].get("reasoning_effort"), "none")
        self.assertEqual(self.submitted, [], "a helper request must not run the memory intake")

    def test_user_request_keeps_thinking(self):
        self.lm.replies = ["391", "[]"]
        self.post([{"role": "system", "content": "You are opencode, an interactive CLI tool."},
                   {"role": "user", "content": "what is 17 times 23?"}])
        self.assertNotIn("reasoning_effort", self.lm.requests[0])
        self.assertEqual(len(self.submitted), 1)
