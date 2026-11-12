"""Offline ADC stitching fixtures and invariants."""
import unittest

from dragonsniff.adc import AdcFormatError, Exchange, parse_snapshot, stitch

HEADER = b"timestamp_us,channel,source_index,raw,millivolts,temperature_c,status,rref_kohm\n"


def row(t, raw=100, status="ok", channel="ptc", temp="30"):
    return f"{t},{channel},{1 if channel == 'ptc' else 0},{raw},900,{temp if status == 'ok' else ''},{status},82\n".encode()


def exchange(rows, boot="boot1", device="device1", status=200, error=None):
    return Exchange(device, status, (("X-DragonBreath-Boot-ID", boot),), HEADER + b"".join(rows), error)


class ParserTests(unittest.TestCase):
    def test_header_only_bom_crlf(self):
        self.assertEqual(parse_snapshot(b"\xef\xbb\xbf" + HEADER.replace(b"\n", b"\r\n")), ())

    def test_exact_measurements(self):
        sample, = parse_snapshot(HEADER + row(1))
        self.assertEqual((sample.timestamp_us, sample.source_index, sample.raw,
                          str(sample.temperature_c)), (1, 1, 100, "30"))
        self.assertEqual(sample.original_row, row(1))

    def test_non_ok_raw_is_not_erased(self):
        sample, = parse_snapshot(HEADER + row(1, 4095, "open"))
        self.assertEqual((sample.raw, sample.temperature_c), (4095, None))

    def test_reject_bad(self):
        for body in [b"", HEADER + row(3) + row(2),
                     HEADER + b"1,ptc,1,4096,900,30,ok,82\n",
                     HEADER + b"1,ptc,1,-1,900,30,ok,82\n",
                     HEADER + row(1, status="open")[:-1] + b"x\n",
                     HEADER + b"1,ptc,1,100,900,30,open,82\n",
                     HEADER + row(1)*257,
                     HEADER + b"1,ptc,1,100,900,NaN,ok,82\n",
                     HEADER + b"1,ptc,1,100,900,30,ok,0\n",
                     HEADER + b"9007199254740992,ptc,1,100,900,30,ok,82\n",
                     HEADER + b"\xff"]:
            with self.subTest(body=body[:80]), self.assertRaises(AdcFormatError):
                parse_snapshot(body)


class StitchTests(unittest.TestCase):
    def test_proven_tail_overlap_and_dedup(self):
        timeline = stitch([exchange([row(1), row(2), row(3)]),
                           exchange([row(3), row(4)])])
        self.assertEqual([sample.timestamp_us for sample in timeline.samples], [1, 2, 3, 4])
        self.assertEqual([b.state for b in timeline.boundaries],
                         ["start", "proven_continuous"])

    def test_disjoint_is_unknown(self):
        timeline = stitch([exchange([row(1)]), exchange([row(4)])])
        self.assertEqual(timeline.boundaries[1].state, "continuity_unknown")

    def test_only_earlier_overlap_not_proven(self):
        timeline = stitch([exchange([row(1), row(2), row(3)]),
                           exchange([row(1), row(4)])])
        self.assertEqual(timeline.boundaries[1].state, "continuity_unknown")

    def test_reboot_separates_identical_samples(self):
        timeline = stitch([exchange([row(1)]), exchange([row(1)], boot="boot2")])
        self.assertEqual(len(timeline.samples), 2)
        self.assertEqual(timeline.boundaries[1].state, "reboot")

    def test_conflicting_repeat_is_preserved(self):
        timeline = stitch([exchange([row(1)]), exchange([row(1, raw=200)])])
        self.assertEqual([sample.raw for sample in timeline.samples], [100, 200])
        self.assertEqual(timeline.boundaries[1].state, "contradiction")

    def test_duplicate_in_snapshot_is_not_silent(self):
        timeline = stitch([exchange([row(1), row(1, raw=201)])])
        self.assertEqual(len(timeline.samples), 2)
        self.assertEqual(timeline.boundaries[0].state, "contradiction")

    def test_header_only_and_failed_request_do_not_invent_samples(self):
        timeline = stitch([exchange([row(1)]), exchange([], status=403),
                           exchange([row(2)])])
        self.assertEqual(len(timeline.samples), 2)
        self.assertEqual(timeline.boundaries[2].state, "continuity_unknown")
        self.assertEqual(timeline.rejected, ((1, "transport/status failure"),))

    def test_missing_identity_fails_closed(self):
        timeline = stitch([exchange([row(1)], boot=""), exchange([row(1)])])
        self.assertEqual(len(timeline.samples), 1)
        self.assertEqual(timeline.boundaries[1].state, "continuity_unknown")

    def test_schema_version_fails_closed(self):
        saved = exchange([row(1)])
        unsupported = Exchange(saved.device_id, saved.status_code, saved.headers,
                               saved.body, schema_version=2)
        timeline = stitch([saved, unsupported, saved])
        self.assertEqual(timeline.rejected, ((1, "unsupported exchange schema version"),))
        self.assertEqual(timeline.boundaries[2].state, "continuity_unknown")

    def test_source_identity_is_carried_in_derived_samples(self):
        timeline = stitch([exchange([row(1)]), exchange([row(1)], boot="boot2")])
        self.assertEqual([(s.device_id, s.boot_id) for s in timeline.samples],
                         [("device1", "boot1"), ("device1", "boot2")])

    def test_input_is_untouched(self):
        saved = exchange([row(1)])
        timeline = stitch([saved, saved])
        self.assertEqual(saved.body, HEADER + row(1))
        self.assertEqual(len(timeline.samples), 1)


if __name__ == "__main__":
    unittest.main()
