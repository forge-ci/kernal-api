"""Compatibility metadata for the native :mod:`kernal_api` systems crate.

The operating-system and profiler implementation lives in the Rust crate.
This dependency-free companion lets Python launchers enforce the same release
and platform contract without shipping a second implementation.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass

__version__ = "0.1.36"
RUST_MSRV = "1.95.0"
SUPPORTED_SYSTEMS = frozenset({"Linux", "Darwin", "Windows"})
SUPPORTED_MACHINES = frozenset({"x86_64", "amd64", "aarch64", "arm64"})


@dataclass(frozen=True)
class Compatibility:
    """The current interpreter and host's first-party support status."""

    supported: bool
    system: str
    machine: str
    python: tuple[int, int]


def _compatibility_for(system: str, machine: str, python: tuple[int, int]) -> Compatibility:
    """Evaluate explicit values; kept separate so every release target is tested."""

    normalized_machine = machine.casefold()
    return Compatibility(
        supported=(python >= (3, 10) and system in SUPPORTED_SYSTEMS and normalized_machine in SUPPORTED_MACHINES),
        system=system,
        machine=machine,
        python=python,
    )


def compatibility() -> Compatibility:
    """Return the same OS/architecture/Python gate used by release CI."""

    return _compatibility_for(
        platform.system(),
        platform.machine(),
        (sys.version_info.major, sys.version_info.minor),
    )


__all__ = [
    "Compatibility",
    "RUST_MSRV",
    "SUPPORTED_MACHINES",
    "SUPPORTED_SYSTEMS",
    "__version__",
    "compatibility",
]
