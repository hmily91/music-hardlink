"""酷狗音乐数据源（刮削用：元数据 + 封面 + 歌词）。

搜索复用 `app.services.platforms` 的通道矩阵（mobileservice v3 / mobilecdn v3），
歌词走酷狗公开的 lyrics 两阶段接口（先检索 id，再下载 lrc）。
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger
from app.services.providers.base import BaseProvider, match_album_score

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class KugouProvider(BaseProvider):
    name = "kugou"

    def __init__(self) -> None:
        timeout = config.get("scrape.timeout_seconds", 20)
        self._http = httpx.AsyncClient(
            headers={"User-Agent": UA, "Referer": "https://www.kugou.com/"},
            timeout=timeout,
            follow_redirects=True,
        )

    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        try:
            from app.services import platforms

            keyword = f"{info.effective_title} {info.effective_artist}".strip()
            if not keyword:
                return None
            tracks = await platforms.search_platform("kg", keyword, 1)
            if not tracks:
                return None

            best, best_score, best_meta = None, 0.0, None
            for t in tracks[:10]:
                cand = TrackMetadata(
                    title=t.title, artist=t.artist, album=t.album,
                    duration=float(t.duration),
                )
                sc = match_album_score(info, cand)
                if sc > best_score:
                    best_score, best, best_meta = sc, t, cand
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

            lyric, trans = await self._fetch_lyrics(best.external_id, best.duration)
            meta.lyrics = lyric
            meta.translated_lyrics = trans
            return meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"酷狗搜索失败 {getattr(info, 'path', None) and info.path.name}: {e}")
            return None

    async def _fetch_lyrics(self, song_hash: str, duration_sec: int) -> tuple[str, str]:
        """酷狗歌词两阶段：先按 hash+时长检索，再下载 lrc。"""
        if not song_hash:
            return "", ""
        try:
            r = await self._http.get(
                "https://lyrics.kugou.com/search",
                params={
                    "ver": 1, "man": "yes", "client": "pc",
                    "hash": song_hash, "duration": int(duration_sec or 0) * 1000,
                },
            )
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                return "", ""
            pick = cands[0]
            r2 = await self._http.get(
                "https://lyrics.kugou.com/download",
                params={
                    "ver": 1, "client": "pc", "fmt": "lrc", "charset": "utf8",
                    "id": pick.get("id"), "accesskey": pick.get("accesskey"),
                },
            )
            d2 = r2.json()
            content = d2.get("content") or ""
            if not content:
                return "", ""
            import base64

            try:
                return base64.b64decode(content).decode("utf-8", "ignore"), ""
            except Exception:  # noqa: BLE001
                return content, ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"酷狗歌词获取失败: {e}")
            return "", ""
