"""Load DragonSniff evidence bytes into validated, ordered records."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

TERMINAL_KINDS = frozenset(
    {"capture_run_completed", "capture_run_cancelled", "capture_run_failed"}
)
TERMINAL_SESSION_STATUSES = frozenset(
    {"completed", "cancelled", "failed", "interrupted"}
)


class EvidenceError(ValueError):
    """Evidence is corrupt or unusable; no assessment may be produced."""


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str | None
    started: dict[str, Any] | None
    terminal: dict[str, Any] | None
    problem: str | None  # run_missing | run_ambiguous | None


@dataclass(frozen=True, slots=True)
class Evidence:
    sha256: str
    byte_length: int
    records: tuple[dict[str, Any], ...]
    gaps: tuple[tuple[int, int], ...]  # inclusive missing sequence ranges
    monotonic_inversions: int
    run: Run
    session: dict[str, Any] | None

    def missing_between(self, low: int, high: int) -> bool:
        """Return whether any sequence in [low, high] is missing."""
        return any(start <= high and end >= low for start, end in self.gaps)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_line(number: int, line: bytes) -> dict[str, Any]:
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError(f"line {number}: not valid UTF-8 JSON") from exc
    if not isinstance(record, dict):
        raise EvidenceError(f"line {number}: record is not a JSON object")
    sequence = record.get("sequence")
    if not _is_int(sequence) or sequence < 1:
        raise EvidenceError(f"line {number}: invalid sequence")
    monotonic = record.get("monotonic_ns")
    if not _is_int(monotonic) or monotonic < 0:
        raise EvidenceError(f"line {number}: invalid monotonic_ns")
    kind = record.get("kind")
    if not isinstance(kind, str) or not kind:
        raise EvidenceError(f"line {number}: invalid kind")
    if not isinstance(record.get("timestamp"), str):
        raise EvidenceError(f"line {number}: invalid timestamp")
    return record


def _session(metadata: bytes | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    try:
        value = json.loads(metadata.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("session metadata is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError("session metadata is not a JSON object")
    status = value.get("status")
    if status not in TERMINAL_SESSION_STATUSES:
        raise EvidenceError(
            f"session status {status!r} is not terminal; only finished "
            "sessions can be assessed"
        )
    return {
        key: value.get(key)
        for key in ("session_id", "kind", "status", "format_version", "records")
    }


def _run(records: tuple[dict[str, Any], ...]) -> Run:
    starts = [r for r in records if r["kind"] == "capture_run_started"]
    if not starts:
        return Run(None, None, None, "run_missing")
    if len(starts) > 1:
        return Run(None, None, None, "run_ambiguous")
    started = starts[0]
    run_id = started.get("run_id") if isinstance(started.get("run_id"), str) else None
    terminals = [
        r
        for r in records
        if r["kind"] in TERMINAL_KINDS and r["sequence"] > started["sequence"]
    ]
    if len(terminals) > 1:
        return Run(run_id, None, None, "run_ambiguous")
    return Run(run_id, started, terminals[0] if terminals else None, None)


def load_evidence(data: bytes, metadata: bytes | None = None) -> Evidence:
    """Validate evidence bytes. Raises EvidenceError on corruption."""
    if not isinstance(data, bytes):
        raise TypeError("evidence must be bytes")
    if not data:
        raise EvidenceError("evidence is empty")
    if not data.endswith(b"\n"):
        raise EvidenceError("evidence ends with an incomplete record")
    lines = data[:-1].split(b"\n")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            raise EvidenceError(f"line {number}: blank line")
        record = _parse_line(number, line)
        if records and record["sequence"] <= records[-1]["sequence"]:
            if record["sequence"] == records[-1]["sequence"]:
                raise EvidenceError(f"line {number}: duplicate sequence")
            raise EvidenceError(f"line {number}: sequence out of order")
        records.append(record)

    gaps: list[tuple[int, int]] = []
    if records[0]["sequence"] > 1:
        gaps.append((1, records[0]["sequence"] - 1))
    inversions = 0
    for previous, current in zip(records, records[1:]):
        if current["sequence"] > previous["sequence"] + 1:
            gaps.append((previous["sequence"] + 1, current["sequence"] - 1))
        if current["monotonic_ns"] < previous["monotonic_ns"]:
            inversions += 1

    frozen = tuple(records)
    return Evidence(
        sha256=hashlib.sha256(data).hexdigest(),
        byte_length=len(data),
        records=frozen,
        gaps=tuple(gaps),
        monotonic_inversions=inversions,
        run=_run(frozen),
        session=_session(metadata),
    )
