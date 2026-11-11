# Sensor Lab: offline ADC forensics (M1)

Sensor Lab is a local-only, read-only workspace at `/sensor-lab` in the existing DragonSniff service. It works without an attached DragonBreath device. Select a CSV explicitly; the browser parses the file in memory and does **not** submit its bytes to the DragonSniff server, use persistent storage, or change JSONL session evidence. Refreshing the page clears imported data.

## Supported CSV contract

Based on the **unreleased** DragonBreath diagnostic branch `danielbrownjr/DragonBreath:feat/ptc-adc-diagnostic-capture-122`, `docs/ADC_CAPTURE.md` (2026-11-11). Revalidate before any integration or release.

Exact ordered header:

```csv
timestamp_us,channel,source_index,raw,millivolts,temperature_c,status,rref_kohm
```

- UTF-8, optional BOM and CRLF; no other header columns.
- Maximum **128 KiB** and **256 data rows** per file. Header-only captures are accepted. Empty or malformed files are rejected.
- `timestamp_us`: nonnegative, safe-integer monotonic microseconds since **device boot** (ties allowed); never converted to wall time.
- `channel`: logical `chamber` or `ptc`. `source_index`: physical ADC index 0 or 1. Both preserved separately.
- `raw`: integer 0–4095, or -1 for unavailable acquisition; `millivolts`: nonnegative integer, or -1 unavailable.
- `temperature_c`: finite decimal for `ok`, blank for non-OK. No substitute temperatures.
- `status`: `ok`, `open`, `short`, `uninit`. `ok` requires all three measurement values; other states require a blank temperature.
- `rref_kohm`: finite positive numeric reference-resistor value.
- Records remain in original CSV order with source row numbers. Out-of-order timestamps and unknown state/channel values are rejected, not sorted away.

The device currently captures at roughly 2 Hz **per channel**, maintaining at most 256 *total* samples across both channels. Captures are volatile, oldest-first snapshots. A header-only CSV is expected on some HIL builds. Sensor Lab does not infer unseen samples before buffer rollover. Its import cannot establish a device's boot identity, capture date, wall-clock sync, or provenance beyond the selected file.

## Investigation

Use the quantity switch to plot raw counts, calibrated mV, or reported instantaneous °C; PTC/chamber visibility controls; scroll wheel / buttons for zoom, pan buttons; click or keyboard-select a point; drag the graph to select an interval. The detail panel displays the **original imported** sample and status. Invalid observations appear as crosses and break connected traces. A/B bookmarks compute signed deltas, elapsed time, and an **apparent reported temperature rate**, not physical element heating/cooling. Ties yield an unavailable rate.

Statistics calculate over retained measurements only, for one channel and interval: counts, invalid count, duration, min/max, population mean and σ, peak-to-peak, median adjacent cadence, largest adjacent valid-value change, tied timestamps, and irregular intervals longer than 1.5× the positive median. Zero/one observations yield unavailable statistics as appropriate. This irregularity heuristic cannot prove missing records. Data are not smoothed, resampled, or interpolated.

Export creates a separately downloaded JSON **derived-analysis report** with source filename/size, selected rows, window, statistics, and comparison. It is not a replacement for the original CSV and does not certify evidence authenticity.

## Read-only and limitations

No device API calls, control commands, firmware configuration, background upload, or new JSONL record kinds were added. The server only serves fixed static files via its existing allowlist. The browser core contains no network code. The original evidence file remains on the operator's computer. Source identity, physical input index, logical channel, status and monotonic timestamp are never silently converted into generic device records.

This is not independent temperature verification, safety verification, or evidence of a specific electromagnetic coupling mechanism. 2 Hz samples cannot uniquely identify 50/60 Hz interference. For suspicious electrical equipment, do not energize the heater or reconnect mains solely for a capture.

## Future milestones (not implemented)

**Live acquisition:** Explicitly permit exactly `GET /api/v2/adc-capture` after verifying its finalized contract, authenticate `X-DragonBreath-Auth` using a protected backend secret (never browser storage), honor the existing DragonSniff connection budget and Thermal scheduler, and prevent arbitrary HTTP forwarding. Snapshot polling must overlap, key records by verified device + boot + sample identity (timestamp/channel/source_index plus collision analysis), deduplicate without conflating reboots, detect rollover/gaps, retain host request begin/end intervals separately from device monotonic time, and append immutable, provenance-rich evidence through a separately versioned storage design. Never assume clocks sync; report freshness and unavailable sources honestly. Security and schedule review are mandatory before implementing.

**Advanced analysis:** Autocorrelation, period hypotheses and alias exploration (with explicit 2 Hz limitation); cross-run comparisons; synchronized fault and safety-constraint replay; external thermocouple and oscilloscope imports; product-supplied diagnostic assessment profiles; reproducible evidence packages. These should be isolated derived views, not mutations of authoritative evidence.

## Validation

`node --test tests/sensor-lab.test.cjs` covers parser boundaries, empty/invalid states, timestamps/ties, rollover examples, statistics, comparison, plot preparation, and retained-record integrity. Existing tests: `python -m unittest discover -s tests -v` and `node --test tests/payload.test.cjs`. Syntax checks: `node --check` for all three JS sources. CI runs these alongside existing container and Python checks. Browser pointer/keyboard interactions and physical hardware measurements require independent manual acceptance and are **not** established by unit tests.
