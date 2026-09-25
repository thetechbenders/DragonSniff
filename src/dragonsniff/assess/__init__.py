"""Offline assessment of DragonSniff evidence. See docs/assess.md.

The package is deliberately closed: it takes bytes and returns text. It has no
filesystem, network, process, or device access, and it imports nothing from
DragonSniff's observation, capture, or server code.
"""

from __future__ import annotations

from .evidence import EvidenceError, load_evidence
from .profile import ProfileError, load_profile
from .report import (
    ASSESS_SEMANTICS_VERSION,
    ASSESSMENT_FORMAT,
    STATEMENT,
    build_report,
    render_report,
)

__all__ = [
    "ASSESS_SEMANTICS_VERSION",
    "ASSESSMENT_FORMAT",
    "EvidenceError",
    "ProfileError",
    "STATEMENT",
    "assess",
]


def assess(
    evidence: bytes,
    profile: bytes,
    *,
    metadata: bytes | None = None,
    assessed_at: str | None = None,
) -> str:
    """Assess evidence bytes against profile bytes and return the report JSON.

    Raises EvidenceError or ProfileError when no trustworthy report can be
    produced. assessed_at, if supplied, is echoed as report metadata only.
    """
    loaded_profile = load_profile(profile)
    loaded_evidence = load_evidence(evidence, metadata)
    return render_report(build_report(loaded_evidence, loaded_profile, assessed_at))
