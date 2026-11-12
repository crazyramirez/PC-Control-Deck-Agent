"""Known online services playing in a browser tab (Instagram, YouTube, TikTok, Netflix...).

Sites without Media Session metadata reach Windows' media controls with only the tab title and,
as artwork, the browser's own logo. For those, the service is recognised from the tab title and
the deck gets a generated cover (brand colours + icon + name) and "Instagram · Chrome" as source.
Everything is local: no request is made to the service or to any other site.

Only applied to browser sessions that publish no artist (sites with real metadata, e.g. YouTube
videos with channel + thumbnail, keep their own data).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BROWSERS = ("Chrome", "Edge", "Firefox", "Opera", "Brave")


@dataclass(frozen=True)
class Service:
    name: str
    pattern: re.Pattern  # matched against the tab title
    colors: tuple[str, ...]  # gradient stops, top-left -> bottom-right
    icon: str  # Segoe Fluent/MDL2 icon code point, or "" for a letter mark
    letter: str = ""  # big letter instead of an icon (e.g. Netflix "N")


def _p(words: str) -> re.Pattern:
    # The service name as a whole word (titles like "Reel by x • Instagram", "Song - YouTube").
    return re.compile(rf"(?<![\w.]){words}(?![\w])", re.IGNORECASE)


_CAMERA, _PLAY, _VIDEO, _NOTE, _TV, _CHAT = "", "", "", "", "", ""

SERVICES: tuple[Service, ...] = (
    Service("YouTube Music", _p(r"youtube\s+music"), ("#FF2D2D", "#8A0000"), _NOTE),
    Service("YouTube", _p(r"youtube"), ("#FF3B30", "#B00000"), _PLAY),
    Service("Instagram", _p(r"instagram"), ("#FEDA75", "#FA7E1E", "#D62976", "#962FBF", "#4F5BD5"), _CAMERA),
    Service("TikTok", _p(r"tiktok"), ("#25F4EE", "#111111", "#FE2C55"), _NOTE),
    Service("Twitch", _p(r"twitch"), ("#A970FF", "#5C16C5"), _CHAT),
    Service("Netflix", _p(r"netflix"), ("#2A0A0C", "#000000"), "", "N"),
    Service("Prime Video", _p(r"prime\s+video"), ("#1FB6FF", "#0F3D66"), _PLAY),
    Service("Disney+", re.compile(r"disney\s*\+|disney\s*plus", re.IGNORECASE), ("#1A3BD6", "#06144F"), _PLAY),
    Service("Max", re.compile(r"hbo\s*max|(\s[-|]\sMax$)|(^Max$)", re.IGNORECASE), ("#2B5BFF", "#04124D"), _PLAY),
    Service("Facebook", _p(r"facebook"), ("#3B8BFF", "#0B4FC0"), _VIDEO),
    Service("X", re.compile(r"(\s/\sX$)|(\son\sX:)|(^X$)|twitter", re.IGNORECASE), ("#2A2F36", "#000000"), "", "X"),
    Service("Vimeo", _p(r"vimeo"), ("#36C5F0", "#0E7FAF"), _PLAY),
    Service("Dailymotion", _p(r"dailymotion"), ("#2F7BFF", "#0A2A6B"), _PLAY),
    Service("Kick", re.compile(r"(\s[-|]\sKick$)|(^Kick$)|kick\.com", re.IGNORECASE), ("#53FC18", "#0B3D02"), _VIDEO),
    Service("SoundCloud", _p(r"soundcloud"), ("#FF8A2B", "#E33B00"), _NOTE),
    Service("Spotify", _p(r"spotify"), ("#2EE06B", "#0E6B30"), _NOTE),
    Service("Deezer", _p(r"deezer"), ("#B05CFF", "#4A148C"), _NOTE),
    Service("Tidal", _p(r"tidal"), ("#3A3A3A", "#000000"), _NOTE),
    Service("Apple Music", _p(r"apple\s+music"), ("#FF5A6E", "#C4102B"), _NOTE),
    Service("Apple TV", _p(r"apple\s+tv\+?"), ("#4A4A4A", "#0A0A0A"), _TV),
    Service("Crunchyroll", _p(r"crunchyroll"), ("#FF8F3A", "#C24A00"), _PLAY),
    Service("Mixcloud", _p(r"mixcloud"), ("#6B7BFF", "#2A2F8F"), _NOTE),
    Service("Bandcamp", _p(r"bandcamp"), ("#3FC4D9", "#1A6A78"), _NOTE),
)

# Separators sites put between the content title and their own name.
_SEP = r"\s*(?:[-–—|•·:]|on)\s*"


def is_browser(app: str | None) -> bool:
    return bool(app) and any(app == b or app.startswith(b + " ") for b in BROWSERS)


def detect(app: str | None, title: str | None, artist: str | None) -> Service | None:
    """The service playing in a browser tab without metadata, or None."""
    if not is_browser(app) or artist or not title:
        return None
    for s in SERVICES:
        if s.pattern.search(title):
            return s
    return None


def clean_title(service: Service, title: str) -> str:
    """'Song name - YouTube' -> 'Song name'; a title that is only the service name stays as is."""
    words = re.escape(service.name).replace(r"\ ", r"\s+")
    t = re.sub(rf"{_SEP}{words}\s*$", "", title, flags=re.IGNORECASE).strip()
    t = re.sub(rf"^\(\d+\)\s*", "", t)  # "(3) Instagram" notification counters
    return t or title
