"""Reproduce PR #52's four old-head defects and kill targeted semantic mutants.

Run from the checkout with Python 3.11+. All mutations and historical tests run
in temporary copies; the working tree is never modified. No network is used.
"""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "5e20cf0325aa4137e88ba54908ed7722ee45f6d4"
REGRESSIONS = "tests.test_assess_regressions"
REVIEW = REGRESSIONS + ".ReviewRegressionTests"
ENGINE = "src/dragonsniff/assess/engine.py"
PROFILE = "src/dragonsniff/assess/profile.py"

# Each mutant has a specific test. A kill must be an assertion failure, never
# a missing helper, import, syntax, runtime exception, or harness failure.
MUTATIONS = [
    ("ignore missing prefix", ENGINE,
     "incomplete = evidence.missing_between(1, terminal_sequence)", "incomplete = False",
     REVIEW + ".test_deleting_duplicate_request_cannot_create_pass"),
    ("trust parse-error payload", ENGINE, "and _error_free(completion)", "and True",
     REVIEW + ".test_explicit_parse_error_cannot_create_pass"),
    ("trust unhealthy source payload", ENGINE,
     'record.get("source_state") == "healthy" and _error_free(record)',
     'True and _error_free(record)',
     "tests.test_assess_engine.PrusaLinkObservationTests.test_unhealthy_or_malformed_observations_are_unknown"),
    ("trust source error payload", ENGINE, "and _error_free(record)", "and True",
     REGRESSIONS + ".EvidenceBoundaryTests.test_all_error_fields_override_payloads_in_both_sources"),
    ("admit post-terminal observations", ENGINE,
     'run.started["sequence"] <= record["sequence"] <= run.terminal["sequence"]\n'
     '                and run.started["monotonic_ns"] <= record["monotonic_ns"] <= run.terminal["monotonic_ns"]',
     'run.started["sequence"] <= record["sequence"]',
     REVIEW + ".test_post_terminal_violation_cannot_create_fail"),
    ("coerce integer criteria to float", PROFILE,
     '    return value\n\n\ndef _string', '    return float(value)\n\n\ndef _string',
     REVIEW + ".test_integer_threshold_is_not_rounded_to_adjacent_integer"),
    ("round duration before decision", ENGINE,
     'if true_ns * denominator >= numerator * window_ns:', 'if lower >= check.min_fraction:',
     REGRESSIONS + ".ExactNumericTests.test_fraction_decision_does_not_round_up_to_threshold"),
    ("round holds upward", PROFILE,
     '_seconds_to_ns("assumptions.max_hold_s", max_hold_s)',
     '_seconds_to_ns("assumptions.max_hold_s", max_hold_s, ceiling=True)',
     REGRESSIONS + ".ExactNumericTests.test_seconds_conversion_uses_exact_ratio_and_declared_rounding"),
    ("round window end downward", PROFILE,
     '_seconds_to_ns(f"{where}.end_offset_s", raw_end, ceiling=True)',
     '_seconds_to_ns(f"{where}.end_offset_s", raw_end)',
     REGRESSIONS + ".ExactNumericTests.test_seconds_conversion_uses_exact_ratio_and_declared_rounding"),
    ("round window start upward", PROFILE,
     '_seconds_to_ns(f"{where}.start_offset_s", raw_start)',
     '_seconds_to_ns(f"{where}.start_offset_s", raw_start, ceiling=True)',
     REGRESSIONS + ".ExactNumericTests.test_seconds_conversion_uses_exact_ratio_and_declared_rounding"),
    ("ignore hold expiry", ENGINE,
     'end = min(until, limit(attempt.start_ns + hold_ns))', 'end = until',
     REGRESSIONS + ".EvidenceBoundaryTests.test_hold_expires_before_a_long_gap_or_terminal"),
    ("combine unknown as true", ENGINE,
     'if first is True and second is True:', 'if first is True or second is True:',
     "tests.test_assess_engine.TimeInBandTests.test_invalid_sample_interval_is_unknown_not_held"),
    ("ignore overlapping unknown", ENGINE,
     'if active[0] and not active[1] and not active[2]:', 'if active[0]:',
     "tests.test_assess_engine.IntegrationRuleTests.test_overlapping_pieces_are_true_only_when_all_agree"),
    ("let later run marker affect first run", "src/dragonsniff/assess/evidence.py",
     'if any(terminal is None or r["sequence"] < terminal["sequence"] for r in starts[1:]):',
     'if len(starts) > 1:',
     REGRESSIONS + ".EvidenceBoundaryTests.test_post_terminal_records_cannot_change_either_finding"),
    ("ignore terminal identity", "src/dragonsniff/assess/evidence.py",
     'not run_id or terminal.get("run_id") != run_id', 'False',
     REGRESSIONS + ".EvidenceBoundaryTests.test_invalid_terminal_identity_or_time_is_inconclusive"),
]


def run_tests(directory, *tests):
    env = dict(os.environ, PYTHONPATH=str(directory / "src"), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-W", "error", "-m", "unittest", "-v", *tests],
                          cwd=directory, env=env, text=True, capture_output=True)


def main():
    with tempfile.TemporaryDirectory(prefix="assess-correctness-") as temporary:
        root = Path(temporary)
        old = root / "old"
        old.mkdir()
        archive = subprocess.run(["git", "archive", BASELINE], cwd=ROOT,
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
        shutil.copyfile(ROOT / "tests/test_assess_regressions.py",
                        old / "tests/test_assess_regressions.py")
        result = run_tests(old, REVIEW)
        output = result.stdout + result.stderr
        print(output)
        if result.returncode == 0 or "FAILED (failures=4)" not in output or "ERROR" in output:
            raise SystemExit("Historical proof did not produce four semantic assertion failures")
        print(f"BASELINE {BASELINE}: four intended regression failures")

        fixed = run_tests(ROOT, REVIEW)
        if fixed.returncode:
            raise SystemExit(fixed.stdout + fixed.stderr)
        print("FIXED: four regressions pass unchanged")

        killed = 0
        for index, (name, filename, before, after, test) in enumerate(MUTATIONS):
            directory = root / f"mutant-{index}"
            for folder in ("src", "tests"):
                shutil.copytree(ROOT / folder, directory / folder,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            path = directory / filename
            source = path.read_text()
            if source.count(before) != 1:
                raise SystemExit(f"Mutation anchor must match exactly once: {name}")
            path.write_text(source.replace(before, after, 1))
            result = run_tests(directory, test)
            output = result.stdout + result.stderr
            if result.returncode and "FAIL:" in output and "ERROR" not in output:
                killed += 1
                print(f"KILLED: {name} — {test}")
            else:
                print(output)
                raise SystemExit(f"Survived or invalid mutation: {name}")
        print(f"MUTATIONS: {killed}/{len(MUTATIONS)} killed by semantic assertions")


if __name__ == "__main__":
    main()
