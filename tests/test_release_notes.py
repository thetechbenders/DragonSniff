from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest import TestCase

sys.path.insert(0, str(Path("scripts").resolve()))
import release_notes  # noqa: E402

WORKFLOW = Path(".github/workflows/release.yml")
IMAGE = "ghcr.io/thetechbenders/dragonsniff"
SHA = "f030eeba9ce46273d72a49ecd9495ae1d3e2397d"
DIGEST = "sha256:" + "f" * 64
WHEEL = "dragonsniff-0.5.0-py3-none-any.whl"
WHEEL_SHA = "b" * 64


def _notes_dir(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def _artifacts(tag: str = "v0.5.0") -> str:
    return release_notes.artifacts_section(IMAGE, tag, SHA, DIGEST, WHEEL, WHEEL_SHA)


class StablePresentationTests(TestCase):
    def test_stable_release_without_curated_notes_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires curated notes"):
            release_notes.load_presentation("v9.9.9", False, _notes_dir({}))

    def test_stable_title_must_carry_a_curated_suffix(self) -> None:
        for first_line in (
            "# DragonSniff v9.9.9",
            "# DragonSniff v9.9.9 — ",
            "# DragonSniff v9.9.9 - Hyphen is not the separator",
            "# DragonSniff v9.9.8 — Wrong version",
            "DragonSniff v9.9.9 — Not a heading",
        ):
            with self.subTest(first_line=first_line):
                notes = _notes_dir({"v9.9.9.md": f"{first_line}\n\nBody.\n"})
                with self.assertRaisesRegex(ValueError, "must start with"):
                    release_notes.load_presentation("v9.9.9", False, notes)

    def test_title_only_notes_are_rejected(self) -> None:
        notes = _notes_dir({"v9.9.9.md": "# DragonSniff v9.9.9 — Nothing here\n\n"})
        with self.assertRaisesRegex(ValueError, "no release notes"):
            release_notes.load_presentation("v9.9.9", False, notes)

    def test_curated_notes_may_not_hand_write_artifacts(self) -> None:
        notes = _notes_dir(
            {"v9.9.9.md": "# DragonSniff v9.9.9 — Title\n\nBody.\n\n## Artifacts\n\n- x\n"}
        )
        with self.assertRaisesRegex(ValueError, "workflow adds it"):
            release_notes.load_presentation("v9.9.9", False, notes)

    def test_valid_stable_notes_keep_title_and_body(self) -> None:
        notes = _notes_dir({"v9.9.9.md": "# DragonSniff v9.9.9 — Tie it off\n\nBody.\n"})
        presentation = release_notes.load_presentation("v9.9.9", False, notes)
        self.assertEqual(presentation.mode, "curated")
        self.assertEqual(presentation.title, "DragonSniff v9.9.9 — Tie it off")
        self.assertEqual(presentation.body, "Body.")


class RenderedArtifactTests(TestCase):
    def test_artifacts_retain_wheel_checksum_and_container_identity(self) -> None:
        artifacts = _artifacts()
        for expected in (
            f"- Wheel: `{WHEEL}`",
            f"- Wheel SHA256: `{WHEEL_SHA}`",
            f"- Container: `{IMAGE}:v0.5.0`",
            f"- Immutable container: `{IMAGE}:sha-{SHA}`",
            f"- Container manifest digest: `{DIGEST}`",
        ):
            self.assertIn(expected, artifacts)

    def test_version_tag_is_never_labelled_immutable(self) -> None:
        for line in _artifacts().splitlines():
            if line.startswith("- Immutable container:"):
                self.assertIn(":sha-", line)
                self.assertNotIn(":v0.5.0", line)

    def test_artifacts_precede_full_changelog(self) -> None:
        presentation = release_notes.Presentation(
            "curated", "t", "Intro.\n\n## Full changelog\n\nlink"
        )
        body = release_notes.render_body(presentation, _artifacts())
        self.assertLess(body.index("## Artifacts"), body.index("## Full changelog"))
        self.assertTrue(body.startswith("Intro."))

    def test_rendered_body_has_no_doubled_blank_lines(self) -> None:
        presentation = release_notes.load_presentation("v0.5.0", False)
        body = release_notes.render_body(presentation, _artifacts())
        self.assertNotIn("\n\n\n", body)

    def test_artifacts_append_when_no_full_changelog(self) -> None:
        presentation = release_notes.Presentation("curated", "t", "Intro.")
        body = release_notes.render_body(presentation, _artifacts())
        self.assertTrue(body.startswith("Intro.\n\n## Artifacts"))


class ReleaseCandidatePresentationTests(TestCase):
    def test_rc_without_curated_notes_uses_generated_fallback(self) -> None:
        presentation = release_notes.load_presentation(
            "v9.9.9-rc.1", True, _notes_dir({})
        )
        self.assertEqual(presentation.mode, "generated")
        self.assertEqual(presentation.title, "DragonSniff v9.9.9-rc.1")
        body = release_notes.render_body(presentation, _artifacts("v9.9.9-rc.1"))
        self.assertTrue(body.startswith("## Artifacts"))

    def test_rc_with_curated_notes_is_held_to_the_same_rules(self) -> None:
        bad = _notes_dir({"v9.9.9-rc.1.md": "# DragonSniff v9.9.9-rc.1\n\nBody.\n"})
        with self.assertRaisesRegex(ValueError, "must start with"):
            release_notes.load_presentation("v9.9.9-rc.1", True, bad)
        good = _notes_dir(
            {"v9.9.9-rc.1.md": "# DragonSniff v9.9.9-rc.1 — Dry run\n\nBody.\n"}
        )
        self.assertEqual(
            release_notes.load_presentation("v9.9.9-rc.1", True, good).mode, "curated"
        )


class CommandLineTests(TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/release_notes.py", *args],
            capture_output=True,
            text=True,
        )

    def test_check_fails_closed_for_missing_stable_notes(self) -> None:
        result = self._run("--notes-dir", str(_notes_dir({})), "check", "v9.9.9", "false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires curated notes", result.stderr)

    def test_render_writes_title_and_body(self) -> None:
        out = Path(tempfile.mkdtemp())
        result = self._run(
            "render", "v0.5.0", "false",
            "--image-name", IMAGE, "--release-sha", SHA,
            "--manifest-digest", DIGEST, "--wheel-name", WHEEL,
            "--wheel-sha256", WHEEL_SHA,
            "--title-out", str(out / "title"), "--body-out", str(out / "body"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "curated")
        self.assertEqual(
            (out / "title").read_text(encoding="utf-8"),
            "DragonSniff v0.5.0 — Connect the dots",
        )
        body = (out / "body").read_text(encoding="utf-8")
        self.assertIn(f"- Container manifest digest: `{DIGEST}`", body)
        self.assertNotIn("Connect the dots", body)


class CommittedReleaseNotesTests(TestCase):
    def test_every_committed_release_note_is_valid(self) -> None:
        pattern = re.compile(r"\Av\d+\.\d+\.\d+(-rc\.\d+)?\.md\Z")
        files = sorted(Path("docs/releases").glob("*.md"))
        self.assertIn(Path("docs/releases/v0.5.0.md"), files)
        for path in files:
            with self.subTest(path=path.name):
                self.assertRegex(path.name, pattern)
                tag = path.stem
                presentation = release_notes.load_presentation(tag, "-rc." in tag)
                self.assertEqual(presentation.mode, "curated")

    def test_v0_5_0_keeps_its_approved_title(self) -> None:
        presentation = release_notes.load_presentation("v0.5.0", False)
        self.assertEqual(presentation.title, "DragonSniff v0.5.0 — Connect the dots")


class ReleaseWorkflowPresentationContractTests(TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_presentation_is_validated_before_any_mutation(self) -> None:
        check = self.workflow.index("python scripts/release_notes.py check")
        self.assertLess(check, self.workflow.index("\n  tag:\n"))
        self.assertLess(check, self.workflow.index("Verify and smoke-test immutable image"))

    def test_release_title_comes_from_rendered_presentation(self) -> None:
        self.assertNotIn('--title "DragonSniff $RELEASE_TAG"', self.workflow)
        titles = re.findall(r"--title (\S+)", self.workflow)
        self.assertTrue(titles)
        for title in titles:
            self.assertEqual(title, '"$(cat')

    def test_generated_notes_are_guarded_as_rc_only(self) -> None:
        self.assertEqual(self.workflow.count("--generate-notes"), 1)
        generated = self.workflow.index("--generate-notes")
        guard = self.workflow.rindex('test "$PRERELEASE" = true', 0, generated)
        branch = self.workflow.rindex('if test "$notes_mode" = curated', 0, generated)
        self.assertLess(branch, guard)
