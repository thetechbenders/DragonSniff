"""Public-API regressions for the four PR #52 correctness findings.

These tests also run unchanged against reviewed commit 5e20cf0. They use only
the public API and fixtures that already existed at that commit.
"""

import json
import random
from copy import deepcopy
from fractions import Fraction
from unittest import TestCase

from dragonsniff.assess import ProfileError, assess
from dragonsniff.assess.profile import load_profile
from tests.assess_fixtures import (
    HOLDS_LE_70, S, STATE, EvidenceBuilder, in_band, profile, steady,
)


def finding(evidence, checks, hold=10):
    return json.loads(assess(evidence, profile(checks, max_hold_s=hold)))["findings"][0]


class ReviewRegressionTests(TestCase):
    def test_deleting_duplicate_request_cannot_create_pass(self):
        evidence = EvidenceBuilder().start()
        evidence.other("http_request", 900_000_000, run_id=evidence.run_id,
                       endpoint=STATE, request_id=1)
        evidence.value(65, 1, 1.05).value(65, 2, 2.05).complete(3 * S)
        checks = HOLDS_LE_70 + "window = { start_offset_s = 1.1 }\n"
        before = finding(evidence.to_bytes(), checks)
        after = finding(evidence.to_bytes(drop={2}), checks)
        self.assertEqual(before["result"], "INCONCLUSIVE")
        self.assertEqual(after["result"], "INCONCLUSIVE",
                         "deleting the duplicate must not repair evidence into PASS")

    def test_explicit_parse_error_cannot_create_pass(self):
        evidence = EvidenceBuilder().start().value(65, 0.1, parse_error="invalid JSON")
        evidence.complete(2 * S)
        actual = finding(evidence.to_bytes(), HOLDS_LE_70 +
                         "window = { start_offset_s = 1 }\n")
        self.assertEqual(actual["result"], "INCONCLUSIVE",
                         "a parsed payload cannot override an explicit parse error")

    def test_post_terminal_violation_cannot_create_fail(self):
        evidence = steady([65] * 4, duration_s=4.1)
        checks = HOLDS_LE_70 + "window = { start_offset_s = 1, end_offset_s = 10 }\n"
        before = finding(evidence.to_bytes(), checks, hold=60)
        evidence.value(75, 4.5, 4.55)
        after = finding(evidence.to_bytes(), checks, hold=60)
        self.assertEqual(before["result"], "INCONCLUSIVE")
        self.assertEqual(after["result"], "INCONCLUSIVE",
                         "an observation beyond termination cannot prove a violation")
        self.assertEqual(before, after)

    def test_integer_threshold_is_not_rounded_to_adjacent_integer(self):
        evidence = EvidenceBuilder().start().value(2**53, 0.1).value(2**53, 1.2)
        evidence.complete(2 * S)
        checks = HOLDS_LE_70.replace('op = "le", value = 70.0',
                                    'op = "ne", value = 9007199254740993')
        actual = finding(evidence.to_bytes(), checks +
                         "window = { start_offset_s = 1.1 }\n")
        self.assertEqual(actual["result"], "PASS",
                         "9007199254740992 differs from exact threshold 9007199254740993")
        self.assertEqual(actual["criterion"]["predicate"]["value"], 2**53 + 1)


class EvidenceBoundaryTests(TestCase):
    def test_hold_expires_before_a_long_gap_or_terminal(self):
        b = EvidenceBuilder().start().value(65, 0.1, 0.15).complete(10 * S)
        f = finding(b.to_bytes(), in_band(0.5), hold=1)
        self.assertEqual(f["measurement"]["in_band_ns"], 950_000_000)
        self.assertEqual(f["measurement"]["unknown_ns"], 9_050_000_000)
        self.assertEqual(f["result"], "INCONCLUSIVE")

    def test_all_error_fields_override_payloads_in_both_sources(self):
        signals = '[signals.chamber]\nsource="prusalink"\npointer="/temperature"\n'
        checks = HOLDS_LE_70 + "window = { start_offset_s = 1 }\n\n" + in_band(
            0.1, window="{ start_offset_s = 1 }")
        for source in ("dragon", "prusalink"):
            for key in ("parse_error", "decode_error", "parse_error_kind", "error"):
                for error in ("bad", "", False, {}, []):
                    with self.subTest(source=source, key=key, error=error):
                        b = EvidenceBuilder().start()
                        if source == "dragon":
                            b.value(65, 0.1)
                        else:
                            b.prusalink({"temperature": 65}, S // 10, S // 5)
                        b.records[-1][key] = error
                        b.complete(2 * S)
                        p = profile(checks, max_hold_s=10,
                                    signals=signals if source == "prusalink" else None)
                        findings = json.loads(assess(b.to_bytes(), p))["findings"]
                        self.assertEqual([f["result"] for f in findings],
                                         ["INCONCLUSIVE", "INCONCLUSIVE"])
                        self.assertTrue(all(f["coverage"]["known_fraction"] == 0 for f in findings))

    def test_post_terminal_records_cannot_change_either_finding(self):
        checks = HOLDS_LE_70 + "window={start_offset_s=1}\n\n" + in_band(
            0.5, window="{start_offset_s=1,end_offset_s=10}")
        for source in ("dragon", "prusalink"):
            with self.subTest(source=source):
                b = EvidenceBuilder().start()
                for i in range(4):
                    if source == "dragon":
                        b.value(65, i + 0.1)
                    else:
                        b.prusalink({"temperature": 65}, i * S + S // 10, i * S + S // 5)
                b.complete(4 * S)
                signals = None if source == "dragon" else (
                    '[signals.chamber]\nsource="prusalink"\npointer="/temperature"\n')
                p = profile(checks, max_hold_s=10, signals=signals)
                before = json.loads(assess(b.to_bytes(), p))["findings"]
                # Include a forged earlier timestamp, a duplicate request id,
                # a missing sequence, and later run markers after termination.
                b.other("http_request", S, run_id=b.run_id, endpoint=STATE, request_id=1)
                b.prusalink({"temperature": 80}, S, 2 * S)
                b.value(80, 4.5).start(5 * S).complete(6 * S)
                b.records[-1]["sequence"] += 4
                after = json.loads(assess(b.to_bytes(), p))["findings"]
                self.assertEqual(before, after)

    def test_records_beyond_time_horizon_but_before_terminal_are_ignored(self):
        b = EvidenceBuilder().start().value(65, 0.1).value(75, 5).complete(4 * S)
        f = finding(b.to_bytes(), HOLDS_LE_70 + "window={start_offset_s=1,end_offset_s=6}\n")
        self.assertEqual(f["result"], "INCONCLUSIVE")
        self.assertEqual(f["violations"]["count"], 0)
        self.assertEqual(f["coverage"]["true_ns"], 3 * S)

    def test_explicit_window_still_requires_terminal(self):
        b = steady([65, 75, 65])
        f = finding(b.to_bytes(drop={len(b.records)}),
                    HOLDS_LE_70 + "window={start_offset_s=1,end_offset_s=3}\n")
        self.assertEqual(f["result"], "INCONCLUSIVE")
        self.assertEqual(f["reasons"], ["run_not_terminated"])

    def test_gap_before_pair_blocks_point_violation_as_well_as_hold(self):
        b = EvidenceBuilder().start()
        b.other("http_request", S // 10, run_id=b.run_id, endpoint=STATE, request_id=1)
        b.value(75, 1).complete(2 * S)
        checks = HOLDS_LE_70 + "\n" + in_band(0.5)
        for data in (b.to_bytes(), b.to_bytes(drop={2})):
            actual = json.loads(assess(data, profile(checks, max_hold_s=10)))["findings"]
            self.assertEqual([f["result"] for f in actual], ["INCONCLUSIVE"] * 2)
            self.assertEqual(actual[1]["measurement"]["fraction_in_band"],
                             {"lower": 0, "upper": 1})

    def test_invalid_terminal_identity_or_time_is_inconclusive(self):
        for field, value in (("run_id", "different"), ("monotonic_ns", 0)):
            b = EvidenceBuilder().start(S).value(65, 1.1).complete(2 * S)
            b.records[-1][field] = value
            with self.subTest(field=field):
                f = finding(b.to_bytes(), HOLDS_LE_70)
                self.assertEqual(f["result"], "INCONCLUSIVE")
                self.assertIn("run_invalid", f["reasons"])

    def test_malformed_request_ids_are_unknown_not_crashes(self):
        for request_id in (None, True, [], {}, 1.0, ""):
            b = EvidenceBuilder().start().value(65, 0.1).complete(2 * S)
            b.records[1]["request_id"] = b.records[2]["request_id"] = request_id
            with self.subTest(request_id=request_id):
                self.assertEqual(finding(b.to_bytes(), HOLDS_LE_70)["result"], "INCONCLUSIVE")


class ExactNumericTests(TestCase):
    def test_outward_rounding_does_not_admit_reversed_or_empty_windows(self):
        for start, end in ((1.5e-9, 1.4e-9), (0.5e-9, 0.5e-9), (-0.1e-9, 1)):
            with self.subTest(start=start, end=end):
                checks = HOLDS_LE_70 + f"window={{start_offset_s={start},end_offset_s={end}}}\n"
                with self.assertRaises(ProfileError):
                    load_profile(profile(checks))

    def test_integer_comparisons_across_binary64_boundary(self):
        for value in (2**53 - 1, 2**53, 2**53 + 1, 2**53 + 2, -(2**53 + 1), 10**400):
            for op, threshold, expected in (
                ("eq", value, "PASS"), ("ne", value + 1, "PASS"),
                ("lt", value + 1, "PASS"), ("le", value, "PASS"),
                ("gt", value - 1, "PASS"), ("ge", value, "PASS"),
                ("eq", value + 1, "FAIL"), ("lt", value, "FAIL"),
            ):
                with self.subTest(value=value, op=op):
                    b = steady([value] * 3)
                    checks = HOLDS_LE_70.replace('op = "le", value = 70.0',
                                                f'op = "{op}", value = {threshold}')
                    f = finding(b.to_bytes(), checks + "window={start_offset_s=1}\n")
                    self.assertEqual(f["result"], expected)
                    self.assertEqual(f["criterion"]["predicate"]["value"], threshold)
                    self.assertIsInstance(f["criterion"]["predicate"]["value"], int)

    def test_exact_integer_band_and_reference_subtraction(self):
        value = 10**400
        b = EvidenceBuilder().start()
        for i in range(3):
            b.dragon({"value": value + 1, "reference": value}, i * S + S // 10,
                     i * S + S // 5)
        b.complete(3 * S)
        signals = ('[signals.chamber]\nsource="dragon"\nendpoint="/api/v2/state"\n'
                   'pointer="/value"\nreference_pointer="/reference"\n')
        p = profile(in_band(1, low=1, high=1, window="{start_offset_s=1}"),
                    signals=signals, max_hold_s=10)
        self.assertEqual(json.loads(assess(b.to_bytes(), p))["findings"][0]["result"], "PASS")
        p = profile(in_band(1, low=value + 1, high=value + 1, window="{start_offset_s=1}"),
                    max_hold_s=10)
        self.assertEqual(json.loads(assess(steady([value + 1] * 3).to_bytes(), p))
                         ["findings"][0]["result"], "PASS")

    def test_seconds_conversion_uses_exact_ratio_and_declared_rounding(self):
        for seconds in (0, 1, 1.2, 1.3, 0.0000000005, 0.0000000019,
                        2**53 + 1, 1e300, 10**400):
            with self.subTest(seconds=seconds):
                rational_ns = Fraction(seconds) * S
                floor = rational_ns.numerator // rational_ns.denominator
                ceiling = -(-rational_ns.numerator // rational_ns.denominator)
                checks = HOLDS_LE_70 + f"window={{end_offset_s={seconds!r}}}\n"
                if seconds == 0:
                    with self.assertRaises(ProfileError):
                        load_profile(profile(checks))
                    continue
                loaded = load_profile(profile(checks))
                self.assertEqual(loaded.checks[0].window.end_offset_ns, ceiling)
                checks = HOLDS_LE_70 + f"window={{start_offset_s={seconds!r}}}\n"
                self.assertEqual(load_profile(profile(checks)).checks[0].window.start_offset_ns, floor)
                if seconds <= 3600:
                    loaded = load_profile(profile(HOLDS_LE_70, max_hold_s=seconds))
                    self.assertEqual(loaded.max_hold_ns, floor)

    def test_fraction_decision_does_not_round_up_to_threshold(self):
        # True for exactly 1/10 of the window, below the parsed binary64 0.1.
        b = EvidenceBuilder().start().value(65, 0, 0).complete(10 * S)
        f = finding(b.to_bytes(), in_band(0.1), hold=1)
        self.assertEqual(f["measurement"]["in_band_ns"], S)
        self.assertEqual(f["result"], "INCONCLUSIVE")


class ExpandedDeletionTests(TestCase):
    def test_malformed_pairing_deletions_never_improve_findings(self):
        # 80 deterministic fixtures * 40 deletions, both check families.
        results_seen = set()
        for seed in range(80):
            rng = random.Random(seed)
            b = steady([rng.choice([65, 65, 75, None]) for _ in range(5)])
            style = seed % 8
            if style in (1, 2):
                # Duplicate request or response, before or after the pair.
                record = deepcopy(b.records[1 if style == 1 else 2])
                b.records.insert(rng.choice([1, 4, 8]), record)
            elif style == 3:
                b.records[4]["request_id"] = b.records[1]["request_id"]
            elif style == 4:
                b.records[2]["parse_error"] = "bad"
            elif style == 5:
                b.records[2]["monotonic_ns"] = 0  # reversed completion
            elif style == 6:
                b.records[1]["request_id"] = []
            elif style == 7:
                b.records.insert(2, deepcopy(b.records[0]))  # ambiguous run
            for sequence, record in enumerate(b.records, 1):
                record["sequence"] = sequence
            checks = HOLDS_LE_70 + "window={start_offset_s=1}\n\n" + in_band(
                rng.choice([0.1, 0.5, 0.9]), window="{start_offset_s=1}")
            p = profile(checks, max_hold_s=10)
            before = json.loads(assess(b.to_bytes(), p))["findings"]
            results_seen.update(f["result"] for f in before)
            sequences = list(range(1, len(b.records) + 1))
            for trial in range(40):
                drop = ({sequences[trial]} if trial < len(sequences) else
                        set(rng.sample(sequences, rng.randint(1, len(sequences) - 1))))
                after = json.loads(assess(b.to_bytes(drop), p))["findings"]
                with self.subTest(seed=seed, drop=sorted(drop)):
                    for original, reduced in zip(before, after):
                        if reduced["result"] == "PASS":
                            self.assertEqual(original["result"], "PASS")
                        if original["result"] == "PASS":
                            self.assertNotEqual(reduced["result"], "FAIL")
                        if "measurement" in original and "measurement" in reduced:
                            a = original["measurement"]["fraction_in_band"]
                            z = reduced["measurement"]["fraction_in_band"]
                            self.assertLessEqual(z["lower"], a["lower"])
                            self.assertGreaterEqual(z["upper"], a["upper"])
        self.assertEqual(results_seen, {"PASS", "FAIL", "INCONCLUSIVE"})

    def test_replay_with_malformed_evidence_is_byte_identical(self):
        b = steady([65, 75, 65])
        b.records[1]["request_id"] = {}
        p = profile(HOLDS_LE_70 + "\n" + in_band(0.5), max_hold_s=10)
        self.assertEqual(assess(b.to_bytes(), p), assess(b.to_bytes(), p))
