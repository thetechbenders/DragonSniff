"""Parse assessment profiles against a closed schema.

Any key the schema does not name is rejected, so a profile cannot express
actions, hooks, or anything other than signals, checks, and assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import tomllib
from typing import Any

PROFILE_FORMAT = 1
MAX_HOLD_S = 3600.0
ID_PATTERN = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "between"})
SOURCES = frozenset({"dragon", "prusalink"})
CHECK_KINDS = frozenset({"holds_throughout", "time_in_band"})


class ProfileError(ValueError):
    """The profile is malformed or asks for something Assess does not do."""


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    source: str
    pointer: str
    endpoint: str | None
    reference_pointer: str | None
    source_id: str | None

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "endpoint": self.endpoint,
            "pointer": self.pointer,
            "reference_pointer": self.reference_pointer,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class Window:
    start_offset_ns: int
    end_offset_ns: int | None


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    kind: str
    signal: str
    window: Window
    predicate: dict[str, Any] | None
    band: tuple[int | float, int | float] | None
    min_fraction: int | float | None


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    version: str
    sha256: str
    byte_length: int
    max_hold_ns: int
    max_hold_s: int | float
    signals: dict[str, Signal]
    checks: tuple[Check, ...]


def _keys(where: str, table: object, required: set[str], optional: set[str]) -> dict:
    if not isinstance(table, dict):
        raise ProfileError(f"{where} must be a table")
    unknown = set(table) - required - optional
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) {sorted(unknown)}")
    missing = required - set(table)
    if missing:
        raise ProfileError(f"{where}: missing key(s) {sorted(missing)}")
    return table


def _number(where: str, value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ProfileError(f"{where} must be finite")
    return value


def _string(where: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{where} must be a non-empty string")
    return value


def _pointer(where: str, value: object) -> str:
    text = _string(where, value)
    if not text.startswith("/"):
        raise ProfileError(f"{where} must be a JSON Pointer starting with '/'")
    return text


def _seconds_to_ns(where: str, value: object, *, ceiling: bool = False) -> int:
    seconds = _number(where, value)
    if seconds < 0:
        raise ProfileError(f"{where} must not be negative")
    # Preserve integers and avoid an intermediate rounded/overflowing float
    # multiplication. Float seconds mean the exact binary value TOML parsed.
    numerator, denominator = seconds.as_integer_ratio()
    quotient, remainder = divmod(numerator * 1_000_000_000, denominator)
    return quotient + int(ceiling and remainder != 0)


def _signal(signal_id: str, table: object) -> Signal:
    where = f"signals.{signal_id}"
    if not ID_PATTERN.match(signal_id):
        raise ProfileError(f"{where}: invalid signal id")
    fields = _keys(
        where,
        table,
        {"source", "pointer"},
        {"endpoint", "reference_pointer", "source_id"},
    )
    source = fields["source"]
    if source not in SOURCES:
        raise ProfileError(f"{where}.source must be one of {sorted(SOURCES)}")
    if source == "dragon":
        if "source_id" in fields:
            raise ProfileError(f"{where}: source_id applies only to prusalink")
        endpoint = _pointer(f"{where}.endpoint", fields.get("endpoint"))
    else:
        if "endpoint" in fields:
            raise ProfileError(f"{where}: endpoint applies only to dragon")
        endpoint = None
    reference = fields.get("reference_pointer")
    return Signal(
        id=signal_id,
        source=source,
        pointer=_pointer(f"{where}.pointer", fields["pointer"]),
        endpoint=endpoint,
        reference_pointer=(
            None
            if reference is None
            else _pointer(f"{where}.reference_pointer", reference)
        ),
        source_id=(
            None
            if "source_id" not in fields
            else _string(f"{where}.source_id", fields["source_id"])
        ),
    )


def _window(where: str, table: object) -> Window:
    fields = _keys(where, table, set(), {"start_offset_s", "end_offset_s"})
    raw_start = _number(f"{where}.start_offset_s", fields.get("start_offset_s", 0))
    start = _seconds_to_ns(f"{where}.start_offset_s", raw_start)
    end = None
    if "end_offset_s" in fields:
        raw_end = _number(f"{where}.end_offset_s", fields["end_offset_s"])
        if raw_end <= raw_start:
            raise ProfileError(f"{where}: end_offset_s must exceed start_offset_s")
        end = _seconds_to_ns(f"{where}.end_offset_s", raw_end, ceiling=True)
    return Window(start, end)


def _predicate(where: str, table: object) -> dict[str, Any]:
    if not isinstance(table, dict) or "op" not in table:
        raise ProfileError(f"{where} must be a table with an op")
    op = table["op"]
    if op not in OPERATORS:
        raise ProfileError(f"{where}.op must be one of {sorted(OPERATORS)}")
    if op == "between":
        fields = _keys(where, table, {"op", "low", "high"}, set())
        low = _number(f"{where}.low", fields["low"])
        high = _number(f"{where}.high", fields["high"])
        if low > high:
            raise ProfileError(f"{where}: low must not exceed high")
        return {"op": op, "low": low, "high": high}
    fields = _keys(where, table, {"op", "value"}, set())
    value = fields["value"]
    if op in {"eq", "ne"}:
        if isinstance(value, bool) or isinstance(value, str):
            return {"op": op, "value": value}
        return {"op": op, "value": _number(f"{where}.value", value)}
    return {"op": op, "value": _number(f"{where}.value", value)}


def _check(index: int, table: object, signals: dict[str, Signal]) -> Check:
    where = f"check[{index}]"
    if not isinstance(table, dict):
        raise ProfileError(f"{where} must be a table")
    kind = table.get("kind")
    if kind not in CHECK_KINDS:
        raise ProfileError(f"{where}.kind must be one of {sorted(CHECK_KINDS)}")
    if kind == "holds_throughout":
        fields = _keys(where, table, {"id", "kind", "signal", "predicate"}, {"window"})
    else:
        fields = _keys(
            where, table, {"id", "kind", "signal", "band", "min_fraction"}, {"window"}
        )
    check_id = fields["id"]
    if not isinstance(check_id, str) or not ID_PATTERN.match(check_id):
        raise ProfileError(f"{where}: invalid check id")
    if fields["signal"] not in signals:
        raise ProfileError(f"{where}: unknown signal {fields['signal']!r}")
    window = _window(f"{where}.window", fields.get("window", {}))
    if kind == "holds_throughout":
        return Check(
            check_id,
            kind,
            fields["signal"],
            window,
            _predicate(f"{where}.predicate", fields["predicate"]),
            None,
            None,
        )
    band = _keys(f"{where}.band", fields["band"], {"low", "high"}, set())
    low = _number(f"{where}.band.low", band["low"])
    high = _number(f"{where}.band.high", band["high"])
    if low > high:
        raise ProfileError(f"{where}.band: low must not exceed high")
    minimum = _number(f"{where}.min_fraction", fields["min_fraction"])
    if not 0.0 < minimum <= 1.0:
        # Zero would pass with no evidence at all.
        raise ProfileError(f"{where}.min_fraction must be within (0, 1]")
    return Check(check_id, kind, fields["signal"], window, None, (low, high), minimum)


def load_profile(data: bytes) -> Profile:
    """Parse and validate profile bytes. Raises ProfileError."""
    if not isinstance(data, bytes):
        raise TypeError("profile must be bytes")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError("profile is not valid UTF-8 TOML") from exc
    fields = _keys(
        "profile",
        document,
        {"profile_format", "name", "version", "signals", "check"},
        {"assumptions"},
    )
    if fields["profile_format"] != PROFILE_FORMAT or isinstance(
        fields["profile_format"], bool
    ):
        raise ProfileError(f"profile_format must be {PROFILE_FORMAT}")
    assumptions = _keys("assumptions", fields.get("assumptions", {}), set(), {"max_hold_s"})
    max_hold_s = _number("assumptions.max_hold_s", assumptions.get("max_hold_s", 0))
    if not 0.0 <= max_hold_s <= MAX_HOLD_S:
        raise ProfileError(f"assumptions.max_hold_s must be within [0, {MAX_HOLD_S}]")
    if not isinstance(fields["signals"], dict) or not fields["signals"]:
        raise ProfileError("signals must declare at least one signal")
    signals = {sid: _signal(sid, table) for sid, table in fields["signals"].items()}
    raw_checks = fields["check"]
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ProfileError("profile must declare at least one [[check]]")
    checks = tuple(_check(i, table, signals) for i, table in enumerate(raw_checks))
    ids = [check.id for check in checks]
    if len(set(ids)) != len(ids):
        raise ProfileError("check ids must be unique")
    return Profile(
        name=_string("name", fields["name"]),
        version=_string("version", fields["version"]),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_length=len(data),
        max_hold_ns=_seconds_to_ns("assumptions.max_hold_s", max_hold_s),
        max_hold_s=max_hold_s,
        signals=signals,
        checks=checks,
    )
