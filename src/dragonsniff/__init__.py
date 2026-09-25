"""DragonSniff local Dragon API observer."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ._version import __version__

# Re-exports resolve on first access (PEP 562). Importing a subpackage such as
# dragonsniff.assess must not load the observer, client, or network stack.
_LAZY_EXPORTS = {
    "DeviceTarget": ".target",
    "Observer": ".observer",
    "SessionRecorder": ".recording",
    "TargetValidationError": ".target",
    "parse_target": ".target",
}

__all__ = [
    "DeviceTarget",
    "Observer",
    "SessionRecorder",
    "TargetValidationError",
    "__version__",
    "parse_target",
]


def __getattr__(name: str) -> Any:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
