"""App self-update: which installs can update themselves, which release asset they take, the swap
script (run for real on Linux / macOS), the no-retry-loop rule."""

import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from pcdeck_agent import updater
from pcdeck_agent.flasher.releases import Asset, Release

ASSETS = [
    "PCControlDeck-0.9.0-linux-x86_64.tar.gz",
    "PCControlDeck-0.9.0-macos-arm64.dmg",
    "PCControlDeck-0.9.0-macos-arm64.zip",
    "PCControlDeck-0.9.0-macos-x86_64.dmg",
    "PCControlDeck-0.9.0-macos-x86_64.zip",
    "PCControlDeck-0.9.0-windows-x64-portable.zip",
    "PCControlDeck-Setup-0.9.0.exe",
]
RELEASE = Release("v0.9.0", "0.9.0", {n: Asset(n, f"https://example.invalid/{n}", 10) for n in ASSETS})


@pytest.mark.parametrize(
    "kind, arch, expected",
    [
        ("windows", "x86_64", "PCControlDeck-Setup-0.9.0.exe"),
        ("macos", "arm64", "PCControlDeck-0.9.0-macos-arm64.zip"),
        ("macos", "x86_64", "PCControlDeck-0.9.0-macos-x86_64.zip"),
        ("linux", "x86_64", "PCControlDeck-0.9.0-linux-x86_64.tar.gz"),
        ("linux", "arm64", None),  # no build for this machine: manual update
        (None, "x86_64", None),  # portable / source: manual update
    ],
)
def test_pick_asset(kind, arch, expected):
    u = updater.pick(RELEASE, "0.8.0", kind, arch)
    assert u is not None and u.version == "0.9.0"
    assert (u.asset.name if u.asset else None) == expected


def test_pick_nothing_newer():
    assert updater.pick(RELEASE, "0.9.0", "windows", "x86_64") is None
    assert updater.pick(RELEASE, "0.10.0", "windows", "x86_64") is None


def test_install_kind(tmp_path):
    exe = tmp_path / "PCControlDeck.exe"
    exe.write_bytes(b"")
    assert updater.install_kind(exe, frozen=False, platform_name="win32") is None  # from source
    assert updater.install_kind(exe, frozen=True, platform_name="win32") is None  # portable zip
    (tmp_path / "setup-tasks.ps1").write_text("")
    assert updater.install_kind(exe, frozen=True, platform_name="win32") == "windows"
    app_exe = tmp_path / "PC Control Deck.app" / "Contents" / "MacOS" / "PCControlDeck"
    app_exe.parent.mkdir(parents=True)
    app_exe.write_bytes(b"")
    assert updater.install_kind(app_exe, frozen=True, platform_name="darwin") == "macos"
    assert updater.bundle_of(app_exe.resolve()).name == "PC Control Deck.app"
    assert updater.install_kind(exe, frozen=True, platform_name="darwin") is None
    assert updater.install_kind(exe, frozen=True, platform_name="linux") == "linux"


def test_machine_names(monkeypatch):
    for raw, norm in (("AMD64", "x86_64"), ("x86_64", "x86_64"), ("arm64", "arm64"), ("aarch64", "arm64")):
        monkeypatch.setattr(updater.platform, "machine", lambda r=raw: r)
        assert updater.machine() == norm


def test_no_retry_loop():
    now = time.time()
    assert updater.recently_failed("0.9.0", "0.9.0", now - 60, now)
    assert not updater.recently_failed("0.9.0", "0.9.0", now - updater.RETRY_FAILED_AFTER_S - 1, now)
    assert not updater.recently_failed("0.9.1", "0.9.0", now - 60, now)  # a newer release is tried


def test_fetch_without_asset_is_manual():
    with pytest.raises(updater.UpdateError) as e:
        updater.fetch(updater.Update("0.9.0", None))
    assert e.value.key == "update_manual"


def test_swap_script_text():
    P = PurePosixPath  # the script only ever runs on macOS / Linux
    s = updater.swap_script(P("/opt/a b/PCControlDeck"), P("/tmp/x/PCControlDeck"), P("/tmp/x"), ["/opt/a b/PCControlDeck/PCControlDeck", "--minimized"], 4242)
    assert "kill -0 4242" in s
    assert "'/opt/a b/PCControlDeck'" in s  # paths with spaces are quoted
    assert "exec '/opt/a b/PCControlDeck/PCControlDeck' --minimized" in s


@pytest.mark.skipif(sys.platform == "win32", reason="the swap script runs with /bin/sh (macOS / Linux)")
def test_swap_script_replaces_and_relaunches(tmp_path):
    old = tmp_path / "app" / "PCControlDeck"
    old.mkdir(parents=True)
    (old / "version").write_text("old")
    work = tmp_path / "work"
    new = work / "unpacked" / "PCControlDeck"
    new.mkdir(parents=True)
    (new / "version").write_text("new")
    marker = tmp_path / "relaunched"
    dead_pid = 2**22 + 12345  # no such process: the script does not wait
    script = updater.swap_script(old, new, work, ["/bin/sh", "-c", f"echo ok > '{marker}'"], dead_pid)
    path = tmp_path / "swap.sh"
    path.write_text(script)
    subprocess.run(["/bin/sh", str(path)], check=True, timeout=30)
    assert (old / "version").read_text() == "new"
    assert not work.exists() and not Path(str(old) + ".old-update").exists()
    assert marker.read_text().strip() == "ok"


def test_can_replace(tmp_path):
    assert updater.can_replace("windows")
    assert not updater.can_replace(None)
    exe = tmp_path / "PCControlDeck" / "PCControlDeck"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert updater.can_replace("linux", exe) == (os.access(tmp_path, os.W_OK) and os.access(exe.parent, os.W_OK))
