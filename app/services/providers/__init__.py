"""数据源注册与编排。"""
from __future__ import annotations

from app.services.providers.base import BaseProvider
from app.services.providers.musicbrainz import MusicBrainzProvider
from app.services.providers.netease import NeteaseProvider
from app.services.providers.qq import QQMusicProvider
from app.services.providers.lx import LxSourceProvider

PROVIDERS: dict[str, type[BaseProvider]] = {
    "musicbrainz": MusicBrainzProvider,
    "netease": NeteaseProvider,
    "qq": QQMusicProvider,
    "lx": LxSourceProvider,
}
