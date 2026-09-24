"""Validate and render DragonSniff GitHub Release presentation.

Curated notes live at ``docs/releases/<tag>.md``. The file's first line is the
release title as a Markdown H1:

    # DragonSniff v0.5.0 — Connect the dots

Everything after that line is the curated body. The workflow owns the
artifact section; curated files must not include one.

Stable releases require curated notes. Release candidates may use them, and
otherwise fall back to a plain title with GitHub-generated notes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys

DEFAULT_NOTES_DIR = Path("docs/releases")
ARTIFACTS_HEADING = "## Artifacts"
FULL_CHANGELOG_HEADING = "## Full changelog"
TITLE_SEPARATOR = " — "


@dataclass(frozen=True, slots=True)
class Presentation:
    mode: str  # "curated" or "generated"
    title: str
    body: str


def _title_pattern(release_tag: str) -> re.Pattern[str]:
    return re.compile(
        rf"\A# (DragonSniff {re.escape(release_tag)}{TITLE_SEPARATOR}\S.*?)\s*\Z"
    )


def load_presentation(
    release_tag: str, prerelease: bool, notes_dir: Path = DEFAULT_NOTES_DIR
) -> Presentation:
    """Return the curated presentation, or the RC-only generated fallback."""
    path = notes_dir / f"{release_tag}.md"
    if not path.is_file():
        if not prerelease:
            raise ValueError(
                f"stable release {release_tag} requires curated notes at {path}"
            )
        return Presentation("generated", f"DragonSniff {release_tag}", "")

    lines = path.read_text(encoding="utf-8").splitlines()
    first = lines[0] if lines else ""
    match = _title_pattern(release_tag).match(first)
    if match is None:
        raise ValueError(
            f"{path} must start with '# DragonSniff {release_tag}"
            f"{TITLE_SEPARATOR}<title>'"
        )
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError(f"{path} has a title but no release notes")
    if re.search(rf"^{re.escape(ARTIFACTS_HEADING)}\s*$", body, re.M):
        raise ValueError(
            f"{path} must not include '{ARTIFACTS_HEADING}'; the workflow adds it"
        )
    return Presentation("curated", match.group(1), body)


def artifacts_section(
    image_name: str,
    release_tag: str,
    release_sha: str,
    manifest_digest: str,
    wheel_name: str,
    wheel_sha256: str,
) -> str:
    return "\n".join(
        [
            ARTIFACTS_HEADING,
            "",
            f"- Wheel: `{wheel_name}`",
            f"- Wheel SHA256: `{wheel_sha256}`",
            f"- Container: `{image_name}:{release_tag}`",
            f"- Immutable container: `{image_name}:sha-{release_sha}`",
            f"- Container manifest digest: `{manifest_digest}`",
        ]
    )


def render_body(presentation: Presentation, artifacts: str) -> str:
    """Place the artifact section before a trailing full-changelog section."""
    body = presentation.body
    if not body:
        return artifacts + "\n"
    marker = f"\n{FULL_CHANGELOG_HEADING}"
    index = body.find(marker)
    if index == -1:
        return f"{body}\n\n{artifacts}\n"
    return f"{body[:index].rstrip()}\n\n{artifacts}\n{body[index:]}\n"


def _bool(value: str) -> bool:
    if value not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected 'true' or 'false'")
    return value == "true"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-dir", type=Path, default=DEFAULT_NOTES_DIR)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check")
    check.add_argument("release_tag")
    check.add_argument("prerelease", type=_bool)

    render = commands.add_parser("render")
    render.add_argument("release_tag")
    render.add_argument("prerelease", type=_bool)
    for name in (
        "image-name",
        "release-sha",
        "manifest-digest",
        "wheel-name",
        "wheel-sha256",
        "title-out",
        "body-out",
    ):
        render.add_argument(f"--{name}", required=True)

    args = parser.parse_args(argv)
    try:
        presentation = load_presentation(
            args.release_tag, args.prerelease, args.notes_dir
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.command == "check":
        print(f"{presentation.mode}: {presentation.title}")
        return 0

    artifacts = artifacts_section(
        args.image_name,
        args.release_tag,
        args.release_sha,
        args.manifest_digest,
        args.wheel_name,
        args.wheel_sha256,
    )
    Path(args.title_out).write_text(presentation.title, encoding="utf-8")
    Path(args.body_out).write_text(
        render_body(presentation, artifacts), encoding="utf-8"
    )
    print(presentation.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
