"""Frozen kc-a helper selection. These tests never read or write Keychain items."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from one_memory import keychain_helper_runtime as helper_runtime
from one_memory import secrets as secret_module
from one_memory.secrets import SecretStoreUnavailableError

FIXTURE_IDENTITY = b"kc-helper-identity-fixture"
FIXTURE_SHA256 = "92af667fce4369c9d60404f3a306009dcfd8fda6cf1d4ecaba94c84ed0b42af6"
RECOVERED_ID_A_SHA256 = "4be9d0207242fa14238b229861f78403879ecc8c8d69ce34890bb4aec4add4b0"
SELF_ERROR = "Private helper must not reference the running Core"
IDENTITY_ERROR = "Private helper identity mismatch"

_DELETE_MANY = (
    "import json,sys; d=json.load(sys.stdin); "
    'sys.stdout.write(json.dumps({"deleted": len(d["references"])}))'
)
_PROXY = """#!/bin/sh
dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
printf '%s\\n' "$0" "$@" > "$dir/last-argv"
if [ "${1-}" = "--keychain-delete" ] && [ "${2-}" = "--many" ]; then
  python3 -c 'DELETE_MANY'
  exit 0
fi
cat >/dev/null
case "${1-}" in
  --keychain-read) printf '%s' '{"value":null}' ;;
  --keychain-write) printf '%s' '{"stored": true}' ;;
  --keychain-delete) printf '%s' '{"deleted": true}' ;;
  *) exit 2 ;;
esac
"""


def _versions(tmp_path: Path, *, identity: bytes | None, helper: bool) -> Path:
    root = tmp_path / "versions"
    identity_path = root / "id-a" / "core" / "one-memory"
    helper_path = root / "kc-a" / "core" / "one-memory"
    identity_path.parent.mkdir(parents=True)
    helper_path.parent.mkdir(parents=True)
    if identity is not None:
        identity_path.write_bytes(identity)
    if helper:
        helper_path.write_bytes(b"frozen-helper")
    return root


def _install(root: Path, executable: Path) -> str:
    return helper_runtime.install_frozen_keychain_helper(
        versions_root=root,
        identity_sha256=FIXTURE_SHA256,
        executable=executable,
    )


def _proxy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PROXY.replace("DELETE_MANY", _DELETE_MANY), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _refuse_success(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{"value":null}', "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_recovered_id_a_digest_is_the_identity_pin() -> None:
    assert helper_runtime.IDENTITY_SHA256 == RECOVERED_ID_A_SHA256


def test_install_selects_the_kc_a_helper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(helper_runtime.HELPER_ENV, raising=False)
    root = _versions(tmp_path, identity=FIXTURE_IDENTITY, helper=True)
    running = tmp_path / "running-core"
    running.write_bytes(b"running")
    selected = _install(root, running)
    again = _install(root, running)
    helper = root / "kc-a" / "core" / "one-memory"
    assert selected == str(helper.resolve())
    assert again == selected
    assert os.environ[helper_runtime.HELPER_ENV] == selected
    assert Path(selected).parts[-3:] == ("kc-a", "core", "one-memory")


def test_default_versions_root_uses_application_support_layout() -> None:
    assert helper_runtime.default_versions_root() == (
        Path.home() / "Library" / "Application Support" / "One Memory" / "versions"
    )


def test_install_uses_the_default_versions_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(helper_runtime.HELPER_ENV, raising=False)
    root = _versions(tmp_path, identity=FIXTURE_IDENTITY, helper=True)
    running = tmp_path / "running-core"
    running.write_bytes(b"running")
    monkeypatch.setattr(helper_runtime, "default_versions_root", lambda: root)
    live_root = Path.home() / "Library" / "Application Support" / "One Memory"
    live_identity = live_root / "versions" / "id-a" / "core" / "one-memory"
    live_helper = live_root / "versions" / "kc-a" / "core" / "one-memory"
    before_identity = live_identity.read_bytes() if live_identity.is_file() else None
    before_helper = live_helper.read_bytes() if live_helper.is_file() else None
    selected = helper_runtime.install_frozen_keychain_helper(
        identity_sha256=FIXTURE_SHA256,
        executable=running,
    )
    helper = root / "kc-a" / "core" / "one-memory"
    assert selected == str(helper.resolve())
    assert os.environ[helper_runtime.HELPER_ENV] == selected
    if before_identity is None:
        assert not live_identity.exists()
    else:
        assert live_identity.read_bytes() == before_identity
        assert before_identity != FIXTURE_IDENTITY
    if before_helper is None:
        assert not live_helper.exists()
    else:
        assert live_helper.read_bytes() == before_helper
        assert before_helper != b"frozen-helper"


def test_install_refuses_to_run_as_id_a(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(helper_runtime.HELPER_ENV, "stale-helper")
    root = _versions(tmp_path, identity=FIXTURE_IDENTITY, helper=True)
    with pytest.raises(RuntimeError, match=SELF_ERROR):
        _install(root, root / "id-a" / "core" / "one-memory")
    assert helper_runtime.HELPER_ENV not in os.environ


def test_install_refuses_to_run_as_the_helper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(helper_runtime.HELPER_ENV, "stale-helper")
    root = _versions(tmp_path, identity=FIXTURE_IDENTITY, helper=True)
    with pytest.raises(RuntimeError, match=SELF_ERROR):
        _install(root, root / "kc-a" / "core" / "one-memory")
    assert helper_runtime.HELPER_ENV not in os.environ


def test_install_refuses_an_identity_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(helper_runtime.HELPER_ENV, "stale-helper")
    root = _versions(tmp_path, identity=b"not-the-anchor", helper=True)
    with pytest.raises(RuntimeError, match=IDENTITY_ERROR):
        _install(root, tmp_path / "running-core")
    assert helper_runtime.HELPER_ENV not in os.environ


@pytest.mark.parametrize("identity,helper", [(None, True), (FIXTURE_IDENTITY, False)])
def test_install_refuses_a_missing_anchor_file(
    tmp_path: Path, monkeypatch, identity: bytes | None, helper: bool
) -> None:
    monkeypatch.setenv(helper_runtime.HELPER_ENV, "stale-helper")
    root = _versions(tmp_path, identity=identity, helper=helper)
    with pytest.raises(RuntimeError, match=IDENTITY_ERROR):
        _install(root, tmp_path / "running-core")
    assert helper_runtime.HELPER_ENV not in os.environ


def test_keychain_flags_do_not_select_a_helper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(helper_runtime.HELPER_ENV, raising=False)
    root = _versions(tmp_path, identity=FIXTURE_IDENTITY, helper=True)
    monkeypatch.setenv("ONE_MEMORY_TEST_ISOLATED", "1")
    monkeypatch.setenv("ONE_MEMORY_VERSIONS_ROOT", str(root))
    monkeypatch.setattr(helper_runtime, "IDENTITY_SHA256", FIXTURE_SHA256)
    helper_runtime.maybe_install_frozen_keychain_helper(("--keychain-read",))
    helper_runtime.maybe_install_frozen_keychain_helper(("--keychain-write",))
    helper_runtime.maybe_install_frozen_keychain_helper(("--keychain-delete",))
    helper_runtime.maybe_install_frozen_keychain_helper(("--keychain-delete", "--many"))
    assert helper_runtime.HELPER_ENV not in os.environ
    helper_runtime.maybe_install_frozen_keychain_helper(("serve",))
    helper = root / "kc-a" / "core" / "one-memory"
    assert os.environ[helper_runtime.HELPER_ENV] == str(helper.resolve())


def test_serve_selects_the_helper(tmp_path: Path, monkeypatch) -> None:
    import uvicorn

    from one_memory import api, cli, hy_runtime_config, log_file, runtime

    root = _versions(tmp_path, identity=FIXTURE_IDENTITY, helper=True)
    monkeypatch.delenv(helper_runtime.HELPER_ENV, raising=False)
    monkeypatch.setenv("ONE_MEMORY_TEST_ISOLATED", "1")
    monkeypatch.setenv("ONE_MEMORY_VERSIONS_ROOT", str(root))
    monkeypatch.setattr(helper_runtime, "IDENTITY_SHA256", FIXTURE_SHA256)
    settings = type("Settings", (), {"data_dir": tmp_path, "host": "127.0.0.1", "port": 12345})()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        hy_runtime_config,
        "configure_hy_runtime_environment",
        lambda _data_dir: None,
    )
    monkeypatch.setattr(log_file, "start_logging_to_file", lambda _data_dir: None)
    monkeypatch.setattr(runtime, "parent_pid", lambda: None)
    monkeypatch.setattr(api, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    cli.serve()
    helper = root / "kc-a" / "core" / "one-memory"
    assert os.environ[helper_runtime.HELPER_ENV] == str(helper.resolve())


def test_bounded_commands_use_the_helper_flags(tmp_path: Path, monkeypatch) -> None:
    proxy = tmp_path / "kc-a" / "core" / "one-memory"
    _proxy(proxy)
    monkeypatch.setenv(helper_runtime.HELPER_ENV, str(proxy))
    real_run = subprocess.run

    def run(command, **kwargs):
        if Path(command[0]).resolve() != proxy.resolve():
            raise AssertionError(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert secret_module._bounded_keychain_read("ref-only") is None
    secret_module._bounded_keychain_write("ref-only", "fixture-value")
    secret_module._bounded_keychain_delete("ref-only")
    secret_module._bounded_keychain_delete_many(("ref-a", "ref-b"))
    recorded = (proxy.parent / "last-argv").read_text(encoding="utf-8").splitlines()
    assert Path(recorded[0]).resolve() == proxy.resolve()
    assert recorded[1:] == ["--keychain-delete", "--many"]
    text = (proxy.parent / "last-argv").read_text(encoding="utf-8")
    assert "fixture-value" not in text


def test_each_helper_flag_is_recorded(tmp_path: Path, monkeypatch) -> None:
    proxy = tmp_path / "helper-proxy"
    _proxy(proxy)
    monkeypatch.setenv(helper_runtime.HELPER_ENV, str(proxy))
    real_run = subprocess.run

    def run(command, **kwargs):
        if Path(command[0]).resolve() != proxy.resolve():
            raise AssertionError(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    secret_module._bounded_keychain_read("ref-only")
    assert (proxy.parent / "last-argv").read_text(encoding="utf-8").splitlines()[1:] == [
        "--keychain-read"
    ]
    secret_module._bounded_keychain_write("ref-only", "fixture-value")
    assert (proxy.parent / "last-argv").read_text(encoding="utf-8").splitlines()[1:] == [
        "--keychain-write"
    ]
    secret_module._bounded_keychain_delete("ref-only")
    assert (proxy.parent / "last-argv").read_text(encoding="utf-8").splitlines()[1:] == [
        "--keychain-delete"
    ]


def test_a_missing_helper_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(helper_runtime.HELPER_ENV, str(tmp_path / "missing-helper"))
    calls = _refuse_success(monkeypatch)
    with pytest.raises(SecretStoreUnavailableError):
        secret_module._bounded_keychain_read("ref-only")
    assert calls == []


def test_id_a_is_not_used_as_the_helper(tmp_path: Path, monkeypatch) -> None:
    identity = tmp_path / "versions" / "id-a" / "core" / "one-memory"
    _proxy(identity)
    monkeypatch.setenv(helper_runtime.HELPER_ENV, str(identity))
    calls = _refuse_success(monkeypatch)
    with pytest.raises(SecretStoreUnavailableError):
        secret_module._bounded_keychain_read("ref-only")
    assert calls == []


def test_the_running_core_is_not_used_as_the_helper(monkeypatch) -> None:
    monkeypatch.setenv(helper_runtime.HELPER_ENV, sys.executable)
    calls = _refuse_success(monkeypatch)
    with pytest.raises(SecretStoreUnavailableError):
        secret_module._bounded_keychain_read("ref-only")
    assert calls == []


def test_without_a_helper_id_a_is_not_a_fallback(tmp_path: Path, monkeypatch) -> None:
    identity = tmp_path / "versions" / "id-a" / "core" / "one-memory"
    _proxy(identity)
    monkeypatch.delenv(helper_runtime.HELPER_ENV, raising=False)
    monkeypatch.setattr(secret_module.sys, "executable", str(identity))
    monkeypatch.setattr(secret_module.sys, "frozen", True, raising=False)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{"stored": true}', "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SecretStoreUnavailableError):
        secret_module._bounded_keychain_write("ref-only", "fixture-value")
    assert calls == []
