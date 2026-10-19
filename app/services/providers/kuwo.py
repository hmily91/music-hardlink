"""酷我音乐数据源（刮削用：元数据 + 封面 + 歌词）。

搜索复用 `app.services.platforms` 的 r.s 通道（伪 JSON，模块内已做容错解析），
歌词走酷我移动端 songinfoandlrc 接口。
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger
from app.services.providers.base import BaseProvider, match_album_score

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class KuwoProvider(BaseProvider):
    name = "kuwo"

    def __init__(self) -> None:
        timeout = config.get("scrape.timeout_seconds", 20)
        self._http = httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True
        )

    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        try:
            from app.services import platforms

            keyword = f"{info.effective_title} {info.effective_artist}".strip()
            if not keyword:
                return None
            tracks = await platforms.search_platform("kw", keyword, 1)
            if not tracks:
                return None

            best, best_score = None, 0.0
            for t in tracks[:10]:
                cand = TrackMetadata(
                    title=t.title, artist=t.artist, album=t.album,
                    duration=float(t.duration),
                )
                sc = match_album_score(info, cand)
                if sc > best_score:
                    best_score, best = sc, t
            if best is None or best_score < 0.2:
                return None

            meta = TrackMetadata()
            meta.title = best.title
            meta.artist = best.artist
            meta.album = best.album
            meta.album_artist = best.artist
            meta.duration = float(best.duration)
            meta.match_score = best_score
            meta.provider = self.name
            meta.cover_url = best.cover

            lyric = await self._fetch_lyrics(best.external_id)
            meta.lyrics = lyric
            return meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"酷我搜索失败 {getattr(info, 'path', None) and info.path.name}: {e}")
            return None

    async def _fetch_lyrics(self, music_id: str) -> str:
        """酷我歌词：返回 LRC 文本。"""
        if not music_id:
            return ""
        try:
            r = await self._http.get(
                "https://m.kuwo.cn/newh5/singles/songinfoandlrc",
                params={"musicId": music_id},
            )
            data = r.json()
            rows = ((data.get("data") or {}).get("lrclist")) or []
            if not rows:
                return ""
            lines = []
            for row in rows:
                t = float(row.get("time") or 0)
                mm, ss = int(t // 60), t % 60
                lines.append(f"[{mm:02d}:{ss:05.2f}]{row.get('lineLyric','')}")
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"酷我歌词获取失败: {e}")
            return ""
