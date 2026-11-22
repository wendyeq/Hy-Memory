"""Point serve at the frozen kc-a keychain helper.

id-a is the identity anchor. It is never the helper. The running executable
is never the helper. Keychain subcommands do not run this check.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path

IDENTITY_SHA256 = "4be9d0207242fa14238b229861f78403879ecc8c8d69ce34890bb4aec4add4b0"
HELPER_ENV = "ONE_MEMORY_FROZEN_KEYCHAIN_HELPER"
SELF_ERROR = "Private helper must not reference the running Core"
IDENTITY_ERROR = "Private helper identity mismatch"
_KEYCHAIN_ARGV = {
    ("--keychain-read",),
    ("--keychain-write",),
    ("--keychain-delete",),
    ("--keychain-delete", "--many"),
}


def default_versions_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "One Memory" / "versions"


def install_frozen_keychain_helper(
    *,
    versions_root: Path | None = None,
    identity_sha256: str | None = None,
    executable: Path | None = None,
) -> str:
    """Validate the anchor and point ONE_MEMORY_FROZEN_KEYCHAIN_HELPER at kc-a.

    A failed check leaves the variable unset. Repeating a successful check
    selects the same helper.
    """
    os.environ.pop(HELPER_ENV, None)
    root = default_versions_root() if versions_root is None else versions_root
    identity = (root / "id-a" / "core" / "one-memory").resolve()
    helper = (root / "kc-a" / "core" / "one-memory").resolve()
    running = (Path(sys.executable) if executable is None else executable).resolve()
    if running in (identity, helper):
        raise RuntimeError(SELF_ERROR)
    if not identity.is_file() or not helper.is_file() or identity == helper:
        raise RuntimeError(IDENTITY_ERROR)
    expected = IDENTITY_SHA256 if identity_sha256 is None else identity_sha256
    with identity.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != expected:
        raise RuntimeError(IDENTITY_ERROR)
    os.environ[HELPER_ENV] = str(helper)
    return str(helper)


def maybe_install_frozen_keychain_helper(argv: Sequence[str] | None = None) -> None:
    """Serve entry. Isolated tests skip unless they inject a versions root."""
    args = tuple(sys.argv[1:] if argv is None else argv)
    if args in _KEYCHAIN_ARGV:
        return
    if os.environ.get("ONE_MEMORY_TEST_ISOLATED") == "1":
        raw_root = os.environ.get("ONE_MEMORY_VERSIONS_ROOT", "").strip()
        if not raw_root:
            return
        install_frozen_keychain_helper(versions_root=Path(raw_root))
        return
    identity = default_versions_root() / "id-a" / "core" / "one-memory"
    if not getattr(sys, "frozen", False) and not identity.is_file():
        return
    install_frozen_keychain_helper()
