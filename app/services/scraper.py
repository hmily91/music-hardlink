"""刮削编排：多数据源 fallback + 本地缓存 + 外置资源生成。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.audio import read_audio_info, TrackMetadata
from app.core.config import config
from app.core.database import get_db
from app.core.logger import logger
from app.services import assets
from app.services.providers import PROVIDERS


@dataclass
class ScrapeResult:
    target_path: str
    status: str = "pending"        # done / failed / skipped
    provider: str = ""
    match_score: float = 0.0
    cover_path: str = ""
    lyrics_path: str = ""
    album_nfo_path: str = ""
    artist_nfo_path: str = ""
    error: str = ""


@dataclass
class BatchResult:
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    items: list[ScrapeResult] = field(default_factory=list)


async def _search_providers(info) -> Optional[TrackMetadata]:
    """按配置优先级依次尝试数据源。"""
    providers = config.get("scrape.providers", ["musicbrainz"])
    best: Optional[TrackMetadata] = None
    for name in providers:
        cls = PROVIDERS.get(name)
        if cls is None:
            continue
        try:
            provider = cls()
            meta = await provider.search(info)
            if meta and meta.match_score > 0.2:
                logger.info(f"[{info.path.name}] {name} 命中 score={meta.match_score}")
                return meta
            if meta and (best is None or meta.match_score > best.match_score):
                best = meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"数据源 {name} 异常: {e}")
    return best


async def scrape_track(target_path: str, force: bool = False) -> ScrapeResult:
    """刮削单个曲目（外置资源全生成）。"""
    result = ScrapeResult(target_path=target_path)
    db = get_db()
    tgt = Path(target_path)

    if not tgt.exists():
        result.status = "failed"
        result.error = "文件不存在"
        return result

    # 本地缓存：非 force 且已 done 则跳过
    cached = db.get_scrape(target_path)
    if cached and cached["status"] == "done" and not force:
        result.status = "skipped"
        result.provider = cached["provider"]
        return result

    info = read_audio_info(tgt)
    meta = await _search_providers(info)

    # 洛雪音源补齐歌词/封面（若内置数据源未拿到）
    if config.get("scrape.lx_enabled", True):
        try:
            from app.services.providers.lx import LxSourceProvider
            lx = LxSourceProvider()
            sources = lx.available_sources()
            lx_source = config.get("scrape.lx_source", "")
            if sources:
                use = lx_source if lx_source in sources else sources[0]
                music_info = {
                    "name": info.effective_title,
                    "singer": info.effective_artist,
                    "album": info.album or "",
                }
                if not (meta and meta.lyrics):
                    lyric, tlyric = await lx.fetch_lyrics(use, music_info)
                    if lyric:
                        if meta is None:
                            meta = TrackMetadata()
                        meta.lyrics = lyric
                        meta.translated_lyrics = tlyric
                if not (meta and meta.cover_url):
                    pic = await lx.fetch_pic(use, music_info)
                    if pic:
                        if meta is None:
                            meta = TrackMetadata()
                        meta.cover_url = pic
        except Exception as e:  # noqa: BLE001
            logger.debug(f"洛雪音源补齐失败: {e}")

    if meta is None:
        result.status = "failed"
        result.error = "无数据源命中"
        db.upsert_scrape(_record(result))
        return result

    # 生成外置资源
    artist_dir = tgt.parent.parent
    try:
        if meta.cover_url:
            cover = await assets.write_cover(tgt, meta.cover_url)
            result.cover_path = str(cover) if cover else ""
        lyrics = assets.write_lyrics(tgt, meta)
        result.lyrics_path = str(lyrics) if lyrics else ""
        album_nfo = assets.write_album_nfo(tgt, meta)
        result.album_nfo_path = str(album_nfo) if album_nfo else ""
        artist_nfo = assets.write_artist_nfo(artist_dir, meta.album_artist or meta.artist, meta)
        result.artist_nfo_path = str(artist_nfo) if artist_nfo else ""

        result.status = "done"
        result.provider = meta.provider
        result.match_score = meta.match_score
    except Exception as e:  # noqa: BLE001
        result.status = "failed"
        result.error = str(e)

    db.upsert_scrape(_record(result))
    db.add_log("info" if result.status == "done" else "warn", "scrape",
               f"{result.status}: {tgt.name} ({result.provider})")
    return result


def _record(r: ScrapeResult) -> dict:
    import time
    return {
        "target_path": r.target_path,
        "provider": r.provider,
        "match_score": r.match_score,
        "matched_key": "",
        "status": r.status,
        "cover_path": r.cover_path,
        "lyrics_path": r.lyrics_path,
        "album_nfo_path": r.album_nfo_path,
        "artist_nfo_path": r.artist_nfo_path,
        "error": r.error,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


async def scrape_batch(target_paths: list[str], force: bool = False) -> BatchResult:
    """批量刮削。"""
    result = BatchResult(total=len(target_paths))
    # 简单串行，避免触发限流；可优化为有界并发
    for tp in target_paths:
        r = await scrape_track(tp, force=force)
        result.items.append(r)
        if r.status == "done":
            result.done += 1
        elif r.status == "failed":
            result.failed += 1
        else:
            result.skipped += 1
    return result


async def scrape_album(album_dir: str, force: bool = False) -> BatchResult:
    """刮削整张专辑目录下的所有音频硬链接。"""
    d = Path(album_dir)
    from app.core.config import config as cfg
    tracks = [str(p) for p in d.glob("*") if p.suffix.lower() in cfg.audio_extensions]
    return await scrape_batch(tracks, force=force)


async def scrape_all(force: bool = False) -> BatchResult:
    """刮削媒体库中全部硬链接。"""
    db = get_db()
    tracks = [row["target_path"] for row in db.list_hardlinks("active")]
    return await scrape_batch(tracks, force=force)
