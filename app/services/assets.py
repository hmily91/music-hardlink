"""外置资源生成：封面、歌词(LRC)、双语歌词、NFO 信息文件。

所有资源独立存储，绝不内嵌进音频文件。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import httpx

from app.core.audio import AudioInfo, TrackMetadata
from app.core.config import config
from app.core.logger import logger

# NFO 模板（兼容 Plex/Emby/Jellyfin/Kodi）
NFO_HEADER = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'


def _asset_dir(target_path: Path) -> Path:
    return target_path.parent


def _safe_name(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip().strip(".") or "unknown"


def build_lyrics_path(target_path: Path) -> Path:
    """歌词文件 = 歌曲同名 .lrc。"""
    return target_path.with_suffix(config.get("assets.lyrics_ext", ".lrc"))


def merge_bilingual(lyrics: str, trans: str) -> str:
    """合并双语歌词为带时间轴的双语 LRC。"""
    if not trans:
        return lyrics
    if not lyrics:
        return trans

    def parse(lrc: str) -> dict[str, str]:
        result = {}
        for line in lrc.splitlines():
            m = re.match(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)", line)
            if m:
                minute, sec, frac, text = m.groups()
                frac = (frac or "0").ljust(3, "0")[:3]
                ts = f"{int(minute):02d}:{int(sec):02d}.{frac}"
                result[ts] = text.strip()
        return result

    orig = parse(lyrics)
    tr = parse(trans)
    lines = []
    for ts in sorted(set(orig) | set(tr)):
        if ts in orig and ts in tr:
            lines.append(f"[{ts}]{orig[ts]}")
            lines.append(f"[{ts}]{tr[ts]}")
        elif ts in orig:
            lines.append(f"[{ts}]{orig[ts]}")
        else:
            lines.append(f"[{ts}]{tr[ts]}")
    return "\n".join(lines)


def _to_lrc_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}.00"


def write_lyrics(target_path: Path, meta: TrackMetadata) -> Optional[Path]:
    """写歌词，双语则合并。返回歌词文件路径。"""
    lyrics = meta.lyrics or ""
    trans = meta.translated_lyrics or ""
    if not lyrics and not trans:
        return None

    # 若是纯文本（无时间轴），转为简单 LRC
    content = lyrics
    if trans and config.get("scrape.bilingual_lyrics", True):
        content = merge_bilingual(lyrics, trans)

    if content and not re.search(r"\[\d{1,2}:\d{1,2}", content):
        # 纯文本 → 加时间轴（按行均匀分布）
        lines = [l for l in content.splitlines() if l.strip()]
        duration = meta.duration or len(lines) * 4
        step = duration / max(len(lines), 1)
        timed = []
        for i, line in enumerate(lines):
            timed.append(f"[{_to_lrc_time(i * step)}]{line}")
        content = "\n".join(timed)

    path = build_lyrics_path(target_path)
    try:
        path.write_text(content, encoding="utf-8")
        return path
    except Exception as e:  # noqa: BLE001
        logger.error(f"写歌词失败 {path}: {e}")
        return None


async def write_cover(target_path: Path, cover_url: str) -> Optional[Path]:
    """下载并保存封面 cover.jpg（同时生成 folder.jpg 别名）。"""
    if not cover_url:
        return None
    cover_name = config.get("assets.cover_name", "cover.jpg")
    path = _asset_dir(target_path) / cover_name
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            r = await client.get(cover_url)
            r.raise_for_status()
            data = r.content
        if len(data) < 1000:
            return None
        path.write_bytes(data)
        # 可选缩放，保证分辨率
        _maybe_resize(path)
        # 别名
        for alias in config.get("assets.cover_aliases", []):
            if alias and alias != cover_name:
                alias_path = _asset_dir(target_path) / alias
                if not alias_path.exists():
                    alias_path.write_bytes(path.read_bytes())
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"下载封面失败 {cover_url}: {e}")
        return None


def _maybe_resize(path: Path) -> None:
    """确保封面最小分辨率（放大/缩放到至少 min_size）。"""
    try:
        from PIL import Image
        min_size = config.get("assets.cover_min_size", 800)
        img = Image.open(path)
        w, h = img.size
        if w < min_size or h < min_size:
            scale = min_size / min(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            img.save(path)
    except Exception:  # noqa: BLE001
        pass


def _xml_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_album_nfo(target_path: Path, meta: TrackMetadata) -> Optional[Path]:
    if not config.get("assets.generate_nfo", True):
        return None
    name = config.get("assets.album_nfo_name", "album.nfo")
    path = _asset_dir(target_path) / name
    year = f"<year>{meta.year}</year>" if meta.year else ""
    genre = f"<genre>{_xml_escape(meta.genre)}</genre>" if meta.genre else ""
    content = (
        NFO_HEADER +
        "<album>\n"
        f"  <title>{_xml_escape(meta.album)}</title>\n"
        f"  <artist>{_xml_escape(meta.album_artist or meta.artist)}</artist>\n"
        f"  {year}\n"
        f"  {genre}\n"
        "</album>\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def write_artist_nfo(artist_dir: Path, artist: str, meta: TrackMetadata) -> Optional[Path]:
    if not config.get("assets.generate_nfo", True):
        return None
    name = config.get("assets.artist_nfo_name", "artist.nfo")
    path = artist_dir / name
    content = (
        NFO_HEADER +
        "<artist>\n"
        f"  <name>{_xml_escape(artist)}</name>\n"
        f"  <genre>{_xml_escape(meta.genre)}</genre>\n"
        "</artist>\n"
    )
    path.write_text(content, encoding="utf-8")
    return path
