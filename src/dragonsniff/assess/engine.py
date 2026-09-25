"""Evaluate checks against evidence. Pure functions over validated inputs.

Truth values are three-valued: True, False, or None (unknown). Nothing unknown
is ever coerced to a convenient value.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterator

from .evidence import Evidence
from .profile import Check, Profile, Signal

MAX_LISTED_RECORDS = 32
DRAGON_COMPLETIONS = frozenset({"http_response", "http_error"})


@dataclass(frozen=True, slots=True)
class Attempt:
    """One observation attempt. value is None when the attempt is invalid."""

    start_ns: int
    end_ns: int
    first_sequence: int
    last_sequence: int
    value: Any
    valid: bool


# --- JSON Pointer ---------------------------------------------------------

_MISSING = object()


def _resolve(document: object, pointer: str) -> object:
    current = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


def _usable(value: object) -> bool:
    return _is_number(value) or isinstance(value, (bool, str))


def _extract(document: object, signal: Signal) -> tuple[bool, Any]:
    value = _resolve(document, signal.pointer)
    if value is _MISSING or not _usable(value):
        return False, None
    if signal.reference_pointer is None:
        return True, value
    reference = _resolve(document, signal.reference_pointer)
    if not _is_number(value) or reference is _MISSING or not _is_number(reference):
        return False, None
    try:
        difference = value - reference
    except OverflowError:
        return False, None
    if not _is_number(difference):
        return False, None
    return True, difference


# --- Observation extraction -------------------------------------------------


def _run_records(evidence: Evidence) -> Iterator[dict[str, Any]]:
    """Only this run's records inside both its sequence and time horizon."""
    run = evidence.run
    if run.problem is not None or run.started is None or run.terminal is None:
        return
    for record in evidence.records:
        if (run.started["sequence"] <= record["sequence"] <= run.terminal["sequence"]
                and run.started["monotonic_ns"] <= record["monotonic_ns"] <= run.terminal["monotonic_ns"]
                and record.get("run_id") == run.run_id):
            yield record


def _error_free(record: dict[str, Any]) -> bool:
    # Non-null error fields always win over an apparently usable payload.
    return all(record.get(key) is None for key in
               ("parse_error", "decode_error", "parse_error_kind", "error"))


def _dragon_attempts(evidence: Evidence, signal: Signal) -> list[Attempt]:
    requests: dict[object, list[dict[str, Any]]] = {}
    completions: dict[object, list[dict[str, Any]]] = {}
    unidentified: list[dict[str, Any]] = []
    for record in _run_records(evidence):
        if record.get("endpoint") != signal.endpoint:
            continue
        request_id = record.get("request_id")
        if (isinstance(request_id, bool) or not isinstance(request_id, (str, int))
                or request_id == ""):
            if record["kind"] in DRAGON_COMPLETIONS or record["kind"] == "http_request":
                unidentified.append(record)
            continue
        if record["kind"] == "http_request":
            requests.setdefault(request_id, []).append(record)
        elif record["kind"] in DRAGON_COMPLETIONS:
            completions.setdefault(record.get("request_id"), []).append(record)

    attempts: list[Attempt] = [
        Attempt(r["monotonic_ns"], r["monotonic_ns"], r["sequence"], r["sequence"], None, False)
        for r in unidentified
    ]
    for request_id in requests.keys() | completions.keys():
        opened = requests.get(request_id, [])
        closed = completions.get(request_id, [])
        if len(opened) == 1 and len(closed) == 1 and request_id is not None:
            request, completion = opened[0], closed[0]
            start, end = request["monotonic_ns"], completion["monotonic_ns"]
            ordered = completion["sequence"] > request["sequence"] and end >= start
            valid, value = False, None
            if (
                ordered
                and completion["kind"] == "http_response"
                and completion.get("ok") is True
                and _error_free(completion)
                and isinstance(completion.get("parsed"), dict)
            ):
                valid, value = _extract(completion["parsed"], signal)
            if not ordered:
                start = end = min(start, end)
            attempts.append(
                Attempt(start, end, request["sequence"], completion["sequence"], value, valid)
            )
            continue
        # Unpaired, duplicated, or unidentified: an attempt with no usable value.
        for record in opened + closed:
            stamp = record["monotonic_ns"]
            attempts.append(
                Attempt(stamp, stamp, record["sequence"], record["sequence"], None, False)
            )
    attempts.sort(key=lambda a: (a.first_sequence, a.last_sequence))
    return attempts


def _prusalink_attempts(evidence: Evidence, signal: Signal) -> list[Attempt]:
    attempts: list[Attempt] = []
    for record in _run_records(evidence):
        if record["kind"] != "source_observation" or record.get("source") != "prusalink":
            continue
        if signal.source_id is not None and record.get("source_id") != signal.source_id:
            continue
        end = record["monotonic_ns"]
        start = record.get("observed_monotonic_ns")
        sequence = record["sequence"]
        if isinstance(start, bool) or not isinstance(start, int) or start < 0 or start > end:
            attempts.append(Attempt(end, end, sequence, sequence, None, False))
            continue
        valid, value = False, None
        data = record.get("data")
        if record.get("source_state") == "healthy" and _error_free(record) and isinstance(data, dict):
            valid, value = _extract(data, signal)
        attempts.append(Attempt(start, end, sequence, sequence, value, valid))
    return attempts


def attempts_for(evidence: Evidence, signal: Signal) -> list[Attempt]:
    if signal.source == "dragon":
        return _dragon_attempts(evidence, signal)
    return _prusalink_attempts(evidence, signal)


# --- Predicates ---------------------------------------------------------------


def _truth(attempt: Attempt, predicate: dict[str, Any]) -> bool | None:
    if not attempt.valid:
        return None
    value, op = attempt.value, predicate["op"]
    if op == "between":
        if not _is_number(value):
            return None
        return predicate["low"] <= value <= predicate["high"]
    expected = predicate["value"]
    if op in {"eq", "ne"}:
        comparable = (
            (_is_number(value) and _is_number(expected))
            or (isinstance(value, bool) and isinstance(expected, bool))
            or (isinstance(value, str) and isinstance(expected, str))
        )
        if not comparable:
            return None
        return (value == expected) if op == "eq" else (value != expected)
    if not _is_number(value):
        return None
    return {
        "lt": value < expected,
        "le": value <= expected,
        "gt": value > expected,
        "ge": value >= expected,
    }[op]


def _combine(first: bool | None, second: bool | None) -> bool | None:
    if first is True and second is True:
        return True
    if first is False and second is False:
        return False
    return None


# --- Timeline -----------------------------------------------------------------


def _pieces(
    evidence: Evidence,
    attempts: list[Attempt],
    truths: list[bool | None],
    hold_ns: int,
    horizon_ns: int | None,
    terminal_sequence: int | None,
) -> list[tuple[int, int, bool | None]]:
    """Return (start, end, truth) pieces. Uncovered time is unknown."""

    def holdable(index: int) -> bool:
        attempt = attempts[index]
        if hold_ns <= 0 or not attempt.valid:
            return False
        if index + 1 < len(attempts):
            following = attempts[index + 1]
            if following.start_ns < attempt.end_ns:
                return False
            return not evidence.missing_between(
                attempt.first_sequence, following.last_sequence
            )
        if terminal_sequence is None:
            return False
        return not evidence.missing_between(attempt.first_sequence, terminal_sequence)

    def limit(value: int) -> int:
        return value if horizon_ns is None else min(value, horizon_ns)

    pieces: list[tuple[int, int, bool | None]] = []
    for index, attempt in enumerate(attempts):
        # The attempt's own interval: the previous held value or this value.
        if index > 0 and holdable(index - 1):
            previous = attempts[index - 1]
            reach = limit(previous.start_ns + hold_ns)
            split = min(attempt.end_ns, reach)
            if split > attempt.start_ns:
                pieces.append(
                    (attempt.start_ns, split, _combine(truths[index - 1], truths[index]))
                )
        # After the attempt: its own value, held.
        if holdable(index):
            if index + 1 < len(attempts):
                until = attempts[index + 1].start_ns
            else:
                until = horizon_ns if horizon_ns is not None else attempt.end_ns
            end = min(until, limit(attempt.start_ns + hold_ns))
            if end > attempt.end_ns:
                pieces.append((attempt.end_ns, end, truths[index]))
    return pieces


def _integrate(
    pieces: list[tuple[int, int, bool | None]], window_start: int, window_end: int
) -> tuple[int, int, int]:
    """Return (true_ns, false_ns, unknown_ns) over the window.

    Where pieces overlap, time is true only if every covering piece is true,
    false only if every covering piece is false, and unknown otherwise.
    Uncovered time is unknown. Integer nanoseconds keep the sum exact.
    """
    events: list[tuple[int, int, int]] = []  # (position, delta, slot)
    for start, end, truth in pieces:
        start, end = max(start, window_start), min(end, window_end)
        if end <= start:
            continue
        slot = 0 if truth is True else 1 if truth is False else 2
        events.append((start, 1, slot))
        events.append((end, -1, slot))
    events.sort()
    active = [0, 0, 0]
    true_ns = false_ns = 0
    position = window_start
    for point, delta, slot in events:
        if point > position:
            if active[0] and not active[1] and not active[2]:
                true_ns += point - position
            elif active[1] and not active[0] and not active[2]:
                false_ns += point - position
            position = point
        active[slot] += delta
    unknown_ns = (window_end - window_start) - true_ns - false_ns
    return true_ns, false_ns, unknown_ns


# --- Findings -----------------------------------------------------------------


def _listed(sequences: list[int]) -> dict[str, Any]:
    ordered = sorted(sequences)
    return {"count": len(ordered), "sequences": ordered[:MAX_LISTED_RECORDS]}


def _window(evidence: Evidence, check: Check) -> tuple[tuple[int, int] | None, list[str]]:
    run = evidence.run
    if run.problem is not None:
        return None, [run.problem]
    # Even an explicit window needs closure: a missing tail could contain a
    # duplicate request, another run marker, or a different terminal horizon.
    if run.terminal is None:
        return None, ["run_not_terminated"]
    started = run.started["monotonic_ns"]
    start = started + check.window.start_offset_ns
    if check.window.end_offset_ns is None:
        end = run.terminal["monotonic_ns"]
    else:
        end = started + check.window.end_offset_ns
    if end <= start:
        return None, ["window_empty"]
    return (start, end), []


def evaluate(evidence: Evidence, profile: Profile, check: Check) -> dict[str, Any]:
    signal = profile.signals[check.signal]
    finding: dict[str, Any] = {
        "id": check.id,
        "kind": check.kind,
        "signal": signal.describe(),
        "criterion": (
            {"predicate": check.predicate}
            if check.kind == "holds_throughout"
            else {
                "band": {"low": check.band[0], "high": check.band[1]},
                "min_fraction": check.min_fraction,
            }
        ),
    }
    window, problems = _window(evidence, check)
    if window is None:
        finding.update(
            {"result": "INCONCLUSIVE", "reasons": sorted(problems), "window": None}
        )
        return finding

    start, end = window
    run = evidence.run
    started = run.started["monotonic_ns"]
    terminal_sequence = run.terminal["sequence"]
    horizon = run.terminal["monotonic_ns"]

    if check.kind == "holds_throughout":
        predicate = check.predicate
    else:
        predicate = {"op": "between", "low": check.band[0], "high": check.band[1]}

    # A gap anywhere in the prefix could conceal a duplicate request id (or
    # a run marker), invalidating otherwise apparently independent evidence.
    # Suppress both holds and point violations, never just a final PASS label.
    incomplete = evidence.missing_between(1, terminal_sequence)
    attempts = attempts_for(evidence, signal)
    truths = [None if incomplete else _truth(attempt, predicate) for attempt in attempts]
    pieces = [] if incomplete else _pieces(
        evidence, attempts, truths, profile.max_hold_ns, horizon, terminal_sequence
    )
    true_ns, false_ns, unknown_ns = _integrate(pieces, start, end)

    overlapping = [
        index
        for index, attempt in enumerate(attempts)
        if attempt.end_ns >= start and attempt.start_ns <= end
    ]
    violations: list[int] = []
    edge_violations: list[int] = []
    unusable: list[int] = []
    invalid: list[int] = []
    for index in overlapping:
        attempt, truth = attempts[index], truths[index]
        contained = attempt.start_ns >= start and attempt.end_ns <= end
        if truth is False:
            (violations if contained else edge_violations).append(attempt.last_sequence)
        elif truth is None and not incomplete:
            unusable.append(attempt.last_sequence)
        if not attempt.valid:
            invalid.append(attempt.last_sequence)

    reasons: set[str] = set()
    if profile.max_hold_ns == 0:
        reasons.add("no_hold_declared")
    if unknown_ns > 0:
        reasons.add("coverage_incomplete")
    if not overlapping:
        reasons.add("no_observations")
    if unusable:
        reasons.add("invalid_observations")
    if incomplete:
        reasons.add("sequence_gap")
    if end > horizon:
        reasons.add("evidence_ended")
    if edge_violations:
        reasons.add("violation_at_window_edge")

    window_ns = end - start
    finding["window"] = {
        "start_monotonic_ns": start,
        "end_monotonic_ns": end,
        "duration_ns": window_ns,
        "start_offset_ns": start - started,
        "end_offset_ns": end - started,
    }
    finding["coverage"] = {
        "true_ns": true_ns,
        "false_ns": false_ns,
        "unknown_ns": unknown_ns,
        "known_fraction": (true_ns + false_ns) / window_ns,
    }
    finding["observations"] = {
        "attempts": len(overlapping),
        "valid": sum(1 for index in overlapping if attempts[index].valid),
        "invalid": _listed(invalid),
        "supporting": (
            {
                "first_sequence": min(attempts[i].first_sequence for i in overlapping),
                "last_sequence": max(attempts[i].last_sequence for i in overlapping),
            }
            if overlapping
            else None
        ),
    }

    if check.kind == "holds_throughout":
        finding["violations"] = _listed(violations)
        if violations:
            result = "FAIL"
            reasons = set()
        elif false_ns == 0 and unknown_ns == 0 and true_ns == window_ns:
            result = "PASS"
            reasons = set()
        else:
            result = "INCONCLUSIVE"
            if false_ns > 0:
                reasons.add("held_violation")
    else:
        lower = true_ns / window_ns
        upper = (window_ns - false_ns) / window_ns
        known = true_ns + false_ns
        finding["measurement"] = {
            "in_band_ns": true_ns,
            "out_of_band_ns": false_ns,
            "unknown_ns": unknown_ns,
            "fraction_in_band": {"lower": lower, "upper": upper},
            "fraction_of_known_in_band": (true_ns / known) if known else None,
        }
        numerator, denominator = check.min_fraction.as_integer_ratio()
        if true_ns * denominator >= numerator * window_ns:
            result = "PASS"
            reasons = set()
        elif (window_ns - false_ns) * denominator < numerator * window_ns:
            result = "FAIL"
            reasons = set()
        else:
            result = "INCONCLUSIVE"

    finding["result"] = result
    finding["reasons"] = sorted(reasons)
    return finding
