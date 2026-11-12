"""Offline, evidence-preserving ADC snapshot stitching. No I/O or device access."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import csv
import io
import re
from typing import Iterable

HEADERS = ("timestamp_us", "channel", "source_index", "raw", "millivolts",
           "temperature_c", "status", "rref_kohm")
MAX_CSV_BYTES = 128 * 1024
MAX_ROWS = 256
MAX_SAFE_INTEGER = 2**53 - 1
_INT = re.compile(r"-?[0-9]+\Z")
_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z")
_STATUSES = frozenset(("ok", "open", "short", "uninit"))
_CHANNELS = frozenset(("ptc", "chamber"))


class AdcFormatError(ValueError):
    """Malformed or unsupported saved ADC response."""


@dataclass(frozen=True)
class Exchange:
    """Unmodified saved HTTP exchange, with externally supplied source identity.

    device_id is an operator/capture-system attribution, not device proof.
    headers retains duplicates and order; body retains original bytes.
    Unauthenticated or unverified identity must be represented by None.
    """

    device_id: str | None
    status_code: int | None
    headers: tuple[tuple[str, str], ...]
    body: bytes
    error: str | None = None


@dataclass(frozen=True)
class Sample:
    exchange_index: int
    row: int
    timestamp_us: int
    channel: str
    source_index: int
    raw: int
    millivolts: int
    temperature_c: Decimal | None
    status: str
    rref_kohm: Decimal
    original_row: bytes

    @property
    def identity(self) -> tuple[int, str]:
        return self.timestamp_us, self.channel


@dataclass(frozen=True)
class Boundary:
    """Relationship of an exchange to preceding accepted observations."""

    exchange_index: int
    state: str  # start, proven_continuous, continuity_unknown, contradiction, reboot
    detail: str


@dataclass(frozen=True)
class Timeline:
    """Derived view: source exchanges are never replaced or rewritten."""

    samples: tuple[Sample, ...]
    boundaries: tuple[Boundary, ...]
    rejected: tuple[tuple[int, str], ...]


def _integer(value: str, name: str, row: int, minimum: int, maximum: int | None = None) -> int:
    if not _INT.fullmatch(value):
        raise AdcFormatError(f"row {row}: invalid {name}")
    number = int(value)
    if number < minimum or (maximum is not None and number > maximum):
        raise AdcFormatError(f"row {row}: invalid {name}")
    return number


def _decimal(value: str, name: str, row: int, *, positive: bool = False) -> Decimal:
    if not _DECIMAL.fullmatch(value):
        raise AdcFormatError(f"row {row}: invalid {name}")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise AdcFormatError(f"row {row}: invalid {name}") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise AdcFormatError(f"row {row}: invalid {name}")
    try:
        value_as_float = float(number)
    except OverflowError as exc:
        raise AdcFormatError(f"row {row}: invalid {name}") from exc
    if not -float("inf") < value_as_float < float("inf"):
        raise AdcFormatError(f"row {row}: invalid {name}")
    return number


def parse_snapshot(body: bytes, exchange_index: int = 0) -> tuple[Sample, ...]:
    """Parse one bounded ADC CSV body without normalizing its original rows."""
    if not isinstance(body, bytes):
        raise TypeError("snapshot body must be bytes")
    if len(body) > MAX_CSV_BYTES:
        raise AdcFormatError("CSV exceeds 128 KiB")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError as exc:
        raise AdcFormatError("CSV is not UTF-8") from exc
    # The provisional firmware emits one physical line per sample. A quoted
    # field may not span physical lines; retain exact row bytes for comparisons.
    raw_lines = body.splitlines(keepends=True)
    while raw_lines and raw_lines[-1].strip(b"\r\n") == b"":
        raw_lines.pop()
    if not raw_lines:
        raise AdcFormatError("CSV header required")
    if len(raw_lines) - 1 > MAX_ROWS:
        raise AdcFormatError("CSV exceeds 256 rows")
    lines = text.splitlines()
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) != len(raw_lines):
        raise AdcFormatError("unsupported CSV line ending")

    def fields(line: str, row: int) -> list[str]:
        try:
            parsed = list(csv.reader(io.StringIO(line), strict=True))
        except csv.Error as exc:
            raise AdcFormatError(f"row {row}: invalid CSV quoting") from exc
        if len(parsed) != 1:
            raise AdcFormatError(f"row {row}: malformed CSV")
        return parsed[0]

    if tuple(fields(lines[0], 1)) != HEADERS:
        raise AdcFormatError("unsupported CSV header")
    samples = []
    previous_time = -1
    for row, (line, original) in enumerate(zip(lines[1:], raw_lines[1:]), start=2):
        parts = fields(line, row)
        if len(parts) != 8:
            raise AdcFormatError(f"row {row}: expected eight fields")
        stamp, channel, index, raw, mv, temp, status, rref = parts
        timestamp_us = _integer(stamp, "timestamp_us", row, 0, MAX_SAFE_INTEGER)
        if timestamp_us < previous_time:
            raise AdcFormatError(f"row {row}: nonmonotonic timestamps")
        previous_time = timestamp_us
        if channel not in _CHANNELS or status not in _STATUSES:
            raise AdcFormatError(f"row {row}: unsupported channel/status")
        source_index = _integer(index, "source_index", row, 0, 1)
        raw_value = _integer(raw, "raw", row, -1, 4095)
        millivolts = _integer(mv, "millivolts", row, -1)
        temperature = None if temp == "" else _decimal(temp, "temperature_c", row)
        rref_value = _decimal(rref, "rref_kohm", row, positive=True)
        if status != "ok" and temperature is not None:
            raise AdcFormatError(f"row {row}: non-OK temperature must be blank")
        if status == "ok" and (raw_value < 0 or millivolts < 0):
            raise AdcFormatError(f"row {row}: incomplete OK sample")
        samples.append(Sample(exchange_index, row, timestamp_us, channel,
                              source_index, raw_value, millivolts, temperature,
                              status, rref_value, original))
    return tuple(samples)


def _unique_header(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    values = [v for k, v in headers if k.lower() == name.lower()]
    if len(values) != 1 or not values[0].strip():
        return None
    return values[0].strip()


def stitch(exchanges: Iterable[Exchange]) -> Timeline:
    """Stitch saved exchanges; never infer continuity from nominal cadence.

    Only byte-identical overlap for the same device and boot proves a boundary
    continuous. Repeated identities with different bytes remain visible.
    Missing boot identity, transport failures and header-only snapshots do not
    establish continuity. Input order is the caller's observed exchange order.
    """
    output: list[Sample] = []
    boundaries: list[Boundary] = []
    rejected: list[tuple[int, str]] = []
    seen: dict[tuple[int, str], Sample] = {}
    prior: tuple[Sample, ...] = ()
    identity: tuple[str, str] | None = None
    uncertain = False
    for number, exchange in enumerate(exchanges):
        if not isinstance(exchange, Exchange):
            raise TypeError("expected Exchange instances")
        boot = _unique_header(exchange.headers, "X-DragonBreath-Boot-ID")
        current_identity = (exchange.device_id, boot) if exchange.device_id and boot else None
        if exchange.error is not None or exchange.status_code != 200 or current_identity is None:
            reason = "transport/status failure" if exchange.error or exchange.status_code != 200 else "unverified device/boot identity"
            rejected.append((number, reason))
            boundaries.append(Boundary(number, "continuity_unknown", reason))
            uncertain = True
            prior = ()
            seen = {}
            identity = None
            continue
        try:
            samples = parse_snapshot(exchange.body, number)
        except AdcFormatError as exc:
            rejected.append((number, str(exc)))
            boundaries.append(Boundary(number, "continuity_unknown", str(exc)))
            prior = ()
            seen = {}
            identity = None
            uncertain = True
            continue
        if identity is not None and identity != current_identity:
            boundaries.append(Boundary(number, "reboot" if identity[0] == current_identity[0] else "continuity_unknown",
                                       "boot identity changed" if identity[0] == current_identity[0] else "device identity changed"))
            seen = {}
            prior = ()
            uncertain = False
        elif identity is None:
            boundaries.append(Boundary(number, "continuity_unknown" if uncertain else "start",
                                       "no verified overlap with preceding snapshot" if uncertain else "first verified snapshot"))
        else:
            prior_keys = {s.identity: s for s in prior}
            overlap = [s for s in samples if s.identity in prior_keys]
            contradictory = any(s.original_row != prior_keys[s.identity].original_row for s in overlap)
            # The previous newest sample must survive the next window.
            continuous = bool(prior and samples and prior[-1].identity in {s.identity for s in samples}
                              and overlap and not contradictory)
            state = "contradiction" if contradictory else "proven_continuous" if continuous else "continuity_unknown"
            boundaries.append(Boundary(number, state, "conflicting overlap" if contradictory else
                                       "byte-identical overlap includes prior tail" if continuous else "no proven overlap"))
        identity = current_identity
        local_keys: dict[tuple[int, str], Sample] = {}
        contradiction = False
        for sample in samples:
            earlier = local_keys.get(sample.identity) or seen.get(sample.identity)
            if earlier is not None:
                if earlier.original_row != sample.original_row:
                    contradiction = True
                    output.append(sample)
            else:
                output.append(sample)
            local_keys.setdefault(sample.identity, sample)
            seen.setdefault(sample.identity, sample)
        if contradiction and boundaries[-1].state != "contradiction":
            boundaries[-1] = Boundary(number, "contradiction", "same sample identity has conflicting bytes")
        prior = samples
        uncertain = False
    return Timeline(tuple(output), tuple(boundaries), tuple(rejected))
