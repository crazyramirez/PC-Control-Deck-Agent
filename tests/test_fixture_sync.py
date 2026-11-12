"""The firmware parser tests embed the shared fixtures; make sure they are not stale."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_firmware_fixture_header_up_to_date():
    spec = importlib.util.spec_from_file_location("gen_fixtures", ROOT / "firmware" / "tools" / "gen_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    header = (ROOT / "firmware" / "test" / "test_protocol" / "fixtures.h").read_text(encoding="utf-8")
    assert header == mod.render(), "run: python firmware/tools/gen_fixtures.py"
