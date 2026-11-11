"""Static-only Sensor Lab boundary regression."""
from unittest import TestCase

from dragonsniff.server import STATIC_FILES, LOCAL_POST_PATHS
from tests.test_server import LocalServerFixture


class SensorLabStaticTests(TestCase):
    def test_exact_static_allowlist_only(self):
        self.assertEqual(STATIC_FILES["/sensor-lab"][0], "sensor-lab.html")
        self.assertEqual(STATIC_FILES["/sensor-lab.js"][0], "sensor-lab.js")
        self.assertEqual(STATIC_FILES["/sensor-lab-core.js"][0], "sensor-lab-core.js")
        self.assertFalse(any("adc" in path or "sensor" in path for path in LOCAL_POST_PATHS))
        with LocalServerFixture() as fixture:
            for path in ("/sensor-lab", "/sensor-lab.js", "/sensor-lab-core.js"):
                status, body, _ = fixture.request("GET", path)
                self.assertEqual(status, 200, path)
                self.assertTrue(body, path)
            status, _, _ = fixture.request("GET", "/api/v2/adc-capture")
            self.assertEqual(status, 404)
            status, _, _ = fixture.request("POST", "/sensor-lab")
            self.assertNotEqual(status, 200)
