"""QQ 音乐数据源（免登录公开接口，可选 Cookie）。"""
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
        try:
            keyword = f"{info.effective_title} {info.effective_artist}".strip()
            url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            params = {
                "w": keyword, "format": "json", "p": 1, "n": 5,
                "t": 0, "aggr": 1, "lossless": 0, "cr": 1, "new_json": 1,
            }
            r = await self._http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            songs = (data.get("data", {}).get("song", {}) or {}).get("list", [])
            if not songs:
                return None

            best, best_score = None, 0.0
            for s in songs:
                artists = "/".join(a.get("name", "") for a in s.get("singer", []))
                cand = TrackMetadata(
                    title=s.get("name", "") or s.get("songname", ""),
                    artist=artists,
                    album=s.get("albumname", "") or (s.get("album", {}).get("name", "") if isinstance(s.get("album"), dict) else ""),
                    duration=(s.get("interval", 0) or 0),
                )
                sc = match_album_score(info, cand)
                if sc > best_score:
                    best_score = sc
                    best = s

            if not best or best_score < 0.2:
                return None

            meta = TrackMetadata()
            meta.title = best.get("name", "") or best.get("songname", "")
            meta.artist = "/".join(a.get("name", "") for a in best.get("singer", []))
            meta.album = best.get("albumname", "")
            meta.album_artist = meta.artist
            meta.duration = best.get("interval", 0) or 0
            meta.match_score = best_score
            meta.provider = self.name

            songmid = best.get("songmid") or best.get("mid")
            albummid = (best.get("album", {}) or {}).get("mid") if isinstance(best.get("album"), dict) else best.get("albummid")

            if albummid:
                cover = f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{albummid}.jpg"
                meta.cover_url = cover
                await self._fetch_album(albummid, meta)

            if songmid:
                lyrics, trans = await self._fetch_lyrics(songmid)
                meta.lyrics = lyrics
                meta.translated_lyrics = trans

            return meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QQ音乐搜索失败 {info.path.name}: {e}")
            return None

    async def _fetch_album(self, albummid: str, meta: TrackMetadata) -> None:
        try:
            url = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
            params = {"albummid": albummid, "format": "json", "new_format": 1}
            r = await self._http.get(url, params=params)
            data = r.json().get("data", {})
            if data.get("aDate"):
                meta.year = int(str(data["aDate"])[:4])
            if data.get("genre"):
                meta.genre = data["genre"]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"QQ音乐专辑获取失败: {e}")

    async def _fetch_lyrics(self, songmid: str) -> tuple[str, str]:
        lyrics, trans = "", ""
        try:
            url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
            params = {
                "songmid": songmid, "format": "json", "nobase64": 1,
                "g_tk": 5381, "loginUin": 0, "hostUin": 0,
            }
            r = await self._http.get(url, params=params)
            data = r.json()
            lyrics = data.get("lyric", "") or ""
            trans = data.get("trans", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"QQ音乐歌词获取失败: {e}")
        return lyrics, trans
