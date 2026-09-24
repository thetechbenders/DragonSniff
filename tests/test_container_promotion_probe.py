"""Behavioral and contract checks for the GHCR version-tag existence probe.

The promotion workflow asks GHCR whether the version tag already exists before
creating it. On a first release the answer is HTTP 404. The probe must treat
that response as headers-only; waiting for the advertised body stalls until the
registry drops the connection and fails the release (curl exit 18).

These tests run the workflow's own curl invocation against a local server that
answers HEAD the way GHCR does: a status and Content-Length but no body.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from unittest import TestCase, skipUnless

WORKFLOW = Path(".github/workflows/promote-container.yml")
REGISTRY = "https://ghcr.io"
MANIFEST_UNKNOWN = (
    b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"manifest unknown"}]}\n'
)
EXISTING_DIGEST = "sha256:" + "a" * 64


class _RegistryHead(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_authorization: list[str] = []

    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        _RegistryHead.seen_authorization.append(
            self.headers.get("Authorization", "")
        )
        if self.path.endswith("/manifests/v9.9.9"):
            self.send_response(200)
            self.send_header("Docker-Content-Digest", EXISTING_DIGEST)
            self.send_header("Content-Length", "1234")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(MANIFEST_UNKNOWN)))
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _probe_command() -> str:
    """Return the workflow's manifest-existence curl command, verbatim."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r'status="\$\((curl .*?/manifests/\$RELEASE_VERSION")\)"', text, re.S
    )
    if match is None:
        raise AssertionError("manifest existence probe not found in workflow")
    return match.group(1)


@skipUnless(shutil.which("curl") and shutil.which("bash"), "requires curl and bash")
class ManifestProbeBehaviorTests(TestCase):
    def setUp(self) -> None:
        _RegistryHead.seen_authorization = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHead)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.headers = Path(tempfile.mkstemp()[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.headers.unlink(missing_ok=True)

    def _run(self, version: str, command: str | None = None) -> subprocess.CompletedProcess[str]:
        command = (command or _probe_command()).replace(REGISTRY, self.base)
        script = f'status="$({command})"; rc=$?; printf "%s" "$status"; exit $rc'
        return subprocess.run(
            ["bash", "-c", script],
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "headers": str(self.headers),
                "registry_token": "test-token",
                "RELEASE_VERSION": version,
            },
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_missing_version_tag_reports_404_promptly(self) -> None:
        result = self._run("v0.5.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "404")
        self.assertEqual(_RegistryHead.seen_authorization, ["Bearer test-token"])

    def test_existing_version_tag_reports_200_and_digest_header(self) -> None:
        result = self._run("v9.9.9")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "200")
        dumped = self.headers.read_text(encoding="utf-8").lower()
        self.assertIn(f"docker-content-digest: {EXISTING_DIGEST}", dumped)

    def test_harness_detects_request_head_body_wait(self) -> None:
        # Control: the pre-fix construct must stall on the same 404 response.
        # Bounded with --max-time and no retries so the control stays fast.
        broken = re.sub(r"--head\b", "--request HEAD", _probe_command())
        broken = re.sub(r"--retry\S*( \d+)?", "", broken).replace(
            "curl ", "curl --max-time 2 ", 1
        )
        result = self._run("v0.5.0", broken)
        self.assertNotEqual(result.returncode, 0)


class ManifestProbeContractTests(TestCase):
    def test_probe_uses_curl_head_mode(self) -> None:
        command = _probe_command()
        self.assertRegex(command, r"--head\b")
        self.assertNotIn("--request HEAD", command)

    def test_promotion_workflow_never_uses_request_head(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(--request|-X)\s+HEAD\b")
