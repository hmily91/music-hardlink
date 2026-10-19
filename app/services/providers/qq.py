"""QQ 音乐数据源。

搜索复用 platforms 的 tx 官方通道（search_for_qq_cp 等，失败自动降级），
原先直连的 client_search_cp 已失效（返回 500）。歌词走免登录公开接口。
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger
from app.services.providers.base import BaseProvider, match_album_score


class QQMusicProvider(BaseProvider):
    name = "qq"

    def __init__(self) -> None:
        cookie = config.get("scrape.qq_cookie", "")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://y.qq.com",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._http = httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True)

    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        from app.services.platforms import search_platform

        try:
            keyword = f"{info.effective_title} {info.effective_artist}".strip()
            tracks = await search_platform("tx", keyword, 1)
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
            songmid = t.external_id
            if songmid:
                meta.lyrics, meta.translated_lyrics = await self._fetch_lyrics(songmid)
            if t.album_id:
                await self._fetch_album(t.album_id, meta)
                if not meta.cover_url:
                    meta.cover_url = (
                        f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{t.album_id}.jpg"
                    )
            return meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QQ音乐搜索失败 {info.path.name}: {e}")
            return None

    async def _fetch_lyrics(self, songmid: str) -> tuple[str, str]:
        lyrics, trans = "", ""
        try:
            r = await self._http.get(
                "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                params={
                    "songmid": songmid, "format": "json", "nobase64": 1,
                    "g_tk": 5381, "loginUin": 0, "hostUin": 0,
                },
            )
            data = r.json()
            lyrics = data.get("lyric", "") or ""
            trans = data.get("trans", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"QQ音乐歌词获取失败: {e}")
        return lyrics, trans

    async def _fetch_album(self, albummid: str, meta: TrackMetadata) -> None:
        try:
            r = await self._http.get(
                "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg",
                params={"albummid": albummid, "format": "json", "new_format": 1},
            )
            data = r.json().get("data", {})
            if data.get("aDate"):
                meta.year = int(str(data["aDate"])[:4])
            if data.get("genre"):
                meta.genre = data["genre"]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"QQ音乐专辑获取失败: {e}")
