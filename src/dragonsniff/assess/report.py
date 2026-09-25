"""Build and render the deterministic assessment report."""

from __future__ import annotations

import json
from typing import Any

from .._version import __version__
from .engine import evaluate
from .evidence import Evidence
from .profile import Profile

ASSESSMENT_FORMAT = 1
ASSESS_SEMANTICS_VERSION = 1
STATEMENT = (
    "PASS means the listed assertions were satisfied by sufficient captured "
    "evidence under this assessment profile. It is not a safety certification."
)
RESULTS = ("PASS", "FAIL", "INCONCLUSIVE")


def _run(evidence: Evidence) -> dict[str, Any]:
    run = evidence.run
    started, terminal = run.started, run.terminal
    return {
        "run_id": run.run_id,
        "problem": run.problem,
        "started_sequence": started["sequence"] if started else None,
        "started_utc": started["timestamp"] if started else None,
        "terminal_kind": terminal["kind"] if terminal else None,
        "terminal_sequence": terminal["sequence"] if terminal else None,
        "terminal_utc": terminal["timestamp"] if terminal else None,
    }


def build_report(
    evidence: Evidence, profile: Profile, assessed_at: str | None = None
) -> dict[str, Any]:
    findings = [evaluate(evidence, profile, check) for check in profile.checks]
    report: dict[str, Any] = {
        "assessment_format": ASSESSMENT_FORMAT,
        "assess_semantics_version": ASSESS_SEMANTICS_VERSION,
        "dragonsniff_version": __version__,
        "statement": STATEMENT,
        "profile": {
            "name": profile.name,
            "version": profile.version,
            "sha256": profile.sha256,
            "byte_length": profile.byte_length,
        },
        "evidence": {
            "sha256": evidence.sha256,
            "byte_length": evidence.byte_length,
            "record_count": len(evidence.records),
            "first_sequence": evidence.records[0]["sequence"],
            "last_sequence": evidence.records[-1]["sequence"],
            "sequence_gaps": [list(gap) for gap in evidence.gaps],
            "monotonic_inversions": evidence.monotonic_inversions,
            "run": _run(evidence),
            "session": evidence.session,
        },
        "assumptions": {
            "hold": "bounded" if profile.max_hold_ns else "none",
            "max_hold_s": profile.max_hold_s,
        },
        "summary": {
            result: sum(1 for f in findings if f["result"] == result)
            for result in RESULTS
        },
        "findings": findings,
    }
    if assessed_at is not None:
        # Presentation metadata only. Never read by evaluation.
        report["metadata"] = {"assessed_at": assessed_at}
    return report


def render_report(report: dict[str, Any]) -> str:
    """Render deterministically: sorted keys, fixed separators, final newline."""
    return (
        json.dumps(
            report, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )
