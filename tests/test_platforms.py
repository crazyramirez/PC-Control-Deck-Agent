"""Provider / backend selection by OS, per-OS paths, credential file permissions, start-at-login files."""

import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest

from pcdeck_agent import config, platforms
from pcdeck_agent.commands import router
from pcdeck_agent.gui import autostart_posix as AP
from pcdeck_agent.security.credentials import CredentialStore

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


@pytest.mark.parametrize("plat,expected", [("win32", "windows"), ("linux", "linux"), ("darwin", "macos"), ("freebsd14", "freebsd14")])
def test_current(monkeypatch, plat, expected):
    monkeypatch.setattr(sys, "platform", plat)
    assert platforms.current() == expected


def test_hardware_provider_and_battery_by_platform(monkeypatch):
    from pcdeck_agent.hardware.battery import BatteryReader
    from pcdeck_agent.hardware.linux_battery import LinuxBatteryReader
    from pcdeck_agent.hardware.linux_provider import LinuxProvider
    from pcdeck_agent.hardware.macos_battery import MacBatteryReader
    from pcdeck_agent.hardware.macos_provider import MacProvider

    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(platforms.hardware_provider(), LinuxProvider)
    assert isinstance(platforms.battery_reader(), LinuxBatteryReader)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(platforms.hardware_provider(), MacProvider)
    assert isinstance(platforms.battery_reader(), MacBatteryReader)
    monkeypatch.setattr(sys, "platform", "win32")
    from pcdeck_agent.hardware.lhm_provider import LhmProvider

    assert isinstance(platforms.hardware_provider(), LhmProvider)  # constructed only, not opened
    assert type(platforms.battery_reader()) is BatteryReader
    monkeypatch.setattr(sys, "platform", "sunos5")
    with pytest.raises(RuntimeError):
        platforms.hardware_provider()


def test_cli_create_provider_uses_platform(monkeypatch):
    from pcdeck_agent.__main__ import create_provider

    monkeypatch.setattr(sys, "platform", "aix")
    with pytest.raises(SystemExit):
        create_provider(False)
    assert create_provider(True).status().name == "fake"


def test_command_backend_by_platform(monkeypatch):
    from pcdeck_agent.commands import linux_media, macos_media

    made = []

    def stub(name):
        class Stub:
            def __init__(self, allow_power):
                made.append((name, allow_power()))

        return Stub

    monkeypatch.setattr(router, "WindowsBackend", stub("windows"))
    monkeypatch.setattr(linux_media, "LinuxBackend", stub("linux"))
    monkeypatch.setattr(macos_media, "MacBackend", stub("macos"))
    for plat in ("win32", "linux", "darwin"):
        monkeypatch.setattr(sys, "platform", plat)
        assert router.default_backend(lambda: True) is not None
    assert made == [("windows", True), ("linux", True), ("macos", True)]
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert router.default_backend() is None

    def broken(_allow):
        raise OSError("no D-Bus")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux_media, "LinuxBackend", broken)
    assert router.default_backend() is None  # logged, agent keeps running without commands


def test_app_and_log_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert config.app_dir() == tmp_path / "cfg" / "pcdeck"
    assert config.log_dir() == tmp_path / "state" / "pcdeck" / "logs"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.delenv("XDG_STATE_HOME")
    assert config.app_dir() == tmp_path / ".config" / "pcdeck"
    assert config.log_dir() == tmp_path / ".local" / "state" / "pcdeck" / "logs"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.app_dir() == tmp_path / "Library" / "Application Support" / "PC Control Deck"
    assert config.log_dir() == tmp_path / "Library" / "Logs" / "PC Control Deck"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.app_dir() == tmp_path / "local" / "PcControlDeck"  # unchanged on Windows
    assert config.log_dir() == tmp_path / "local" / "PcControlDeck" / "logs"


def test_credential_store_roundtrip_and_owner_only(tmp_path):
    p = tmp_path / "pcdeck" / "devices.dat"
    s = CredentialStore(p)  # Windows: DPAPI; Linux/macOS: plain JSON readable by the owner only
    token = s.add("deck-1", "Mesa")
    assert token.encode() not in p.read_bytes()  # only the hash is stored
    assert CredentialStore(p).verify("deck-1", token)
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) & 0o077 == 0


@posix_only
def test_credential_store_tightens_loose_permissions(tmp_path):
    p = tmp_path / "devices.dat"
    CredentialStore(p).add("deck-1", "Mesa")
    os.chmod(p, 0o644)
    assert len(CredentialStore(p).devices()) == 1
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_linux_autostart_entry(tmp_path):
    cmd = ["/opt/PC Control Deck/PCControlDeck", "--minimized"]
    assert not AP.is_enabled("linux", tmp_path)
    assert AP.set_enabled(True, "linux", tmp_path, cmd) == (True, "")
    entry = (tmp_path / ".config" / "autostart" / "pcdeck.desktop").read_text(encoding="utf-8")
    assert 'Exec="/opt/PC Control Deck/PCControlDeck" --minimized' in entry and "Type=Application" in entry
    assert AP.is_enabled("linux", tmp_path)
    (tmp_path / ".config" / "autostart" / "pcdeck.desktop").write_text(entry + "Hidden=true\n", encoding="utf-8")
    assert not AP.is_enabled("linux", tmp_path)  # disabled from the desktop's own settings
    assert AP.set_enabled(False, "linux", tmp_path) == (True, "") and not AP.is_enabled("linux", tmp_path)
    assert AP._exec_quote('a"b$c') == '"a\\"b\\$c"' and AP._exec_quote("--minimized") == "--minimized"


def test_macos_launch_agent(tmp_path):
    cmd = ["/Applications/PC Control Deck.app/Contents/MacOS/PCControlDeck", "--minimized"]
    assert AP.set_enabled(True, "macos", tmp_path, cmd) == (True, "")
    plist = plistlib.loads((tmp_path / "Library" / "LaunchAgents" / "com.pcdeck.agent.plist").read_bytes())
    assert plist == {"Label": "com.pcdeck.agent", "ProgramArguments": cmd, "RunAtLoad": True,
                     "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}  # fmt: skip
    assert AP.is_enabled("macos", tmp_path)
    AP.set_enabled(False, "macos", tmp_path)
    assert not AP.is_enabled("macos", tmp_path)


def test_launch_command(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert AP.launch_command()[1:] == ["-m", "pcdeck_agent.gui.app", "--minimized"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("APPIMAGE", "/home/u/PCControlDeck.AppImage")
    assert AP.launch_command() == ["/home/u/PCControlDeck.AppImage", "--minimized"]
