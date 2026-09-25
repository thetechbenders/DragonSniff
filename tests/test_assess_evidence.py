import hashlib
import json
from unittest import TestCase

from dragonsniff.assess.evidence import EvidenceError, load_evidence
from tests.assess_fixtures import RUN_ID, S, steady


def line(**record):
    base = {"sequence": 1, "timestamp": "t", "monotonic_ns": 0, "kind": "k"}
    base.update(record)
    return json.dumps(base).encode() + b"\n"


class EvidenceLoaderTests(TestCase):
    def test_identity_is_exact_bytes(self) -> None:
        data = steady([65.0, 65.0]).to_bytes()
        evidence = load_evidence(data)
        self.assertEqual(evidence.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(evidence.byte_length, len(data))
        self.assertEqual(len(evidence.records), 6)
        self.assertEqual(evidence.run.run_id, RUN_ID)
        self.assertIsNone(evidence.run.problem)
        self.assertEqual(evidence.gaps, ())

    def test_corruption_refuses_the_whole_assessment(self) -> None:
        good = line(sequence=1) + line(sequence=2)
        cases = {
            "empty": b"",
            "incomplete final record": good[:-1],
            "blank line": line(sequence=1) + b"\n" + line(sequence=2),
            "duplicate sequence": line(sequence=1) + line(sequence=1),
            "sequence out of order": line(sequence=2) + line(sequence=1),
            "not an object": b"[1]\n",
            "not json": b"{nope\n",
            "not utf-8": b'{"a":"\xff"}\n',
            "boolean sequence": line(sequence=True),
            "zero sequence": line(sequence=0),
            "float monotonic": line(monotonic_ns=1.5),
            "negative monotonic": line(monotonic_ns=-1),
            "missing kind": line(kind=""),
            "missing timestamp": line(timestamp=None),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(EvidenceError):
                    load_evidence(data)

    def test_gaps_are_reported_not_rejected(self) -> None:
        data = line(sequence=3) + line(sequence=4) + line(sequence=9)
        evidence = load_evidence(data)
        self.assertEqual(evidence.gaps, ((1, 2), (5, 8)))
        self.assertTrue(evidence.missing_between(5, 5))
        self.assertTrue(evidence.missing_between(8, 12))
        self.assertFalse(evidence.missing_between(3, 4))

    def test_cross_thread_monotonic_inversion_is_not_corruption(self) -> None:
        data = line(sequence=1, monotonic_ns=10) + line(sequence=2, monotonic_ns=9)
        evidence = load_evidence(data)
        self.assertEqual(evidence.monotonic_inversions, 1)

    def test_run_structure_problems_are_findings_not_errors(self) -> None:
        missing = load_evidence(line(sequence=1))
        self.assertEqual(missing.run.problem, "run_missing")
        two = line(sequence=1, kind="capture_run_started") + line(
            sequence=2, kind="capture_run_started"
        )
        self.assertEqual(load_evidence(two).run.problem, "run_ambiguous")
        unterminated = load_evidence(line(sequence=1, kind="capture_run_started"))
        self.assertIsNone(unterminated.run.problem)
        self.assertIsNone(unterminated.run.terminal)

    def test_session_metadata_must_be_terminal(self) -> None:
        data = steady([65.0]).to_bytes()
        active = json.dumps({"session_id": "a" * 32, "status": "active"}).encode()
        with self.assertRaisesRegex(EvidenceError, "not terminal"):
            load_evidence(data, active)
        done = json.dumps(
            {"session_id": "a" * 32, "status": "interrupted", "kind": "capture",
             "format_version": 1, "records": 4, "secret": "x"}
        ).encode()
        session = load_evidence(data, done).session
        self.assertEqual(session["status"], "interrupted")
        self.assertNotIn("secret", session)

    def test_bytes_are_required(self) -> None:
        with self.assertRaises(TypeError):
            load_evidence("not bytes")  # type: ignore[arg-type]

    def test_non_finite_json_tokens_load_as_values_for_the_engine_to_reject(self) -> None:
        data = steady([float("nan"), float("inf")]).to_bytes()
        self.assertIn(b"NaN", data)
        self.assertEqual(len(load_evidence(data).records), 6)
