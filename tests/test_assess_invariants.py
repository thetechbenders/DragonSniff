"""First-class epistemic properties of the Assess engine.

The central invariant: deleting evidence must never move a finding toward
PASS. Formally, for any set of deleted records:

- reduced PASS implies original PASS;
- original PASS never becomes FAIL;
- time_in_band bounds only widen (lower never rises, upper never falls).
"""

import json
import random
from unittest import TestCase

from dragonsniff.assess import EvidenceError, assess
from tests.assess_fixtures import HOLDS_LE_70, S, EvidenceBuilder, in_band, profile, steady

SEEDS = range(40)
DELETIONS_PER_FIXTURE = 25


def random_fixture(rng: random.Random) -> EvidenceBuilder:
    evidence = EvidenceBuilder().start(0)
    now = 0.1
    for _ in range(rng.randint(3, 14)):
        duration = rng.choice([0.01, 0.05, 0.2])
        roll = rng.random()
        if roll < 0.08:
            evidence.value(65.0, now, now + duration, failed=True)
        elif roll < 0.14:
            evidence.value(float("nan"), now, now + duration)
        else:
            evidence.value(rng.choice([63.5, 64.5, 65.0, 65.5, 66.5, 71.0]), now, now + duration)
        if rng.random() < 0.3:
            # Another source's record, stamped slightly earlier than the last
            # sample as a cross-thread race would produce.
            stamp = max(0, round((now + duration) * S) - rng.randint(0, 2_000_000))
            evidence.other("source_observation", stamp, source="other")
        now += rng.choice([0.5, 1.0, 1.0, 2.5])
    return evidence.complete(round(now * S))


def random_checks(rng: random.Random) -> tuple[str, float | None]:
    start = rng.choice([0.0, 0.5, 1.0])
    window = f"{{ start_offset_s = {start} }}"
    checks = HOLDS_LE_70 + f"window = {window}\n\n" + in_band(
        rng.choice([0.2, 0.5, 0.8, 1.0]), window=window
    )
    hold = rng.choice([None, 0.6, 1.2, 3.0, 60.0])
    return checks, hold


def findings(data: bytes, checks: str, hold: float | None) -> dict[str, dict]:
    report = json.loads(assess(data, profile(checks, max_hold_s=hold)))
    return {finding["id"]: finding for finding in report["findings"]}


def bounds(finding: dict) -> tuple[float, float] | None:
    measurement = finding.get("measurement")
    if measurement is None:
        return None
    fraction = measurement["fraction_in_band"]
    return fraction["lower"], fraction["upper"]


class DeletionNeverMovesTowardPassTests(TestCase):
    def assert_not_toward_pass(self, original: dict, reduced: dict, context: str) -> None:
        for check_id, before in original.items():
            after = reduced[check_id]
            message = f"{context} {check_id}: {before['result']} -> {after['result']}"
            if after["result"] == "PASS":
                self.assertEqual(before["result"], "PASS", message)
            if before["result"] == "PASS":
                self.assertNotEqual(after["result"], "FAIL", message)
            original_bounds, reduced_bounds = bounds(before), bounds(after)
            if original_bounds and reduced_bounds:
                self.assertLessEqual(reduced_bounds[0], original_bounds[0] + 1e-15, message)
                self.assertGreaterEqual(reduced_bounds[1], original_bounds[1] - 1e-15, message)

    def reduced(self, evidence: EvidenceBuilder, drop: set[int], checks: str, hold):
        data = evidence.to_bytes(drop=drop)
        if not data:
            return None
        try:
            return findings(data, checks, hold)
        except EvidenceError:
            return None  # refusing to assess is never a move toward PASS

    def test_random_deletions(self) -> None:
        results_seen: set[str] = set()
        for seed in SEEDS:
            rng = random.Random(seed)
            evidence = random_fixture(rng)
            checks, hold = random_checks(rng)
            original = findings(evidence.to_bytes(), checks, hold)
            results_seen.update(f["result"] for f in original.values())
            sequences = [record["sequence"] for record in evidence.records]
            for trial in range(DELETIONS_PER_FIXTURE):
                style = trial % 3
                if style == 0:
                    drop = {rng.choice(sequences)}
                elif style == 1:
                    drop = set(rng.sample(sequences, rng.randint(1, max(1, len(sequences) // 3))))
                else:
                    first = rng.randrange(len(sequences))
                    drop = set(sequences[first:first + rng.randint(1, 4)])
                reduced = self.reduced(evidence, drop, checks, hold)
                if reduced is not None:
                    self.assert_not_toward_pass(original, reduced, f"seed={seed} drop={sorted(drop)}")
        # The property is only meaningful if the fixtures produce every outcome.
        self.assertEqual(results_seen, {"PASS", "FAIL", "INCONCLUSIVE"})

    def test_every_single_record_deletion_from_a_passing_run(self) -> None:
        evidence = steady([65.0] * 8)
        checks = HOLDS_LE_70 + "window = { start_offset_s = 1.0 }\n\n" + in_band(
            0.9, window="{ start_offset_s = 1.0 }"
        )
        original = findings(evidence.to_bytes(), checks, 1.05)
        self.assertEqual({f["result"] for f in original.values()}, {"PASS"})
        for record in evidence.records:
            reduced = self.reduced(evidence, {record["sequence"]}, checks, 1.05)
            if reduced is None:
                continue
            with self.subTest(sequence=record["sequence"]):
                # holds_throughout needs the whole window, so any loss ends PASS.
                self.assertEqual(reduced["below_limit"]["result"], "INCONCLUSIVE")
                # time_in_band may remain PASS if the worst case still clears
                # the criterion, but it may never fail and its bounds only widen.
                self.assert_not_toward_pass(original, reduced, f"drop={record['sequence']}")

    def test_deleting_the_only_proof_of_violation_gives_inconclusive_not_pass(self) -> None:
        evidence = steady([65.0, 65.0, 75.0, 65.0, 65.0])
        checks = HOLDS_LE_70 + "window = { start_offset_s = 1.0 }\n"
        self.assertEqual(findings(evidence.to_bytes(), checks, 1.05)["below_limit"]["result"], "FAIL")
        for drop in ({6}, {7}, {6, 7}):
            with self.subTest(drop=drop):
                after = findings(evidence.to_bytes(drop=drop), checks, 1.05)["below_limit"]
                self.assertEqual(after["result"], "INCONCLUSIVE")


class MissingEvidenceNeverPassesTests(TestCase):
    def test_no_observations_never_pass_under_any_hold(self) -> None:
        empty = EvidenceBuilder().start(0).complete(30 * S).to_bytes()
        for hold in (None, 1.0, 3600.0):
            for minimum in (0.001, 0.5, 1.0):
                with self.subTest(hold=hold, minimum=minimum):
                    result = findings(empty, HOLDS_LE_70 + "\n" + in_band(minimum), hold)
                    self.assertNotIn("PASS", {f["result"] for f in result.values()})

    def test_only_invalid_observations_never_pass(self) -> None:
        evidence = EvidenceBuilder().start(0)
        for index in range(10):
            evidence.value(None, 0.1 + index)
        data = evidence.complete(round(10.1 * S)).to_bytes()
        result = findings(data, HOLDS_LE_70 + "\n" + in_band(0.001), 3600.0)
        self.assertNotIn("PASS", {f["result"] for f in result.values()})


class DeterministicReplayTests(TestCase):
    def test_same_inputs_give_byte_identical_reports(self) -> None:
        rng = random.Random(7)
        evidence = random_fixture(rng).to_bytes()
        checks, hold = random_checks(rng)
        profile_bytes = profile(checks, max_hold_s=hold)
        first = assess(evidence, profile_bytes)
        second = assess(evidence, profile_bytes)
        self.assertEqual(first, second)
        self.assertNotIn("assessed_at", first)
        self.assertTrue(first.endswith("\n"))

    def test_assessed_at_is_metadata_only(self) -> None:
        evidence = steady([65.0] * 5).to_bytes()
        profile_bytes = profile(HOLDS_LE_70, max_hold_s=1.05)
        plain = json.loads(assess(evidence, profile_bytes))
        stamped = json.loads(assess(evidence, profile_bytes, assessed_at="2026-09-25T00:00:00Z"))
        self.assertEqual(stamped.pop("metadata"), {"assessed_at": "2026-09-25T00:00:00Z"})
        self.assertEqual(stamped, plain)

    def test_report_identifies_inputs_and_semantics(self) -> None:
        evidence = steady([65.0] * 3).to_bytes()
        profile_bytes = profile(HOLDS_LE_70)
        report = json.loads(assess(evidence, profile_bytes))
        self.assertEqual(report["assess_semantics_version"], 1)
        self.assertEqual(report["assessment_format"], 1)
        self.assertIn("not a safety certification", report["statement"])
        self.assertEqual(report["evidence"]["byte_length"], len(evidence))
        self.assertEqual(report["profile"]["byte_length"], len(profile_bytes))
        self.assertEqual(report["assumptions"], {"hold": "none", "max_hold_s": 0.0})
        self.assertEqual(sum(report["summary"].values()), len(report["findings"]))
