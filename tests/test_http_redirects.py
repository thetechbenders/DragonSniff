"""Exercise the production device openers against isolated loopback origins."""

from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch

from dragonsniff.client import JSON_ENDPOINTS, DragonClient
from dragonsniff.prusalink import PrusaLinkConfig, PrusaLinkSource
from dragonsniff.recording import SessionRecorder
from dragonsniff.target import parse_target


REDIRECT_STATUSES = (301, 302, 303, 307, 308)
API_KEY = "localhost-regression-key"
STATUS_BODY = b'{"printer":{"state":"IDLE","temp_bed":20,"target_bed":0}}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        fixture = self.server.fixture
        fixture["requests"].append((self.path, dict(self.headers)))
        status = fixture.get("status", 200)
        body = fixture.get("body", STATUS_BODY)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if "location" in fixture:
            self.send_header("Location", fixture["location"])
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def server():
    fixture = {"requests": []}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.fixture = fixture
    thread = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", fixture
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class RedirectTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Keep loopback requests local even on hosts with configured proxies.
        self.stack.enter_context(patch.dict("os.environ", {"no_proxy": "127.0.0.1"}))
        self.origin, self.source = self.stack.enter_context(server())
        self.other_origin, self.target = self.stack.enter_context(server())

    def configure_redirect(self, status, same_origin):
        self.source["requests"].clear()
        self.target["requests"].clear()
        self.source.update(
            status=status,
            body=b'{"error":"redirect refused"}',
            location=(self.origin if same_origin else self.other_origin)
            + "/redirect-target",
        )

    def assert_only_original_request(self, path):
        self.assertEqual(self.target["requests"], [])
        self.assertEqual([path for path, _ in self.source["requests"]], [path])

    def test_dragon_json_redirects_are_recorded_without_following(self):
        for status in REDIRECT_STATUSES:
            for same_origin in (False, True):
                for path in JSON_ENDPOINTS:
                    with self.subTest(status=status, same_origin=same_origin, path=path):
                        self.configure_redirect(status, same_origin)
                        recorder = SessionRecorder(10)
                        client = DragonClient(parse_target(self.origin), recorder)
                        result = client.fetch_json(
                            path, context={"owner": "redirect-test"}
                        )
                        self.assert_only_original_request(path)
                        self.assertEqual(result["status"], status)
                        self.assertTrue(result["response_received"])
                        self.assertFalse(result["http_ok"])
                        self.assertFalse(result["ok"])
                        self.assertGreaterEqual(result["elapsed_ms"], 0)
                        self.assertEqual(
                            result["headers"]["location"], self.source["location"]
                        )
                        self.assertEqual(result["parsed"], {"error": "redirect refused"})
                        records = recorder.snapshot()
                        self.assertEqual(
                            [r["kind"] for r in records],
                            ["http_request", "http_response"],
                        )
                        self.assertEqual(records[-1]["owner"], "redirect-test")
                        self.assertEqual(client.budget.active, 0)

    def test_dragon_sse_redirects_are_unavailable_without_following(self):
        for status in REDIRECT_STATUSES:
            for same_origin in (False, True):
                with self.subTest(status=status, same_origin=same_origin):
                    self.configure_redirect(status, same_origin)
                    recorder = SessionRecorder(10)
                    client = DragonClient(parse_target(self.origin), recorder)
                    states, events = [], []
                    client.stream_events(
                        Event(), events.append,
                        lambda state, details: states.append((state, details)),
                    )
                    self.assert_only_original_request("/api/v2/events")
                    self.assertEqual(events, [])
                    self.assertEqual(
                        [s for s, _ in states], ["connecting", "unavailable", "closed"]
                    )
                    details = states[1][1]
                    self.assertEqual(details["status"], status)
                    self.assertGreaterEqual(details["elapsed_ms"], 0)
                    self.assertTrue(details["response_received"])
                    self.assertFalse(details["http_ok"])
                    self.assertEqual(
                        details["headers"]["location"], self.source["location"]
                    )
                    self.assertEqual(details["parsed"], {"error": "redirect refused"})
                    self.assertEqual(
                        [r["kind"] for r in recorder.snapshot()],
                        ["sse_connecting", "sse_unavailable", "sse_closed"],
                    )
                    self.assertEqual(client.budget.active, 0)

    def test_prusalink_redirects_never_forward_the_api_key(self):
        for status in REDIRECT_STATUSES:
            for same_origin in (False, True):
                with self.subTest(status=status, same_origin=same_origin):
                    self.configure_redirect(status, same_origin)
                    # Even an echoed credential must stay out of normalized evidence.
                    self.source["body"] = API_KEY.encode()
                    recorder = SessionRecorder(10)
                    source = PrusaLinkSource(
                        PrusaLinkConfig.from_values(self.origin, API_KEY, 5), recorder
                    )
                    result = source.poll_once(context={"owner": "redirect-test"})
                    self.assert_only_original_request("/api/v1/status")
                    self.assertEqual(
                        self.source["requests"][0][1]["X-Api-Key"], API_KEY
                    )
                    self.assertEqual(result["response_status"], status)
                    self.assertGreaterEqual(result["elapsed_ms"], 0)
                    self.assertEqual(result["source_state"], "transport_error")
                    self.assertTrue(result["connected"])
                    self.assertIsNone(result["authenticated"])
                    self.assertEqual(result["error"]["kind"], "http_status")
                    self.assertIsNone(result["data"])
                    self.assertEqual(result["freshness"]["state"], "unavailable")
                    self.assertEqual(result["owner"], "redirect-test")
                    self.assertNotIn(API_KEY, json.dumps(recorder.snapshot()))
                    self.assertNotIn(API_KEY, json.dumps(source.snapshot()))

    def test_nonredirected_requests_keep_existing_behavior(self):
        recorder = SessionRecorder(20)
        client = DragonClient(parse_target(self.origin), recorder)
        for path in JSON_ENDPOINTS:
            self.assertTrue(client.fetch_json(path)["ok"])
        self.source["body"] = b'data: {"temperature":20}\n\n'
        events, states = [], []
        client.stream_events(
            Event(), events.append, lambda state, _details: states.append(state)
        )
        self.assertEqual(states, ["connecting", "open", "closed"])
        self.assertEqual(events[0]["parsed"], {"temperature": 20})
        self.source["body"] = STATUS_BODY
        source = PrusaLinkSource(
            PrusaLinkConfig.from_values(self.origin, API_KEY, 5), recorder
        )
        self.assertEqual(source.poll_once()["source_state"], "healthy")
        self.assertEqual(self.source["requests"][-1][1]["X-Api-Key"], API_KEY)
        self.assertEqual(self.target["requests"], [])
