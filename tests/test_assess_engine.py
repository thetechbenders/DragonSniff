import json
from unittest import TestCase

from dragonsniff.assess import ProfileError, assess
from dragonsniff.assess.engine import _integrate
from tests.assess_fixtures import HOLDS_LE_70, S, EvidenceBuilder, in_band, profile, steady

AFTER_FIRST = "{ start_offset_s = 1.0 }"
HOLDS_AFTER_FIRST = HOLDS_LE_70 + f"window = {AFTER_FIRST}\n"


def run(evidence: EvidenceBuilder | bytes, checks: str, **kw) -> list[dict]:
    data = evidence if isinstance(evidence, bytes) else evidence.to_bytes()
    return json.loads(assess(data, profile(checks, **kw)))["findings"]


def one(evidence, checks, **kw) -> dict:
    (finding,) = run(evidence, checks, **kw)
    return finding


class HoldsThroughoutTests(TestCase):
    def test_pass_requires_declared_hold_covering_the_whole_window(self) -> None:
        evidence = steady([65.0] * 10)  # samples at 0.1 s + n, 50 ms each
        finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.05)
        self.assertEqual(finding["result"], "PASS")
        self.assertEqual(finding["reasons"], [])
        self.assertEqual(finding["coverage"]["unknown_ns"], 0)

    def test_strict_no_hold_cannot_prove_a_continuous_claim(self) -> None:
        finding = one(steady([65.0] * 10), HOLDS_AFTER_FIRST)
        self.assertEqual(finding["result"], "INCONCLUSIVE")
        self.assertIn("no_hold_declared", finding["reasons"])
        self.assertEqual(finding["coverage"]["true_ns"], 0)

    def test_hold_boundary_is_exact(self) -> None:
        # Cadence 1.0 s, request 50 ms. A value is guaranteed only until
        # request_start + hold, so the next request's own 50 ms needs 1.05 s.
        evidence = steady([65.0] * 5)
        self.assertEqual(
            one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.0)["result"], "INCONCLUSIVE"
        )
        self.assertEqual(
            one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.049)["result"], "INCONCLUSIVE"
        )
        self.assertEqual(one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.05)["result"], "PASS")

    def test_proven_violation_fails_even_without_hold(self) -> None:
        evidence = steady([65.0, 65.0, 75.0, 65.0])
        finding = one(evidence, HOLDS_AFTER_FIRST)
        self.assertEqual(finding["result"], "FAIL")
        self.assertEqual(finding["reasons"], [])
        self.assertEqual(finding["violations"]["sequences"], [7])

    def test_violation_straddling_window_edge_is_not_proof(self) -> None:
        evidence = EvidenceBuilder().start(0)
        evidence.value(75.0, 0.95, 1.05)  # straddles the 1.0 s window start
        evidence.value(65.0, 2.0).complete(3 * S)
        finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=5)
        self.assertEqual(finding["result"], "INCONCLUSIVE")
        self.assertIn("violation_at_window_edge", finding["reasons"])

    def test_held_violation_is_not_proof(self) -> None:
        evidence = EvidenceBuilder().start(0)
        evidence.value(75.0, 0.5, 0.55)  # before the window, held into it
        evidence.value(65.0, 1.5).complete(3 * S)
        finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=2)
        self.assertEqual(finding["result"], "INCONCLUSIVE")
        self.assertIn("held_violation", finding["reasons"])

    def test_invalid_values_are_unknown_never_coerced(self) -> None:
        bad_values = {
            "nan": float("nan"),
            "infinity": float("inf"),
            "null": None,
            "string": "65",
            "boolean": True,
        }
        for name, bad in bad_values.items():
            with self.subTest(name):
                evidence = steady([65.0, 65.0, bad, 65.0, 65.0])
                finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.05)
                self.assertEqual(finding["result"], "INCONCLUSIVE")
                self.assertIn("invalid_observations", finding["reasons"])
                self.assertEqual(finding["violations"]["count"], 0)

    def test_failed_or_unparsed_responses_are_unknown(self) -> None:
        variants = {
            "http_error": {"failed": True},
            "ok false": {"ok": False},
            "parse error": {"parse_error": "bad"},
        }
        for name, options in variants.items():
            with self.subTest(name):
                evidence = EvidenceBuilder().start(0)
                for index in range(5):
                    extra = options if index == 2 else {}
                    parsed = None if "parse_error" in extra else {
                        "sensors": {"chamber": {"temperature_c": 65.0}}}
                    if "parse_error" not in extra:
                        evidence.value(65.0, 0.1 + index, **extra)
                    else:
                        evidence.dragon(parsed, round((0.1 + index) * S),
                                        round((0.15 + index) * S), **extra)
                evidence.complete(round(5.1 * S))
                finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.05)
                self.assertEqual(finding["result"], "INCONCLUSIVE")
                self.assertIn("invalid_observations", finding["reasons"])

    def test_missing_field_is_unknown(self) -> None:
        evidence = EvidenceBuilder().start(0)
        for index in range(5):
            parsed = {"sensors": {}} if index == 2 else {
                "sensors": {"chamber": {"temperature_c": 65.0}}}
            evidence.dragon(parsed, round((0.1 + index) * S), round((0.15 + index) * S))
        evidence.complete(round(5.1 * S))
        finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.05)
        self.assertEqual(finding["result"], "INCONCLUSIVE")

    def test_sequence_gap_withdraws_holds(self) -> None:
        evidence = steady([65.0] * 5)
        data = evidence.to_bytes(drop=[6])  # the request of the third sample
        finding = one(data, HOLDS_AFTER_FIRST, max_hold_s=1.05)
        self.assertEqual(finding["result"], "INCONCLUSIVE")
        self.assertIn("sequence_gap", finding["reasons"])

    def test_unrelated_record_gap_also_withdraws_holds(self) -> None:
        evidence = EvidenceBuilder().start(0)
        evidence.value(65.0, 0.1).value(65.0, 1.1)
        evidence.other("source_observation", round(1.5 * S), source="other")
        evidence.value(65.0, 2.1).value(65.0, 3.1).complete(round(4.1 * S))
        full = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.05)
        self.assertEqual(full["result"], "PASS")
        gapped = one(evidence.to_bytes(drop=[6]), HOLDS_AFTER_FIRST, max_hold_s=1.05)
        self.assertEqual(gapped["result"], "INCONCLUSIVE")

    def test_run_structure_makes_window_undefined(self) -> None:
        unterminated = EvidenceBuilder().start(0).value(65.0, 0.1)
        self.assertEqual(
            one(unterminated, HOLDS_LE_70, max_hold_s=5)["reasons"], ["run_not_terminated"]
        )
        missing = EvidenceBuilder().value(65.0, 0.1)
        self.assertEqual(one(missing, HOLDS_LE_70, max_hold_s=5)["reasons"], ["run_missing"])
        twice = EvidenceBuilder().start(0).start(1).value(65.0, 0.1).complete(S)
        self.assertEqual(one(twice, HOLDS_LE_70, max_hold_s=5)["reasons"], ["run_ambiguous"])

    def test_window_beyond_evidence_is_unknown(self) -> None:
        checks = HOLDS_LE_70 + "window = { start_offset_s = 1.0, end_offset_s = 60.0 }\n"
        finding = one(steady([65.0] * 5), checks, max_hold_s=1.05)
        self.assertEqual(finding["result"], "INCONCLUSIVE")
        self.assertIn("evidence_ended", finding["reasons"])

    def test_empty_run_has_no_observations(self) -> None:
        evidence = EvidenceBuilder().start(0).complete(10 * S)
        finding = one(evidence, HOLDS_LE_70, max_hold_s=100)
        self.assertEqual(finding["result"], "INCONCLUSIVE")
        self.assertIn("no_observations", finding["reasons"])

    def test_other_runs_are_ignored(self) -> None:
        evidence = steady([65.0] * 5)
        foreign = EvidenceBuilder(run_id="f" * 32)
        foreign.value(99.0, 2.5)
        for record in foreign.records:
            record["sequence"] = len(evidence.records) + 1
            evidence.records.insert(len(evidence.records) - 1, record)
        for index, record in enumerate(evidence.records, start=1):
            record["sequence"] = index
        self.assertEqual(one(evidence, HOLDS_AFTER_FIRST)["violations"]["count"], 0)

    def test_reference_pointer_subtracts_within_one_response(self) -> None:
        signals = (
            "[signals.chamber]\n"
            'source = "dragon"\n'
            'endpoint = "/api/v2/state"\n'
            'pointer = "/t"\n'
            'reference_pointer = "/target"\n'
        )
        evidence = EvidenceBuilder().start(0)
        for index, (t, target) in enumerate([(65, 65), (66, 65), (64, 65), (70, 65)]):
            evidence.dragon({"t": t, "target": target},
                            round((0.1 + index) * S), round((0.15 + index) * S))
        evidence.complete(round(4.1 * S))
        checks = HOLDS_AFTER_FIRST.replace("le\", value = 70.0", "between\", low = -1.5, high = 1.5")
        finding = one(evidence, checks, max_hold_s=1.05, signals=signals)
        self.assertEqual(finding["result"], "FAIL")  # 70 - 65 = 5 is out of band

    def test_string_equality(self) -> None:
        signals = (
            "[signals.chamber]\n"
            'source = "dragon"\n'
            'endpoint = "/api/v2/state"\n'
            'pointer = "/mode"\n'
        )
        evidence = EvidenceBuilder().start(0)
        for index, mode in enumerate(["on", "on", "off"]):
            evidence.dragon({"mode": mode}, round((0.1 + index) * S), round((0.15 + index) * S))
        evidence.complete(round(3.1 * S))
        checks = HOLDS_AFTER_FIRST.replace('op = "le", value = 70.0', 'op = "eq", value = "on"')
        self.assertEqual(one(evidence, checks, max_hold_s=1.05, signals=signals)["result"], "FAIL")


class PrusaLinkObservationTests(TestCase):
    SIGNALS = (
        "[signals.chamber]\n"
        'source = "prusalink"\n'
        'pointer = "/bed_temperature_c"\n'
        'source_id = "printer"\n'
    )

    def build(self, **overrides) -> EvidenceBuilder:
        evidence = EvidenceBuilder().start(0)
        for index in range(5):
            kw = overrides if index == 2 else {}
            evidence.prusalink({"bed_temperature_c": 60.0},
                               round((0.1 + index) * S), round((0.4 + index) * S), **kw)
        return evidence.complete(round(5.1 * S))

    def test_interval_is_observed_start_to_record_time(self) -> None:
        finding = one(self.build(), HOLDS_AFTER_FIRST, max_hold_s=1.3, signals=self.SIGNALS)
        self.assertEqual(finding["result"], "PASS")
        self.assertEqual(
            one(self.build(), HOLDS_AFTER_FIRST, max_hold_s=1.29, signals=self.SIGNALS)["result"],
            "INCONCLUSIVE",
        )

    def test_unhealthy_or_malformed_observations_are_unknown(self) -> None:
        for name, overrides in {
            "auth error": {"state": "auth_error"},
            "no observed time": {"observed": "soon"},
            "observed after completion": {"observed": 10 * S},
        }.items():
            with self.subTest(name):
                finding = one(self.build(**overrides), HOLDS_AFTER_FIRST,
                              max_hold_s=1.3, signals=self.SIGNALS)
                self.assertEqual(finding["result"], "INCONCLUSIVE")

    def test_dragonsniff_freshness_is_never_consulted(self) -> None:
        evidence = self.build()
        for record in evidence.records:
            if record["kind"] == "source_observation":
                record["freshness"] = {"state": "stale", "sample_age_ms": 999999}
        finding = one(evidence, HOLDS_AFTER_FIRST, max_hold_s=1.3, signals=self.SIGNALS)
        self.assertEqual(finding["result"], "PASS")


class TimeInBandTests(TestCase):
    def worked_example(self) -> EvidenceBuilder:
        # Requests at 1.0, 2.0, 3.0 s (50 ms each), values 65, 70, 65; end 4.0 s.
        evidence = EvidenceBuilder().start(0)
        for start, value in [(1.0, 65.0), (2.0, 70.0), (3.0, 65.0)]:
            evidence.value(value, start, start + 0.05)
        return evidence.complete(4 * S)

    def test_hand_computed_integration(self) -> None:
        # Window [1.0, 4.0] = 3.0 s, hold 1.05 s:
        #   [1.00,1.05] unknown  (first sample, nothing held before it)
        #   [1.05,2.00] in band  (65 held)
        #   [2.00,2.05] unknown  (65 held or 70 observed)
        #   [2.05,3.00] out      (70 held)
        #   [3.00,3.05] unknown
        #   [3.05,4.00] in band  (65 held to the terminal record)
        finding = one(self.worked_example(), in_band(0.6, window=AFTER_FIRST), max_hold_s=1.05)
        measurement = finding["measurement"]
        self.assertEqual(measurement["in_band_ns"], 1_900_000_000)
        self.assertEqual(measurement["out_of_band_ns"], 950_000_000)
        self.assertEqual(measurement["unknown_ns"], 150_000_000)
        self.assertAlmostEqual(measurement["fraction_in_band"]["lower"], 1.9 / 3)
        self.assertAlmostEqual(measurement["fraction_in_band"]["upper"], 2.05 / 3)
        self.assertAlmostEqual(measurement["fraction_of_known_in_band"], 1.9 / 2.85)

    def test_decision_uses_worst_case_bounds(self) -> None:
        for minimum, expected in [(0.6, "PASS"), (0.65, "INCONCLUSIVE"), (0.7, "FAIL")]:
            with self.subTest(minimum):
                finding = one(self.worked_example(), in_band(minimum, window=AFTER_FIRST),
                              max_hold_s=1.05)
                self.assertEqual(finding["result"], expected)
                if expected != "INCONCLUSIVE":
                    self.assertEqual(finding["reasons"], [])

    def test_time_weighting_is_not_sample_weighting(self) -> None:
        # Ten quick in-band samples, then one out-of-band sample held for 8 s.
        evidence = EvidenceBuilder().start(0)
        for index in range(10):
            evidence.value(65.0, 1.0 + index * 0.1, 1.0 + index * 0.1 + 0.01)
        evidence.value(80.0, 2.0, 2.01).complete(10 * S)
        finding = one(evidence, in_band(0.5, window=AFTER_FIRST), max_hold_s=8.0)
        self.assertLess(finding["measurement"]["fraction_in_band"]["upper"], 0.2)
        self.assertEqual(finding["result"], "FAIL")

    def test_no_hold_leaves_the_fraction_unbounded(self) -> None:
        finding = one(self.worked_example(), in_band(0.1, window=AFTER_FIRST))
        self.assertEqual(finding["measurement"]["fraction_in_band"], {"lower": 0.0, "upper": 1.0})
        self.assertIsNone(finding["measurement"]["fraction_of_known_in_band"])
        self.assertEqual(finding["result"], "INCONCLUSIVE")

    def test_zero_min_fraction_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProfileError, "min_fraction"):
            assess(self.worked_example().to_bytes(), profile(in_band(0.0)))

    def test_invalid_sample_interval_is_unknown_not_held(self) -> None:
        # Requests at 1.0, 2.0, 3.0 s (50 ms); the middle one is invalid.
        #   [1.00,1.05] unknown, [1.05,2.00] in band (0.95 s)
        #   [2.00,2.05] unknown: held 65 or no value, never assumed in band
        #   [2.05,3.00] unknown: an invalid attempt holds nothing
        #   [3.00,3.05] unknown, [3.05,4.00] in band (0.95 s)
        evidence = EvidenceBuilder().start(0)
        evidence.value(65.0, 1.0, 1.05).value(None, 2.0, 2.05).value(65.0, 3.0, 3.05)
        evidence.complete(4 * S)
        measurement = one(evidence, in_band(0.5, window=AFTER_FIRST), max_hold_s=1.05)["measurement"]
        self.assertEqual(measurement["in_band_ns"], 1_900_000_000)
        self.assertEqual(measurement["out_of_band_ns"], 0)
        self.assertEqual(measurement["unknown_ns"], 1_100_000_000)

    def test_holds_never_extend_past_the_terminal_record(self) -> None:
        evidence = steady([65.0] * 4, duration_s=4.1)
        evidence.value(65.0, 4.5, 4.55)  # recorded after the terminal record
        checks = in_band(0.5, window="{ start_offset_s = 1.0, end_offset_s = 10.0 }")
        measurement = one(evidence, checks, max_hold_s=60.0)["measurement"]
        # Known time ends at the terminal record (4.1 s); 4.1 to 10.0 s is unknown.
        self.assertEqual(measurement["in_band_ns"], 3_100_000_000)
        self.assertEqual(measurement["unknown_ns"], 5_900_000_000)


class IntegrationRuleTests(TestCase):
    def test_overlapping_pieces_are_true_only_when_all_agree(self) -> None:
        self.assertEqual(_integrate([(0, 10, True), (5, 15, None)], 0, 20), (5, 0, 15))
        self.assertEqual(_integrate([(0, 10, True), (5, 15, False)], 0, 20), (5, 5, 10))
        self.assertEqual(_integrate([(0, 10, False), (5, 15, False)], 0, 20), (0, 15, 5))
        self.assertEqual(_integrate([(0, 10, True), (10, 20, True)], 0, 20), (20, 0, 0))

    def test_integration_clips_to_the_window_and_counts_gaps_unknown(self) -> None:
        self.assertEqual(_integrate([(-5, 5, True), (8, 30, False)], 0, 10), (5, 2, 3))
        self.assertEqual(_integrate([], 0, 10), (0, 0, 10))
