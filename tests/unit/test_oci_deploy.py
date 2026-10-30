"""deploy/oci: cloud-init rendering is complete, quoted correctly and valid bash."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from api.auth import hash_password

ROOT = Path(__file__).resolve().parents[2]


def load_provision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "provision", ROOT / "deploy" / "oci" / "provision.py"
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_fills_every_placeholder_and_keeps_the_hash_literal(tmp_path: Path) -> None:
    provision = load_provision()
    pw_hash = hash_password("a long test password")
    script = provision.render_cloud_init(
        repo_url="https://github.com/example/repo.git", branch="main", admin_password_hash=pw_hash
    )
    assert "__" not in "".join(line for line in script.splitlines() if "PLACEHOLDER" not in line)
    assert f"ADMIN_PASSWORD_HASH='{pw_hash}'" in script  # single quotes: no $ expansion in bash
    path = tmp_path / "rendered.sh"
    path.write_text(script)
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(path)], check=True)
    # the value bash assigns is the hash, byte for byte
    out = subprocess.run(
        ["bash", "-c", f"ADMIN_PASSWORD_HASH='{pw_hash}'; printf %s \"$ADMIN_PASSWORD_HASH\""],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert out == pw_hash


def test_render_rejects_quotes() -> None:
    provision = load_provision()
    with pytest.raises(ValueError, match="single quotes"):
        provision.render_cloud_init(repo_url="r", branch="b", admin_password_hash="x'y")


def test_omni_lines_extracts_progress_from_console_noise() -> None:
    provision = load_provision()
    console = "\x1b[0m[  12.3] cloud-init[812]: OMNI: start arch=aarch64\r\nnoise\nOMNI: DONE url=https://x\n"
    assert provision.omni_lines(console) == ["start arch=aarch64", "DONE url=https://x"]


def test_cloud_init_never_prints_generated_secrets() -> None:
    script = (ROOT / "deploy" / "oci" / "cloud-init.sh").read_text()
    assert 'init_env.py --domain "$DOMAIN" > /dev/null' in script
    assert "set -x" not in script
    assert "set -euxo" not in script
    for line in script.splitlines():
        if "mark " in line and ("key" in line.lower() and "$key" in line):
            pytest.fail(f"stream key printed: {line}")


def test_provision_cli_reports_its_results() -> None:
    """Regression: a lint autofix once deleted every print(), so `up` silently lost the
    generated admin password. The operator-facing lines must stay."""
    source = (ROOT / "deploy" / "oci" / "provision.py").read_text()
    for needle in (
        'print(f"public ip:',
        'print(f"admin login: admin / ',
        "print(line, flush=True)",
    ):
        assert needle in source, needle
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"deploy/**" = ["T20"]' in pyproject


def test_down_only_deletes_what_it_created() -> None:
    from types import SimpleNamespace

    provision = load_provision()
    assert provision.ours(SimpleNamespace(freeform_tags=dict(provision.TAGS)))
    assert not provision.ours(SimpleNamespace(freeform_tags={"project": "omnistream"}))
    assert not provision.ours(SimpleNamespace(freeform_tags=None))
    assert not provision.ours(SimpleNamespace())


def test_down_requires_confirmation() -> None:
    provision = load_provision()
    with pytest.raises(SystemExit, match="--yes"):
        provision.main(["down"])
