"""Structural trust-boundary tests for dragonsniff.assess.

These inspect the package source and its runtime behavior directly, so the
boundary cannot erode silently: an import, file write, network call, product
policy string, or action-shaped profile key has to fail a test to get in.
"""

import ast
import builtins
import inspect
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

import dragonsniff.assess as assess_package
from dragonsniff.assess import ProfileError, assess
from dragonsniff.assess.evidence import load_evidence
from dragonsniff.assess.profile import load_profile
from dragonsniff.assess.report import build_report
from tests.assess_fixtures import HOLDS_LE_70, in_band, profile, steady

PACKAGE_DIR = Path(assess_package.__file__).parent
SOURCES = sorted(PACKAGE_DIR.glob("*.py"))

ALLOWED_STDLIB = frozenset(
    {"__future__", "dataclasses", "hashlib", "json", "math", "re", "tomllib", "typing"}
)
ALLOWED_RELATIVE = frozenset(
    {"evidence", "profile", "engine", "report", "_version"}
)
FORBIDDEN_CALLS = frozenset(
    {"open", "exec", "eval", "compile", "__import__", "input", "print", "breakpoint"}
)
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "write", "write_text", "write_bytes", "writelines", "unlink", "rmdir",
        "mkdir", "touch", "chmod", "rename", "truncate", "system", "popen",
        "connect", "send", "sendall", "urlopen", "read_bytes", "read_text",
    }
)
NETWORK_OR_CONTROL_MODULES = (
    "dragonsniff.client", "dragonsniff.observer", "dragonsniff.capture",
    "dragonsniff.churn", "dragonsniff.prusalink", "dragonsniff.server",
    "dragonsniff.storage", "dragonsniff.recording", "dragonsniff.target",
    "socket", "ssl", "http", "urllib", "subprocess", "asyncio", "select",
)
PRODUCT_POLICY_TERMS = (
    "jumpjet", "jump jet", "jump_jet", "dragonbreath", "panda", "heater",
    "chamber", "duty", "interlock", "dc_prusa", "fault_latched", "controller",
    "safety_policy",
)
ACTION_SHAPED_KEYS = (
    "on_fail", "on_pass", "action", "actions", "exec", "command", "webhook",
    "notify", "hook", "set_heater", "callback",
)


class ImportIsolationTests(TestCase):
    def test_sources_import_only_allowlisted_modules(self) -> None:
        self.assertTrue(SOURCES)
        for source in SOURCES:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(file=source.name, module=alias.name):
                            self.assertIn(alias.name.split(".")[0], ALLOWED_STDLIB)
                elif isinstance(node, ast.ImportFrom):
                    with self.subTest(file=source.name, module=node.module, level=node.level):
                        if node.level == 0:
                            self.assertIn((node.module or "").split(".")[0], ALLOWED_STDLIB)
                        else:
                            self.assertIn(node.module, ALLOWED_RELATIVE)
                            if node.module == "_version":
                                self.assertEqual(node.level, 2)
                            else:
                                self.assertEqual(node.level, 1)

    def test_importing_assess_loads_no_network_or_control_module(self) -> None:
        probe = (
            "import json, sys\n"
            "import dragonsniff.assess\n"
            "print(json.dumps(sorted(sys.modules)))\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(PACKAGE_DIR.parent.parent)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        )
        output = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True
        ).stdout
        loaded = set(json.loads(output))
        for module in NETWORK_OR_CONTROL_MODULES:
            with self.subTest(module=module):
                self.assertFalse(
                    any(name == module or name.startswith(module + ".") for name in loaded),
                    f"{module} was loaded by importing dragonsniff.assess",
                )


class NoSideEffectTests(TestCase):
    def test_sources_contain_no_io_or_dynamic_code(self) -> None:
        for source in SOURCES:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    function = node.func
                    if isinstance(function, ast.Name):
                        with self.subTest(file=source.name, call=function.id):
                            self.assertNotIn(function.id, FORBIDDEN_CALLS)
                    elif isinstance(function, ast.Attribute):
                        with self.subTest(file=source.name, call=function.attr):
                            self.assertNotIn(function.attr, FORBIDDEN_ATTRIBUTES)

    def test_assessment_performs_no_io(self) -> None:
        evidence = steady([65.0] * 5).to_bytes()
        profile_bytes = profile(HOLDS_LE_70 + "\n" + in_band(0.5), max_hold_s=1.05)

        def refuse(*args, **kwargs):
            raise AssertionError("assessment attempted I/O")

        with patch.object(socket, "socket", refuse), \
             patch.object(socket, "create_connection", refuse), \
             patch.object(subprocess, "Popen", refuse), \
             patch.object(builtins, "open", refuse), \
             patch.object(os, "open", refuse), \
             patch.object(os, "system", refuse):
            report = json.loads(assess(evidence, profile_bytes))
        self.assertEqual(len(report["findings"]), 2)

    def test_assessment_never_mutates_source_evidence(self) -> None:
        data = steady([65.0, 75.0, 65.0]).to_bytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            path.write_bytes(data)
            path.chmod(stat.S_IRUSR)
            before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
            assess(path.read_bytes(), profile(HOLDS_LE_70, max_hold_s=1.05))
            after = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(before, after)

    def test_evaluation_does_not_mutate_loaded_records(self) -> None:
        data = steady([65.0, float("nan"), 75.0]).to_bytes()
        evidence = load_evidence(data)
        snapshot = json.dumps(evidence.records, sort_keys=True)
        build_report(evidence, load_profile(profile(HOLDS_LE_70 + "\n" + in_band(0.5), max_hold_s=2)))
        self.assertEqual(json.dumps(evidence.records, sort_keys=True), snapshot)

    def test_public_api_takes_data_not_callbacks(self) -> None:
        parameters = inspect.signature(assess).parameters
        self.assertEqual(list(parameters), ["evidence", "profile", "metadata", "assessed_at"])
        self.assertIsInstance(assess(steady([65.0]).to_bytes(), profile(HOLDS_LE_70)), str)
        self.assertEqual(
            sorted(assess_package.__all__),
            ["ASSESSMENT_FORMAT", "ASSESS_SEMANTICS_VERSION", "EvidenceError",
             "ProfileError", "STATEMENT", "assess"],
        )


class GenericEngineTests(TestCase):
    def test_engine_contains_no_product_policy(self) -> None:
        for source in SOURCES:
            text = source.read_text(encoding="utf-8").lower()
            for term in PRODUCT_POLICY_TERMS:
                with self.subTest(file=source.name, term=term):
                    self.assertNotIn(term, text)


class ClosedProfileSchemaTests(TestCase):
    BASE = profile(HOLDS_LE_70).decode()

    def test_action_shaped_keys_are_rejected_everywhere(self) -> None:
        placements = {
            "top level": lambda key: f"{key} = \"x\"\n" + self.BASE,
            "check": lambda key: self.BASE + f"{key} = \"x\"\n",
            "signal": lambda key: self.BASE.replace(
                'pointer = "/sensors', f'{key} = "x"\npointer = "/sensors'),
            "assumptions": lambda key: self.BASE.replace(
                "\n[signals", f"\n[assumptions]\n{key} = \"x\"\n\n[signals", 1),
            "predicate": lambda key: self.BASE.replace(
                "value = 70.0 }", f"value = 70.0, {key} = \"x\" }}"),
            "window": lambda key: self.BASE + f"window = {{ {key} = \"x\" }}\n",
        }
        for key in ACTION_SHAPED_KEYS:
            for place, build in placements.items():
                with self.subTest(key=key, place=place):
                    with self.assertRaises(ProfileError):
                        load_profile(build(key).encode())

    def test_unknown_check_kinds_and_sources_are_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            load_profile(self.BASE.replace('"holds_throughout"', '"set_output"').encode())
        with self.assertRaises(ProfileError):
            load_profile(self.BASE.replace('source = "dragon"', 'source = "shell"').encode())

    def test_strict_types(self) -> None:
        cases = {
            "boolean threshold": self.BASE.replace("value = 70.0", "value = true").replace('"le"', '"lt"'),
            "negative hold": self.BASE.replace("\n[signals", "\n[assumptions]\nmax_hold_s = -1\n\n[signals", 1),
            "excessive hold": self.BASE.replace("\n[signals", "\n[assumptions]\nmax_hold_s = 4000\n\n[signals", 1),
            "nan threshold": self.BASE.replace("value = 70.0", "value = nan"),
            "wrong format": self.BASE.replace("profile_format = 1", "profile_format = 2"),
            "boolean format": self.BASE.replace("profile_format = 1", "profile_format = true"),
            "relative pointer": self.BASE.replace('pointer = "/sensors', 'pointer = "sensors'),
            "duplicate ids": self.BASE + "\n" + HOLDS_LE_70,
            "unknown signal": self.BASE.replace('signal = "chamber"', 'signal = "other"'),
            "empty window": self.BASE + "window = { start_offset_s = 5, end_offset_s = 5 }\n",
            "not toml": "profile_format = = 1",
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(ProfileError):
                    load_profile(text.encode())
