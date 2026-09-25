# Assess: offline evidence assessment

Assess evaluates captured DragonSniff evidence against acceptance criteria that
someone else supplies. It reads a completed evidence file and an assessment
profile, and produces a derived report of **findings**.

This document is the contract. Where the implementation and this document
disagree, the implementation is wrong.

## Authority boundary

- **DragonSniff evaluates externally supplied criteria. It never defines
  product policy.** An assessment profile states what a product is expected to
  do. Profiles are owned by the product being assessed and live in that
  product's repository. The Assess engine is generic and contains no product
  policy.
- **PASS means only that the listed criteria were satisfied by sufficient
  captured evidence under the specified profile.** It says nothing about
  criteria that were not listed, conditions that were not captured, or
  behavior outside the evaluated window.
- **PASS is not a safety certification.** Every report carries this statement:

  > PASS means the listed assertions were satisfied by sufficient captured
  > evidence under this assessment profile. It is not a safety certification.

- **An assessment is derived output, not authoritative evidence.** The evidence
  file remains the record of what was observed. A report is an evaluation of
  that record and can always be regenerated from it.
- **Assessment never mutates evidence.** The engine receives evidence as bytes
  and has no filesystem, network, or process access. It cannot modify
  `evidence.jsonl`, session metadata, or anything else.
- **DragonSniff remains read-only toward every observed system.** Assessment
  performs no device or printer requests, and no finding triggers any action,
  notification, or I/O.
- **Sub-second safety and interlock validation is outside DragonSniff's
  authority.** Polled evidence cannot resolve it. It belongs in the product's
  own tests and in external instruments.

## Terms

- **Evidence file**: a DragonSniff JSONL file such as a persistent session's
  `evidence.jsonl` or a Thermal capture export.
- **Profile**: a TOML file declaring signals and checks.
- **Check**: one criterion declared in the profile.
- **Finding**: the result of evaluating one check.
- **Assessment**: the report containing every finding.

## Time and ordering

- **`sequence` is arrival order.** It is authoritative for ordering records.
  Every sequence value must be unique and must increase through the file.
  Duplicates or decreases are corruption and the assessment is refused.
- **Host `monotonic_ns` is the only duration arithmetic.** All sources in one
  session share it. Records are timestamped before the recorder assigns their
  sequence, so records from different threads can have `monotonic_ns` values
  slightly out of sequence order. That is expected and is not corruption.
- **Wall-clock `timestamp` is display and provenance data only.** It is never
  used in arithmetic.
- **A missing sequence number is missing evidence.** Gaps are reported and can
  only make findings less certain, never more.

### Completeness gate (semantics version 2)

Assessment requires a closed run and a contiguous sequence prefix from **1
through its terminal record**, even when the profile supplies an explicit end
offset. Any gap in that prefix makes every check INCONCLUSIVE and the entire
window unknown (time-in-band bounds `[0, 1]`). Neither held values nor point
violations from an incomplete prefix decide a finding.

This deliberately sacrifices partial assessments: a missing record could be a
duplicate request/completion or run marker that invalidates apparently good
evidence elsewhere. Checking gaps only between adjacent observations cannot
guarantee deletion monotonicity. A missing terminal makes the window undefined;
it must not let an explicit window assess a possibly truncated run.

## Observations

A polled value was sampled at some unknown instant inside a known interval.
Assess never invents an exact sample time.

- **Dragon HTTP**: an `http_request` and its completion record (`http_response`
  or `http_error`) are joined by `request_id`. The observation interval is
  `[request.monotonic_ns, completion.monotonic_ns]`.
- **PrusaLink**: a `source_observation` with `source = "prusalink"` has the
  interval `[observed_monotonic_ns, monotonic_ns]`.

DragonSniff's own PrusaLink `freshness` describes DragonSniff's poller. It is
not the observed product's freshness decision, and Assess never uses it.

Each observation attempt is either **valid**, carrying a value, or **invalid**,
carrying no value. An attempt is invalid when any of the following apply, and
it is never coerced to false, zero, or any other value:

- a failed request (`http_error`, or `ok` not exactly `true`)
- a parse or decode error, or no parsed object
- a missing field, or a JSON `null`
- a value of the wrong type for the comparison (booleans are not numbers)
- NaN or infinity
- a PrusaLink observation whose `source_state` is not `healthy`
- a malformed record, such as an unpaired request or response, a duplicated
  `request_id`, or a completion earlier than its request

Any non-null `parse_error`, `decode_error`, `parse_error_kind`, or `error` field
invalidates the observation, even with `ok = true` / `source_state = healthy`
and a parsed payload. This applies to both sources, including wrongly typed
error fields and empty strings. A Dragon request ID must be an integer (not a
boolean) or a non-empty string; other IDs produce invalid attempts.

## Truth over time

Each check applies a predicate to a signal. At any instant within the window,
the predicate is **true**, **false**, or **unknown**.

### Strict no-hold (default)

With no hold, a value is known only at an instant somewhere inside its
observation interval. Continuous time between observations is **unknown**.

A continuous claim therefore cannot be proven from polled observations under
strict no-hold. That is deliberate: to claim a condition held *throughout* a
period, the profile must say how long a polled value may be assumed to
persist.

### Bounded sample-and-hold (explicit opt-in)

A profile may declare `max_hold_s = h`. The assumption is: a value observed at
instant `t` persists until the next observation attempt or until `t + h`,
whichever comes first. The report always echoes this assumption.

For consecutive attempts `k` and `k+1` with intervals `[a, b]`:

- **After `b_k`**, the value `v_k` is known on `[b_k, min(a_(k+1), a_k + h)]`,
  because `t_k <= b_k` and `t_k + h >= a_k + h`.
- **During `[a_(k+1), b_(k+1)]`**, the value is either `v_k` or `v_(k+1)`. The
  predicate is true there only if it is true for both, false only if false for
  both, and unknown otherwise. `v_k` counts only up to `a_k + h`.
- **Everything else is unknown.**

A hold is withdrawn entirely, so it contributes nothing, when:

- the attempt is invalid;
- the next attempt overlaps it in time;
- any sequence number is missing between this attempt's request and the next
  attempt's completion, since the missing record may have been an observation;
- it is the last attempt and any sequence number is missing before the run's
  terminal record, or there is no terminal record.

Holds never extend past the run's terminal record.

## Windows

This slice evaluates Thermal capture runs. A window is anchored to the run's
`capture_run_started` record:

- start: `started.monotonic_ns + start_offset_s` (default `0`)
- end: `started.monotonic_ns + end_offset_s`, or the terminal record's
  `monotonic_ns` when `end_offset_s` is omitted

The first `capture_run_started` begins the assessed run; the first terminal
record after it closes the run. A second start before that terminal is
ambiguous. A terminal with a different/missing run ID or a timestamp earlier
than the start is invalid. Missing, ambiguous, invalid, or unterminated runs
have an **undefined** window and every finding is INCONCLUSIVE.

Only observations with the selected run ID, sequence inside its start/terminal
boundaries, and record timestamps inside that time horizon are considered.
Records after the terminal sequence cannot affect findings, including later
run markers, duplicate request IDs, and records with backdated timestamps.
They remain in the evidence identity/hash. The loader still validates JSON and
core record integrity throughout the supplied file. Time after termination is
unknown, including for point-violation detection.

### Numeric precision and nanosecond rounding

TOML integers stay exact integers in predicates, bands, and reports. Finite
TOML floats retain the binary value returned by `tomllib`; the engine does not
claim to recover decimal digits already lost when parsing a float literal.
Use integer literals for exact large counters and integer thresholds.

Seconds convert using the parsed number's exact integer ratio, multiplied by
one billion using integer arithmetic. There is no intermediate floating-point
multiplication. Holds and start offsets round **down** to integer nanoseconds;
explicit end offsets round **up**. Thus a hold is never lengthened and a window
is rounded outward. Original offsets must still be nonnegative and the end
must exceed the start before rounding. A positive hold below one nanosecond
becomes no-hold. The report echoes both `max_hold_s` and effective `max_hold_ns`;
window fields contain the effective integer boundaries.

## Findings

Every finding is **PASS**, **FAIL**, or **INCONCLUSIVE**, with reason codes.

### `holds_throughout`

- **FAIL** when a valid observation whose entire interval lies inside the
  window violates the predicate. FAIL requires a proven violation; it never
  depends on a hold assumption.
- **PASS** only when the predicate is true for the entire window: no unknown
  time and no false time.
- **INCONCLUSIVE** otherwise. "Nothing contradicted this" is never PASS.

### `time_in_band`

Time-weighted, never sample-count-weighted. Within the window the engine
integrates, in integer nanoseconds:

- `in_band_ns`: time the value is known to be inside `[low, high]`
- `out_of_band_ns`: time it is known to be outside
- `unknown_ns`: everything else

It reports the fraction of the window in band as an interval:

- lower bound `in_band_ns / window_ns`: unknown time assumed out of band
- upper bound `(window_ns - out_of_band_ns) / window_ns`: unknown time assumed
  in band

The fraction of *known* time in band is also reported, but it never decides
the result. `min_fraction` must be greater than zero, because a zero criterion
would pass with no evidence at all. Against `min_fraction = p`:

- **PASS** only when the lower bound is at least `p`
- **FAIL** only when the upper bound is below `p`
- **INCONCLUSIVE** otherwise

Durations are integer nanoseconds, so accumulation is exact. Decisions compare
integer cross-products against the exact ratio of the parsed `min_fraction`.
Displayed fractions are computed once from those integers and may be rounded;
a displayed lower bound equal to the displayed threshold does not override
the exact comparison.

### Reason codes

| Code | Meaning |
| --- | --- |
| `run_missing` | No `capture_run_started` record. |
| `run_ambiguous` | Multiple starts before the first terminal, or in an unclosed run. |
| `run_invalid` | The terminal has an inconsistent run ID or time. |
| `run_not_terminated` | The run has no terminal, including with an explicit window. |
| `window_empty` | The window has no duration. |
| `no_hold_declared` | Strict no-hold leaves continuous time unknown. |
| `coverage_incomplete` | Part of the window is unknown. |
| `no_observations` | No observation attempt overlaps the window. |
| `invalid_observations` | An attempt in the window carried no usable value. |
| `sequence_gap` | A sequence is missing from 1 through terminal; all time is unknown. |
| `evidence_ended` | The window extends beyond the available evidence. |
| `violation_at_window_edge` | A violating observation only partly overlaps the window. |
| `held_violation` | False time exists only through a hold assumption, not a proven violation. |

A finding that is PASS or FAIL carries no reason codes: it was decided even
under the worst case. Coverage figures are still reported.

## The epistemic invariant

**Deleting evidence must never move a finding toward PASS.**

Formally, for any subset of records removed from an evidence file:

- if the reduced evidence yields PASS, the full evidence yields PASS;
- deletion never turns PASS into FAIL, because deletion cannot create a proven
  violation;
- `time_in_band` bounds only widen: the lower bound never rises and the upper
  bound never falls.

Deleting the one observation that proves a violation can turn FAIL into
INCONCLUSIVE. That is correct: the proof is gone. It is not a move toward
PASS.

The invariant is tested directly, not just implied by individual rules.

It applies to record deletion with original sequence numbers preserved. Any
deletion before the terminal leaves a prefix gap or removes required run
structure; deleting trailing records leaves findings unchanged. It does not
authenticate evidence against editing, renumbering, or fabricated records.

## Profile format

Profiles are TOML, read with Python's standard `tomllib`. The schema is
closed: an unknown key anywhere is rejected, so action-shaped keys such as
`on_fail`, `action`, `exec`, `webhook`, or `notify` cannot enter a profile.

```toml
profile_format = 1
name = "example-long-haul"
version = "1"

[assumptions]
max_hold_s = 10.0          # omit for strict no-hold

[signals.chamber]
source = "dragon"
endpoint = "/api/v2/state"
pointer = "/sensors/chamber/temperature_c"

[[check]]
id = "chamber_in_band"
kind = "time_in_band"
signal = "chamber"
band = { low = 64.0, high = 66.0 }
min_fraction = 0.5
window = { start_offset_s = 3600.0 }

[[check]]
id = "chamber_below_limit"
kind = "holds_throughout"
signal = "chamber"
predicate = { op = "le", value = 80.0 }
```

- `source` is `dragon` or `prusalink`.
- Dragon signals name an `endpoint`. `pointer` is an RFC 6901 JSON Pointer into
  the parsed response. An optional `reference_pointer` subtracts a second value
  from the same response, such as a target.
- PrusaLink signals point into the observation's `data` object and may name a
  `source_id`.
- Predicate operators are `eq`, `ne`, `lt`, `le`, `gt`, `ge`, and `between`
  (inclusive `low` and `high`).

## Reports

Reports are deterministic JSON. The same evidence bytes, profile bytes, and
DragonSniff version always produce byte-identical output. A report records:

- `assessment_format`, `assess_semantics_version`, and the DragonSniff version
- the fixed PASS statement
- profile name, version, SHA-256, and byte length
- evidence SHA-256, byte length, record count, first and last sequence,
  sequence gaps, run identity, and session identity when supplied
- assumptions, including the hold
- per finding: result, reason codes, window, coverage, criteria, observation
  counts, and the sequence numbers of decisive records

An assessment time appears only if the caller supplies one. It is report
metadata and is never used in evaluation.

`assess_semantics_version` increases whenever any rule in this document
changes meaning, so an old report can always be matched to the semantics that
produced it.

## Not in this slice

Live evaluation, a user interface, server routes, a command-line interface,
events and triggers, latency checks, other measurements, and multi-signal
predicates are not implemented. Adding any of them requires updating this
contract first.
