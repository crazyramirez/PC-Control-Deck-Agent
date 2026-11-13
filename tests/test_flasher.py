"""USB firmware updater: board catalog, deck identification, release parsing and verified downloads
(no hardware, no network)."""

import hashlib
import io
import json

import pytest

from pcdeck_agent.flasher import bundled, probe, releases
from pcdeck_agent.flasher.boards import APP_OFFSET, BOARDS, by_key, for_chip, is_newer, parse_version
from pcdeck_agent.flasher.flash import FlashError, writes
from pcdeck_agent.flasher.releases import Asset, Release, ReleaseError, download, image_path, parse_release, pick

STATUS_70C = (
    "fw 0.12.3 | demo off | link 4 | uptime 28s | reset USB | heap 210216\n"
    "wifi ssid='Net' password=<set> connected=1 ip=192.168.1.5 rssi=-67\n"
    "touch GT911 addr=0x5D configured 1024x600 reads=10 i2c_err=0 rejected=0\n"
)
STATUS_543 = (
    "[   327][I][app_controller.cpp:251] setup(): [app] PC Control Defw 0.12.1 | demo off | link 4 | uptime 7s\n"
    "touch GT911 addr=0x5D configured 480x272 reads=253 i2c_err=0 rejected=0 | frame push 20607 us\n"
)
STATUS_535 = (
    "fw 0.13.1 | demo off | link 4 | uptime 25s | reset USB | heap 116956\n"
    "deck power usb node=0mV pct=-1 (adc pin 0 mV)\n"
    "screen on | touch presses=0 int_low_samples=2\n"
)


def test_catalog():
    assert {b.chip for b in BOARDS} == {"ESP32-S3", "ESP32-P4"}
    assert by_key("JC3248W535").chip == "ESP32-S3"
    assert by_key("nope") is None and by_key(None) is None
    assert [b.key for b in for_chip("ESP32-P4")] == ["JC1060P470C"]
    assert len(for_chip(None)) == len(BOARDS)


def test_variant_and_image_names():
    p4, s3 = by_key("JC1060P470C"), by_key("JC4827W543")
    assert p4.env(1) == "deck" and p4.env(3) == "deck_r3" and p4.env(None) == "deck"
    assert s3.env(0) == "deck"
    assert p4.image("0.12.3", "deck_r3") == "pcdeck-deck_r3-0.12.3-merged.bin"
    assert s3.image("0.12.1", "deck") == "pcdeck-deck-0.12.1-merged.bin"


def test_writes_from_a_merged_image():
    image = bytes(range(256)) * ((APP_OFFSET + 1000) // 256 + 1)
    assert writes(image, clean=True) == [(0x0, image)]
    [(offset, app)] = writes(image, clean=False)  # update: the app part only, NVS below stays
    assert offset == APP_OFFSET and app == image[APP_OFFSET:]
    with pytest.raises(FlashError):
        writes(b"\xff" * 100, clean=False)


def test_pick_prefers_bundled_unless_github_is_newer():
    b = Release("v0.12.3", "0.12.3", source="bundled")
    same, newer, older = (Release(f"v{v}", v) for v in ("0.12.3", "0.12.4", "0.12.2"))
    assert pick(b, same) is b and pick(b, older) is b and pick(b, None) is b
    assert pick(b, newer) is newer and pick(None, newer) is newer and pick(None, None) is None


def test_bundled_manifest(tmp_path):
    data = b"merged image"
    (tmp_path / "JC3248W535").mkdir()
    (tmp_path / "JC3248W535" / "pcdeck-deck-0.13.1-merged.bin").write_bytes(data)
    manifest = {"boards": {
        "JC3248W535": {"tag": "v0.13.1", "assets": [
            {"name": "pcdeck-deck-0.13.1-merged.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            {"name": "../escape.bin", "size": 1, "sha256": None},
        ]},
        "JC4827W543": {"tag": "latest", "assets": []},
    }}  # fmt: skip
    (tmp_path / bundled.MANIFEST).write_text(json.dumps(manifest))
    rel = bundled.load(tmp_path)
    assert list(rel) == ["JC3248W535"]  # a bad tag is skipped
    r = rel["JC3248W535"]
    assert r.source == "bundled" and r.version == "0.13.1" and list(r.assets) == ["pcdeck-deck-0.13.1-merged.bin"]
    asset = r.assets["pcdeck-deck-0.13.1-merged.bin"]
    assert image_path(asset, tmp_path / "cache") == asset.path  # bundled: no download, verified in place
    asset.path.write_bytes(b"tampered!!!!")
    with pytest.raises(ReleaseError):
        image_path(asset, tmp_path / "cache")
    assert bundled.load(tmp_path / "missing") == {}


def test_versions():
    assert parse_version("v0.12.3") == (0, 12, 3) and parse_version("0.9.10") == (0, 9, 10)
    assert parse_version("v1.2") is None and parse_version(None) is None
    assert is_newer("v0.13.0", "0.12.9") and is_newer("0.10.0", "0.9.10")
    assert not is_newer("0.12.3", "0.12.3") and not is_newer(None, "0.1.0")
    assert is_newer("0.1.0", None)  # nothing known installed: any release is newer


@pytest.mark.parametrize(
    "text, key, fw",
    [
        ("pcdeck board=JC3248W535 fw=0.13.2\n", "JC3248W535", "0.13.2"),
        ("noise\r\npcdeck board=JC1060P470C fw=0.12.4\r\n", "JC1060P470C", "0.12.4"),
        (STATUS_70C, "JC1060P470C", "0.12.3"),
        (STATUS_543, "JC4827W543", "0.12.1"),
        (STATUS_535, "JC3248W535", "0.13.1"),
        ("fw 0.9.0 | demo off | link 1\n", None, "0.9.0"),  # a deck, model unknown
        ("ERR unknown command (type 'help')\n", None, None),
        ("", None, None),
    ],
)
def test_identify(text, key, fw):
    assert probe.identify(text) == (key, fw)


class FakeSerial:
    """Answers console commands like the deck firmware (old firmware: no `id`)."""

    def __init__(self, replies):
        self.replies = replies
        self.pending = b""
        self.closed = False
        self.sent = []

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        self.pending += self.replies.get(cmd, "ERR unknown command (type 'help')\n").encode()

    def read(self, size=1):
        out, self.pending = self.pending[:size], self.pending[size:]
        return out

    def close(self):
        self.closed = True


PORT = probe.Port("COM9", "AA:BB:CC:DD:EE:FF", "USB")


def test_probe_with_id():
    s = FakeSerial({"id": "pcdeck board=JC4827W543 fw=0.12.2\n"})
    p = probe.probe(PORT, opener=lambda _dev: s)
    assert p.responsive and p.board.key == "JC4827W543" and p.fw == "0.12.2" and p.error is None
    assert s.sent == ["id"] and s.closed


def test_probe_old_firmware_falls_back_to_status():
    s = FakeSerial({"status": STATUS_535})
    p = probe.probe(PORT, opener=lambda _dev: s)
    assert s.sent == ["id", "status"]
    assert p.responsive and p.board.key == "JC3248W535" and p.fw == "0.13.1"


def test_probe_silent_port():
    p = probe.probe(PORT, opener=lambda _dev: FakeSerial({}))
    assert not p.responsive and p.board is None and p.fw is None and p.error is None


def test_probe_open_failure():
    def boom(_dev):
        raise OSError("could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5)")

    p = probe.probe(PORT, opener=boom)
    assert not p.responsive and p.error == "busy"


def release_json(**asset_extra):
    return {
        "tag_name": "v0.12.3",
        "assets": [
            {"name": "pcdeck-deck-0.12.3.bin", "size": 11, "browser_download_url": "https://example.invalid/a.bin", **asset_extra},
            {"name": "evil.bin", "size": 1, "browser_download_url": "http://example.invalid/b.bin"},  # not https: ignored
        ],
    }


def test_parse_release():
    rel = parse_release(release_json(digest="sha256:" + "AB" * 32))
    assert rel.tag == "v0.12.3" and rel.version == "0.12.3"
    assert list(rel.assets) == ["pcdeck-deck-0.12.3.bin"]
    assert rel.assets["pcdeck-deck-0.12.3.bin"].sha256 == "ab" * 32
    assert parse_release(release_json()).assets["pcdeck-deck-0.12.3.bin"].sha256 is None
    with pytest.raises(ReleaseError):
        parse_release({"tag_name": "nightly"})


def test_download_verifies_and_caches(tmp_path):
    data = b"firmware!!!"
    asset = Asset("pcdeck-deck-0.12.3.bin", "https://example.invalid/a.bin", len(data), hashlib.sha256(data).hexdigest())
    calls = []

    def opener(url):
        calls.append(url)
        return io.BytesIO(data)

    seen = []
    path = download(asset, tmp_path, progress=lambda d, t: seen.append((d, t)), opener=opener)
    assert path.read_bytes() == data and seen[-1] == (len(data), len(data))
    assert download(asset, tmp_path, opener=opener) == path and len(calls) == 1  # cached copy reused
    assert not list(tmp_path.glob("*.part"))


def test_download_rejects_bad_digest_and_size(tmp_path):
    data = b"firmware!!!"
    bad_sha = Asset("pcdeck-deck-0.12.3.bin", "https://example.invalid/a.bin", len(data), "00" * 32)
    with pytest.raises(ReleaseError):
        download(bad_sha, tmp_path, opener=lambda _u: io.BytesIO(data))
    short = Asset("pcdeck-deck-0.12.3.bin", "https://example.invalid/a.bin", len(data) + 5)
    with pytest.raises(ReleaseError):
        download(short, tmp_path, opener=lambda _u: io.BytesIO(data))
    assert not list(tmp_path.iterdir())  # nothing half-written left behind


def test_download_prunes_older_images_of_the_same_variant(tmp_path):
    (tmp_path / "pcdeck-deck-0.12.2.bin").write_bytes(b"old")
    (tmp_path / "pcdeck-deck-0.12.2-merged.bin").write_bytes(b"old merged")
    (tmp_path / "pcdeck-deck_r3-0.12.2.bin").write_bytes(b"other variant")
    data = b"new"
    download(Asset("pcdeck-deck-0.12.3.bin", "https://example.invalid/a.bin", len(data)), tmp_path, opener=lambda _u: io.BytesIO(data))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["pcdeck-deck-0.12.2-merged.bin", "pcdeck-deck-0.12.3.bin", "pcdeck-deck_r3-0.12.2.bin"]


def esp_image(cls_name: str) -> bytes:
    """A tiny valid ESP app image (header, checksum, appended SHA-256) built by esptool itself."""
    import tempfile
    from pathlib import Path

    from esptool import bin_image

    img = getattr(bin_image, cls_name)()
    img.entrypoint = 0x40380000
    seg = bin_image.ImageSegment(0x3FC88000, bytes(range(64)))
    seg.name = ".dram0.data"  # esptool names segments after their ELF section when saving
    img.segments = [seg]
    img.append_digest = True
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "app.bin"
        img.save(str(path))
        return path.read_bytes()


def merged(app: bytes, boot_offset: int = 0x0) -> bytes:
    out = bytearray(b"\xff" * APP_OFFSET)
    out[boot_offset : boot_offset + len(app)] = app  # the same image stands in for the bootloader
    out[0x8000:0x8002] = b"\xaa\x50"
    return bytes(out) + app


def test_image_check():
    from pcdeck_agent.flasher import images

    s3, p4 = by_key("JC3248W535"), by_key("JC1060P470C")
    good = merged(esp_image("ESP32S3FirmwareImage"))
    images.check(good, s3)
    with pytest.raises(images.ImageError, match="chip id"):
        images.check(merged(esp_image("ESP32S3FirmwareImage"), boot_offset=0x2000), p4)  # S3 image on a P4
    bad = bytearray(good)
    bad[APP_OFFSET + 40] ^= 0xFF  # one flipped byte in the app's segment data
    with pytest.raises(images.ImageError):
        images.check(bytes(bad), s3)
    no_table = bytearray(good)
    no_table[0x8000] = 0xFF
    with pytest.raises(images.ImageError, match="partition table"):
        images.check(bytes(no_table), s3)
    with pytest.raises(images.ImageError):
        images.check(good[:APP_OFFSET], s3)


def test_variants_cover_every_board():
    for b in BOARDS:
        names = [env for env, _ in b.variants]
        assert names and names[0] == b.env(None) and len(set(names)) == len(names)


def test_releases_module_only_queries_known_repositories():
    assert all(b.repo.startswith("PC-Control-Deck-ESP32-") for b in BOARDS)
    assert releases.API.startswith("https://api.github.com/repos/")
