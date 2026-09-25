"""Build real-shaped DragonSniff evidence for Assess tests.

Records mirror what SessionRecorder, DragonClient, CaptureRunner, and
PrusaLinkSource write: sequence, UTC timestamp, host monotonic_ns, kind, and
the same request/response pairing fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Iterable

S = 1_000_000_000  # nanoseconds per second
RUN_ID = "0123456789abcdef0123456789abcdef"
EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)
STATE = "/api/v2/state"


class EvidenceBuilder:
    def __init__(self, run_id: str = RUN_ID) -> None:
        self.run_id = run_id
        self.records: list[dict[str, Any]] = []
        self._request_id = 0
        self._fetch = 0

    def _append(self, kind: str, at_ns: int, **fields: Any) -> dict[str, Any]:
        record = {
            "sequence": len(self.records) + 1,
            "timestamp": (EPOCH + timedelta(microseconds=at_ns // 1000)).isoformat(
                timespec="milliseconds"
            ),
            "monotonic_ns": at_ns,
            "kind": kind,
            **fields,
        }
        self.records.append(record)
        return record

    def start(self, at_ns: int = 0) -> "EvidenceBuilder":
        self._append(
            "capture_run_started", at_ns, run_id=self.run_id, profile="Custom"
        )
        return self

    def complete(self, at_ns: int, kind: str = "capture_run_completed") -> "EvidenceBuilder":
        self._append(kind, at_ns, run_id=self.run_id)
        return self

    def dragon(
        self,
        parsed: Any,
        start_ns: int,
        end_ns: int,
        *,
        endpoint: str = STATE,
        ok: bool = True,
        failed: bool = False,
        parse_error: str | None = None,
    ) -> "EvidenceBuilder":
        self._request_id += 1
        self._fetch += 1
        context = {
            "run_id": self.run_id,
            "owner": "capture",
            "fetch_sequence": self._fetch,
            "sample_point": "state",
        }
        self._append(
            "http_request",
            start_ns,
            **context,
            request_id=self._request_id,
            method="GET",
            endpoint=endpoint,
        )
        if failed:
            self._append(
                "http_error",
                end_ns,
                **context,
                request_id=self._request_id,
                endpoint=endpoint,
                status=None,
                ok=False,
                parsed=None,
            )
        else:
            self._append(
                "http_response",
                end_ns,
                **context,
                request_id=self._request_id,
                endpoint=endpoint,
                status=200,
                ok=ok,
                parsed=parsed,
                parse_error=parse_error,
                decode_error=None,
            )
        return self

    def value(self, value: Any, start_s: float, end_s: float | None = None, **kw: Any) -> "EvidenceBuilder":
        """Dragon state sample with value at /sensors/chamber/temperature_c."""
        end_s = start_s + 0.05 if end_s is None else end_s
        parsed = {"sensors": {"chamber": {"temperature_c": value}}}
        return self.dragon(parsed, round(start_s * S), round(end_s * S), **kw)

    def prusalink(
        self,
        data: Any,
        start_ns: int,
        end_ns: int,
        *,
        state: str = "healthy",
        observed: Any = None,
    ) -> "EvidenceBuilder":
        self._append(
            "source_observation",
            end_ns,
            run_id=self.run_id,
            source="prusalink",
            source_id="printer",
            endpoint="/api/v1/status",
            method="GET",
            observed_at="2026-09-01T00:00:00.000+00:00",
            observed_monotonic_ns=start_ns if observed is None else observed,
            freshness={"state": "fresh", "sample_age_ms": 0},
            source_state=state,
            data=data,
        )
        return self

    def other(self, kind: str, at_ns: int, **fields: Any) -> "EvidenceBuilder":
        self._append(kind, at_ns, **fields)
        return self

    def to_bytes(self, drop: Iterable[int] = ()) -> bytes:
        dropped = set(drop)
        return "".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
            for r in self.records
            if r["sequence"] not in dropped
        ).encode("utf-8")


def steady(values: list[Any], cadence_s: float = 1.0, duration_s: float | None = None) -> EvidenceBuilder:
    """Samples at a fixed cadence starting at t=0.1 s, completed after the last."""
    builder = EvidenceBuilder().start(0)
    for index, value in enumerate(values):
        builder.value(value, 0.1 + index * cadence_s)
    end = duration_s if duration_s is not None else 0.1 + len(values) * cadence_s
    return builder.complete(round(end * S))


def profile(
    checks: str,
    *,
    max_hold_s: float | None = None,
    signals: str | None = None,
) -> bytes:
    assumptions = "" if max_hold_s is None else f"[assumptions]\nmax_hold_s = {max_hold_s}\n\n"
    signals = signals or (
        "[signals.chamber]\n"
        'source = "dragon"\n'
        'endpoint = "/api/v2/state"\n'
        'pointer = "/sensors/chamber/temperature_c"\n'
    )
    return (
        'profile_format = 1\nname = "fixture"\nversion = "1"\n\n'
        + assumptions
        + signals
        + "\n"
        + checks
    ).encode("utf-8")


HOLDS_LE_70 = (
    "[[check]]\n"
    'id = "below_limit"\n'
    'kind = "holds_throughout"\n'
    'signal = "chamber"\n'
    'predicate = { op = "le", value = 70.0 }\n'
)


def in_band(min_fraction: float = 0.5, low: float = 64.0, high: float = 66.0, window: str = "") -> str:
    return (
        "[[check]]\n"
        'id = "in_band"\n'
        'kind = "time_in_band"\n'
        'signal = "chamber"\n'
        f"band = {{ low = {low}, high = {high} }}\n"
        f"min_fraction = {min_fraction}\n"
        + (f"window = {window}\n" if window else "")
    )
