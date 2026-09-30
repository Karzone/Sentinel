"""The installable-app assets. The static directory is served WITHOUT the
password (a manifest is fetched cookie-less), so what may live there is a
security property, not a tidiness one."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from sentinel import cli
from sentinel.dashboard import app

STATIC = Path(app.__file__).parent / "static"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


def test_the_public_static_directory_holds_only_icons_and_the_manifest():
    """Anything else here would be readable by anyone with the URL."""
    assert {p.name for p in STATIC.iterdir()} == {
        "manifest.json", "icon-192.png", "icon-512.png", "apple-touch-icon.png"}


def test_the_manifest_is_installable_and_points_at_files_that_exist():
    manifest = json.loads((STATIC / "manifest.json").read_text())
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert manifest["name"] == "Sentinel"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes               # what Chrome requires to offer install
    for icon in manifest["icons"]:
        name = icon["src"].rsplit("/", 1)[1]
        side = int(icon["sizes"].split("x")[0])
        assert _png_size(STATIC / name) == (side, side)   # declared size is the real size


def test_the_apple_touch_icon_is_180_square():
    assert _png_size(STATIC / "apple-touch-icon.png") == (180, 180)


def test_the_head_injection_is_idempotent_and_loads_before_the_password_gate():
    assert 'getElementById("sx-pwa")' in app._PWA_HEAD          # reruns must not stack tags
    source = Path(app.__file__).read_text()
    assert source.index("_PWA_HEAD,") < source.index("auth.gate(st)")


def test_the_dashboard_command_turns_static_serving_on():
    assert "--server.enableStaticServing" in Path(cli.__file__).read_text()
