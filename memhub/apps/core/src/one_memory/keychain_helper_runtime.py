"""Select the frozen kc-a keychain helper. Stub until the serve hook lands."""

from __future__ import annotations

from pathlib import Path

IDENTITY_SHA256 = "4be9d0207242fa14238b229861f78403879ecc8c8d69ce34890bb4aec4add4b0"
HELPER_ENV = "ONE_MEMORY_FROZEN_KEYCHAIN_HELPER"


def install_frozen_keychain_helper(
    *,
    versions_root: Path | None = None,
    identity_sha256: str | None = None,
    executable: Path | None = None,
) -> str:
    del versions_root, identity_sha256, executable
    return ""


def maybe_install_frozen_keychain_helper(argv: tuple[str, ...] | None = None) -> None:
    del argv
