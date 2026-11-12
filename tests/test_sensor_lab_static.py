"""Static-only Sensor Lab boundary regression."""
from unittest import TestCase
from importlib.resources import files
from html.parser import HTMLParser

from dragonsniff.server import STATIC_FILES, LOCAL_POST_PATHS
from tests.test_server import LocalServerFixture


class SensorLabStaticTests(TestCase):
    def test_exact_static_allowlist_only(self):
        self.assertEqual(STATIC_FILES["/sensor-lab"][0], "sensor-lab.html")
        self.assertEqual(STATIC_FILES["/sensor-lab.js"][0], "sensor-lab.js")
        self.assertEqual(STATIC_FILES["/sensor-lab.css"][0], "sensor-lab.css")
        self.assertEqual(STATIC_FILES["/sensor-lab-core.js"][0], "sensor-lab-core.js")
        self.assertFalse(any("adc" in path or "sensor" in path for path in LOCAL_POST_PATHS))
        with LocalServerFixture() as fixture:
            for path in ("/sensor-lab", "/sensor-lab.css", "/sensor-lab.js", "/sensor-lab-core.js"):
                status, body, _ = fixture.request("GET", path)
                self.assertEqual(status, 200, path)
                self.assertTrue(body, path)
            status, _, _ = fixture.request("GET", "/api/v2/adc-capture")
            self.assertEqual(status, 404)
            status, _, _ = fixture.request("POST", "/sensor-lab")
            self.assertNotEqual(status, 200)

    def test_sensor_lab_html_does_not_require_unsafe_inline_style(self):
        class InlinePolicyInspector(HTMLParser):
            def __init__(self):
                super().__init__()
                self.offenses = []

            def handle_starttag(self, tag, attrs):
                if tag == "style":
                    self.offenses.append("inline style element")
                if tag == "script" and not dict(attrs).get("src"):
                    self.offenses.append("inline script element")
                if any(key == "style" or key.startswith("on") for key, _ in attrs):
                    self.offenses.append(f"inline attribute on {tag}")

        page = files("dragonsniff.web").joinpath("sensor-lab.html").read_text(encoding="utf-8")
        inspector = InlinePolicyInspector()
        inspector.feed(page)
        self.assertEqual(inspector.offenses, [])
        self.assertIn('href="/sensor-lab.css"', page)
        script = files("dragonsniff.web").joinpath("sensor-lab.js").read_text(encoding="utf-8")
        self.assertNotIn('.style.', script)
        self.assertIn('classList.toggle("sensor-message-error"', script)
