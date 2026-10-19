"""网易云音乐数据源。

搜索复用 platforms 的 wy 官方通道（cloudsearch/pc 优先，失败自动降级到
search/get/web），避免旧接口 403/空结果；歌词与专辑详情走免登录公开接口。
"""
from __future__ import annotations

import datetime
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
        from app.services.platforms import search_platform

        try:
            keyword = f"{info.effective_title} {info.effective_artist}".strip()
            tracks = await search_platform("wy", keyword, 1)
            if not tracks:
                return None

            best_track, best_score = None, 0.0
            for t in tracks:
                cand = TrackMetadata(
                    title=t.title, artist=t.artist, album=t.album,
                    duration=float(t.duration or 0),
                )
                sc = match_album_score(info, cand)
                if sc > best_score:
                    best_score, best_track = sc, t

            if best_track is None or best_score < 0.2:
                return None

            t = best_track
            meta = TrackMetadata(
                title=t.title,
                artist=t.artist,
                album=t.album,
                album_artist=t.artist,
                duration=float(t.duration or 0),
                cover_url=t.cover or "",
                provider=self.name,
                match_score=best_score,
            )
            if t.external_id:
                meta.lyrics, meta.translated_lyrics = await self._fetch_lyrics(t.external_id)
            if t.album_id:
                await self._fetch_album(t.album_id, meta)
            return meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"网易云搜索失败 {info.path.name}: {e}")
            return None

    async def _fetch_lyrics(self, song_id: str) -> tuple[str, str]:
        lyrics, trans = "", ""
        try:
            r = await self._http.get(
                "https://music.163.com/api/song/lyric",
                params={"id": song_id, "lv": -1, "kv": -1, "tv": -1},
            )
            data = r.json()
            lyrics = (data.get("lrc") or {}).get("lyric", "")
            trans = (data.get("tlyric") or {}).get("lyric", "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"网易云歌词获取失败: {e}")
        return lyrics, trans

    async def _fetch_album(self, album_id: str, meta: TrackMetadata) -> None:
        """补齐封面与发行年份。"""
        try:
            r = await self._http.get(f"https://music.163.com/api/album/{album_id}")
            album = (r.json() or {}).get("album", {})
            if not album:
                return
            pic = album.get("picUrl")
            if pic:
                meta.cover_url = pic
            if not meta.genre:
                meta.genre = ""
            if album.get("publishTime"):
                try:
                    meta.year = datetime.datetime.fromtimestamp(
                        album["publishTime"] / 1000
                    ).year
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"网易云专辑获取失败: {e}")
