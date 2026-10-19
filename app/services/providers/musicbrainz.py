"""MusicBrainz 数据源（无需登录，可靠 fallback）。"""
from __future__ import annotations

from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger
from app.services.providers.base import BaseProvider, match_album_score

UA = "MusicHardlink/1.0 (https://github.com; music-scraper)"


class MusicBrainzProvider(BaseProvider):
    name = "musicbrainz"

    def __init__(self) -> None:
        import musicbrainzngs
        musicbrainzngs.set_useragent("MusicHardlink", "1.0")
        self._mb = musicbrainzngs
        self._http = httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=20, follow_redirects=True
        )

    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        meta = TrackMetadata()
        meta.title = info.effective_title
        meta.artist = info.effective_artist
        meta.album = info.album
        meta.album_artist = info.album_artist or info.artist
        meta.duration = info.duration
        meta.year = info.year
        meta.genre = info.genre
        meta.provider = self.name

        try:
            # 先搜录音（recording）
            query = f'{info.effective_title} AND artist:"{info.effective_artist}"'
            result = self._mb.search_recordings(
                recording=info.effective_title,
                artist=info.effective_artist,
                limit=5,
            )
            recordings = result.get("recording-list", [])
            best = None
            best_score = 0.0
            for rec in recordings:
                title = rec.get("title", "")
                artist = rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", "") if rec.get("artist-credit") else ""
                cand = TrackMetadata(
                    title=title, artist=artist, album=info.album,
                    duration=info.duration,
                )
                s = match_album_score(info, cand)
                if s > best_score:
                    best_score = s
                    best = rec
            if best:
                meta.title = best.get("title", info.effective_title)
                if best.get("artist-credit"):
                    meta.artist = best["artist-credit"][0].get("artist", {}).get("name", meta.artist)
                if best.get("length"):
                    meta.duration = int(best["length"]) / 1000.0
                # release 信息：同一录音常有日版/台版/欧美版等多个发行版，
                # 按配置的地区与字形优先级挑选，避免拿到繁体标题或冷门版本
                rel = self._pick_release(best.get("release-list") or [])
                if rel:
                    meta.album = rel.get("title", info.album)
                    date = rel.get("date", "")
                    if date:
                        meta.year = int(date[:4])
                    if rel.get("id"):
                        cover = await self._fetch_cover(rel["id"])
                        if cover:
                            meta.cover_url = cover
            meta.match_score = best_score
            return meta if best_score > 0.2 else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MusicBrainz 搜索失败 {info.path.name}: {e}")
            return None

    def _pick_release(self, releases: list) -> Optional[dict]:
        """按发行地区 / 字形优先级挑选最合适的发行版。"""
        if not releases:
            return None
        region_pri = [
            str(c).upper() for c in (config.get("scrape.release_region_priority", []) or [])
        ]
        script_pri = list(config.get("scrape.release_script_priority", []) or [])

        def rank(rel: dict) -> tuple:
            country = str(rel.get("country") or "").upper()
            script = str((rel.get("text-representation") or {}).get("script", "") or "")
            r = region_pri.index(country) if country in region_pri else len(region_pri)
            s = script_pri.index(script) if script and script in script_pri else len(script_pri)
            return (r, s)

        return sorted(releases, key=rank)[0]

    async def _fetch_cover(self, release_id: str) -> str:
        url = f"https://coverartarchive.org/release/{release_id}/front-500"
        try:
            r = await self._http.get(url)
            # 307 重定向到 images 域名，最终 200 即有效
            if r.status_code in (200, 307):
                return url
        except Exception:  # noqa: BLE001
            pass
        return ""
