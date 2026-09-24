from pathlib import Path
import re
from unittest import TestCase

CANONICAL_OWNER = "thetechbenders"
CANONICAL_IMAGE = f"ghcr.io/{CANONICAL_OWNER}/dragonsniff"

# Active container publication and release workflows. Each must declare the
# canonical image and must not reference any other owner's dragonsniff package.
ACTIVE_WORKFLOWS = (
    ".github/workflows/publish-container.yml",
    ".github/workflows/promote-container.yml",
    ".github/workflows/release.yml",
)

# Every namespace-bearing form a GHCR reference can take in these files:
# image refs, registry token scopes, and raw registry API manifest URLs.
NAMESPACE_FORMS = (
    re.compile(r"ghcr\.io/([A-Za-z0-9_.-]+)/dragonsniff"),
    re.compile(r"repository:([A-Za-z0-9_.-]+)/dragonsniff:"),
    re.compile(r"ghcr\.io/v2/([A-Za-z0-9_.-]+)/dragonsniff/"),
)


class ContainerNamespaceContractTests(TestCase):
    def test_active_workflows_declare_the_canonical_image(self) -> None:
        for path in ACTIVE_WORKFLOWS:
            with self.subTest(path=path):
                text = Path(path).read_text(encoding="utf-8")
                self.assertIn(f"IMAGE_NAME: {CANONICAL_IMAGE}\n", text)

    def test_active_workflows_reference_only_the_canonical_owner(self) -> None:
        for path in ACTIVE_WORKFLOWS:
            text = Path(path).read_text(encoding="utf-8")
            for pattern in NAMESPACE_FORMS:
                for match in pattern.finditer(text):
                    with self.subTest(path=path, reference=match.group(0)):
                        self.assertEqual(match.group(1), CANONICAL_OWNER)

    def test_promotion_registry_calls_use_the_canonical_owner(self) -> None:
        text = Path(".github/workflows/promote-container.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f"scope=repository:{CANONICAL_OWNER}/dragonsniff:pull,push", text
        )
        self.assertIn(f"ghcr.io/v2/{CANONICAL_OWNER}/dragonsniff/manifests/", text)
