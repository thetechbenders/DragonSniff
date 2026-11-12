# Sensor Lab M2a P1: offline ADC exchange stitching

This is a **host-only, pure-Python derived-data library** in dragonsniff.adc.
It does not open sockets, load files, persist sessions, alter the Sensor Lab UI,
or talk to a device. It does not authorize live ADC acquisition.

## Input contract (version 1, provisional)

The caller supplies an **ordered sequence** of immutable Exchange objects.
Each object contains device_id (externally attributed identity), HTTP
status_code, ordered (name, value) response headers (including duplicates),
original response body bytes, and an optional transport error. Keeping these
fields is the proposed **in-memory** contract, not an on-disk format and not
evidence authentication. The caller remains responsible for preserving the
original saved HTTP exchanges.

Only status 200 with exactly one nonblank X-DragonBreath-Boot-ID and a
nonblank externally supplied device identity is admitted for stitching.
The body must conform to the existing M1 ADC CSV fields, in their exact order.
UTF-8/BOM, CRLF, 128 KiB and 256-row limits follow M1. Header-only responses
are valid but contain no samples. The provisional firmware emits one physical
line per sample; multi-line quoted cells are not supported by this parser.

Each parsed sample carries the originating exchange index, source row number,
physical source_index, logical channel, original row bytes and reported
measurements. Temperature is a reported firmware conversion, **not** an
independent thermometer. Non-OK rows can retain raw counts and millivolts.

## Identity and continuity

A row is identified **within a device/boot segment** by
(timestamp_us, channel). The physical ADC source index is preserved and
compared as part of the original row bytes, not used to conceal contradictory
channel remapping. Two rows with the same key but different original bytes are
retained and flagged as a **contradiction**. Identical repeated rows are
deduplicated in the derived view. None of these operations mutate input bytes.

Each exchange receives one boundary:

- start: first accepted verified snapshot, without a claim about earlier history;
- proven_continuous: same source and boot, with byte-identical overlapping
  rows including the previous snapshot's newest row;
- continuity_unknown: missing verified overlap, failed request, missing
  identity, malformed capture, or different source;
- contradiction: same identity but conflicting bytes;
- reboot: verified boot ID changes for the same source.

A nominal 500 ms sampling interval, sequential timestamps, or 200 response
**cannot** establish that no samples were lost. Even a proven overlap only
establishes continuity of the retained ring observations under the provisional
firmware producer contract, not correctness of the ADC or control loop. Header
values and externally supplied identity are not cryptographic attestation.
Loss counts are unavailable without firmware sequence/total-pushed metadata.

The returned Timeline(samples, boundaries, rejected) is derived output.
Failed and malformed exchanges are explicitly identified by index and do not
create fabricated samples. A caller must retain original exchanges elsewhere.
The current implementation assumes input exchanges arrive in the caller's
recorded order; it does not reorder events using untrusted device time.

## Minimal example

~~~python
from dragonsniff.adc import Exchange, stitch

saved_exchanges = [
    Exchange(
        device_id="operator-attributed-controller",
        status_code=200,
        headers=(("X-DragonBreath-Boot-ID", "observed-boot-id"),),
        body=saved_http_body_bytes,
    ),
]
timeline = stitch(saved_exchanges)
~~~

## Validation and limitations

Run PYTHONPATH=src python -W error -m unittest tests.test_adc -v for parser,
overlap, contradiction, reboot, and malformed-response fixtures. The full
repository Python/Node and container CI gates remain applicable.

This library is deliberately independent of M2b persistence design. It has
no clock reconciliation, endpoint authentication, live polling, HTTP transport,
device-control authority, file writing, UI import, Assess integration, hardware
acceptance, or physical fault diagnosis. Do not infer 50/60 Hz mechanisms from
approximately 2 Hz sampled data.
