"""洛雪音源数据源：通过 Node.js lx-bridge 调用用户导入的洛雪 .js 音源。

洛雪音源脚本提供 lyric / pic action（歌词、封面），部分音源也提供 musicUrl。
本 provider 通过 HTTP 调用 lx-bridge 的 /action 接口。
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger
from app.services.providers.base import BaseProvider


class LxSourceProvider(BaseProvider):
    """通过洛雪音源桥接获取歌词/封面。

    注意：洛雪音源脚本通常没有 search 能力（search 在桌面端由内置源完成），
    所以本 provider 主要补齐 lyric/pic；search 仍由 musicbrainz/netease/qq 承担。
    """

    name = "lx"

    def __init__(self) -> None:
        self._bridge_url = config.get("scrape.lx_bridge_url", "http://127.0.0.1:2692")
        self._http = httpx.AsyncClient(timeout=30)

    def available_sources(self) -> list[str]:
        """返回桥接服务中已初始化的音源 key。"""
        try:
            import httpx as _h
            r = _h.get(f"{self._bridge_url}/sources", timeout=5)
            if r.status_code == 200:
                return [s["key"] for s in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            pass
        return []

    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        # 洛雪音源无 search，返回 None 交给其它数据源
        return None

    async def fetch_lyrics(self, source: str, music_info: dict) -> tuple[str, str]:
        """通过指定音源获取歌词，返回 (lyric, tlyric)。"""
        import json
        try:
            r = await self._http.get(
                f"{self._bridge_url}/action",
                params={
                    "source": source,
                    "action": "lyric",
                    "musicInfo": json.dumps(music_info, ensure_ascii=False),
                },
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                if isinstance(data, dict):
                    lyric = data.get("lyric", "") or data.get("lrc", "") or ""
                    tlyric = data.get("tlyric", "") or data.get("tlyric", "") or ""
                    return lyric, tlyric
        except Exception as e:  # noqa: BLE001
            logger.debug(f"洛雪音源歌词获取失败: {e}")
        return "", ""

    async def fetch_pic(self, source: str, music_info: dict) -> str:
        """通过指定音源获取封面 URL。"""
        import json
        try:
            r = await self._http.get(
                f"{self._bridge_url}/action",
                params={
                    "source": source,
                    "action": "pic",
                    "musicInfo": json.dumps(music_info, ensure_ascii=False),
                },
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                if isinstance(data, str) and data.startswith("http"):
                    return data
                if isinstance(data, dict):
                    return data.get("url", "") or data.get("pic", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"洛雪音源封面获取失败: {e}")
        return ""
