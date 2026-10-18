"""网易云音乐数据源（免登录公开接口，可选 Cookie 提升精度）。"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger
from app.services.providers.base import BaseProvider, match_album_score


class NeteaseProvider(BaseProvider):
    name = "netease"

    def __init__(self) -> None:
        cookie = config.get("scrape.netease_cookie", "")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://music.163.com",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._http = httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True)

    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        try:
            keyword = f"{info.effective_title} {info.effective_artist}".strip()
            url = "https://music.163.com/api/search/get/web"
            params = {"s": keyword, "type": 1, "offset": 0, "limit": 5}
            r = await self._http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            songs = (data.get("result") or {}).get("songs", [])
            if not songs:
                return None

            best, best_score = None, 0.0
            for s in songs:
                artists = "/".join(a.get("name", "") for a in s.get("artists", []))
                album = (s.get("album") or {}).get("name", "")
                cand = TrackMetadata(
                    title=s.get("name", ""), artist=artists, album=album,
                    duration=(s.get("duration", 0) or 0) / 1000.0,
                )
                sc = match_album_score(info, cand)
                if sc > best_score:
                    best_score = sc
                    best = s

            if not best or best_score < 0.2:
                return None

            sid = best["id"]
            meta = TrackMetadata()
            meta.title = best.get("name", info.effective_title)
            meta.artist = "/".join(a.get("name", "") for a in best.get("artists", []))
            meta.album = (best.get("album") or {}).get("name", "")
            meta.album_artist = meta.artist
            meta.duration = (best.get("duration", 0) or 0) / 1000.0
            meta.match_score = best_score
            meta.provider = self.name
            album_id = (best.get("album") or {}).get("id")
            if album_id:
                meta.cover_url = f"https://music.163.com/api/song/enhance/player/url"  # placeholder
                # 封面用专辑
                meta.cover_url = f"https://p2.music.126.net/.../"

            # 歌词
            lyrics, trans = await self._fetch_lyrics(sid)
            meta.lyrics = lyrics
            meta.translated_lyrics = trans

            # 专辑详情（封面/年份）
            if album_id:
                await self._fetch_album(album_id, meta)

            return meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"网易云搜索失败 {info.path.name}: {e}")
            return None

    async def _fetch_lyrics(self, song_id: int) -> tuple[str, str]:
        lyrics, trans = "", ""
        try:
            url = "https://music.163.com/api/song/lyric"
            params = {"id": song_id, "lv": -1, "kv": -1, "tv": -1}
            r = await self._http.get(url, params=params)
            data = r.json()
            lyrics = (data.get("lrc") or {}).get("lyric", "")
            trans = (data.get("tlyric") or {}).get("lyric", "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"网易云歌词获取失败: {e}")
        return lyrics, trans

    async def _fetch_album(self, album_id: int, meta: TrackMetadata) -> None:
        try:
            url = f"https://music.163.com/api/album/{album_id}"
            r = await self._http.get(url)
            data = r.json()
            album = data.get("album", {})
            if album:
                pic = album.get("picUrl")
                if pic:
                    meta.cover_url = pic
                date = album.get("publishTime") or album.get("company")
                if album.get("publishTime"):
                    import datetime
                    try:
                        meta.year = datetime.datetime.fromtimestamp(
                            album["publishTime"] / 1000
                        ).year
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"网易云专辑获取失败: {e}")
