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


def _lrc_lines(text: str) -> int:
    return len([l for l in (text or "").splitlines() if re.match(r"\s*\[\d{1,2}:\d{1,2}", l)])


def _is_better_lyrics(new: str, old: str) -> bool:
    """upgrade 策略下判断新歌词是否更完整（时间轴行数更多，或明显更长）。"""
    if not new.strip():
        return False
    if not old.strip():
        return True
    n, o = _lrc_lines(new), _lrc_lines(old)
    if n and o:
        return n > o
    return len(new) > len(old) * 1.2


def write_lyrics(target_path: Path, meta: TrackMetadata, force: bool = False) -> Optional[Path]:
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
    if not force and path.exists():
        pol = _policy("lyrics")
        if pol == "missingOnly":
            return path
        if pol == "upgrade":
            try:
                if not _is_better_lyrics(content, path.read_text(encoding="utf-8", errors="ignore")):
                    logger.debug(f"现有歌词更完整，跳过覆盖: {path.name}")
                    return path
            except Exception:  # noqa: BLE001
                pass
    try:
        path.write_text(content, encoding="utf-8")
        return path
    except Exception as e:  # noqa: BLE001
        logger.error(f"写歌词失败 {path}: {e}")
        return None


def _policy(kind: str) -> str:
    """读取某类资源的写盘策略。"""
    return str(config.get(f"scrape.write_policy.{kind}", "missingOnly") or "missingOnly").lower()


def find_local_cover(target_path: Path) -> Optional[Path]:
    """在曲目同目录查找已存在的封面（按 cover_file_patterns 顺序匹配）。"""
    if not config.get("assets.local_cover_enable", True):
        return None
    d = _asset_dir(target_path)
    for pat in config.get("assets.cover_file_patterns", []) or []:
        hits = sorted(d.glob(pat)) if "*" in pat else ([d / pat] if (d / pat).exists() else [])
        for p in hits:
            if p.is_file() and p.stat().st_size > 1000:
                return p
    return None


def _apply_cover_proxy(url: str) -> str:
    """替换为封面图床代理（解决 coverartarchive.org 访问不畅）。"""
    proxy = str(config.get("assets.cover_proxy", "") or "").strip().rstrip("/")
    if proxy and "coverartarchive.org" in url:
        return url.replace("https://coverartarchive.org", proxy)
    return url


def _cover_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def _need_write(path: Path, kind: str, force: bool = False) -> bool:
    """按写盘策略判断是否该写入/覆盖。"""
    if force:
        return True
    pol = _policy(kind)
    if pol == "always":
        return True
    if not path.exists() or path.stat().st_size == 0:
        return True
    if pol == "missingOnly":
        return False
    # upgrade：仅在现有资源不达标时覆盖
    if kind == "cover":
        w, h = _cover_size(path)
        return 0 < min(w, h) < int(config.get("assets.cover_min_size", 800))
    return False


def extract_embedded_cover(audio_path: Path, dest_dir: Path) -> Optional[Path]:
    """从音频文件内嵌图片提取封面（只读源文件，绝不写回）。"""
    try:
        from mutagen import File as MutagenFile
        f = MutagenFile(str(audio_path))
        if f is None:
            return None
        data = None
        tags = getattr(f, "tags", None)
        if tags is not None and hasattr(tags, "getall"):
            pics = tags.getall("APIC")
            if pics:
                data = pics[0].data
        if data is None:
            for attr in ("pictures", "cover"):
                pics = getattr(f, attr, None)
                if pics:
                    data = pics[0].data if hasattr(pics[0], "data") else pics[0]
                    break
        if not data or len(data) < 1000:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / config.get("assets.cover_name", "cover.jpg")
        out.write_bytes(data)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"提取内嵌封面失败 {audio_path.name}: {e}")
        return None


async def write_cover(target_path: Path, cover_url: str, force: bool = False) -> Optional[Path]:
    """下载并保存封面 cover.jpg（同时生成 folder.jpg 别名）。

    写入前先按策略判断是否需要覆盖；本地已有封面则直接复用。
    """
    if not cover_url:
        return None
    cover_name = config.get("assets.cover_name", "cover.jpg")
    path = _asset_dir(target_path) / cover_name

    # 本地已有封面 → 直接复用，不再下载
    local = find_local_cover(target_path)
    if local is not None:
        logger.debug(f"复用本地封面: {local.name}")
        return local

    if not _need_write(path, "cover", force):
        return path
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            r = await client.get(_apply_cover_proxy(cover_url))
            r.raise_for_status()
            data = r.content
        if len(data) < 1000:
            raise ValueError("封面数据过小")
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
        # 回退：从音频内嵌图片提取
        if config.get("assets.embedded_cover_fallback", True):
            emb = extract_embedded_cover(target_path, _asset_dir(target_path))
            if emb:
                logger.info(f"使用内嵌封面回退: {target_path.name}")
                return emb
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


def write_album_nfo(target_path: Path, meta: TrackMetadata, force: bool = False) -> Optional[Path]:
    if not config.get("assets.generate_nfo", True):
        return None
    name = config.get("assets.album_nfo_name", "album.nfo")
    path = _asset_dir(target_path) / name
    if path.exists() and not force and _policy("nfo") == "missingOnly":
        return path
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


def write_artist_nfo(artist_dir: Path, artist: str, meta: TrackMetadata, force: bool = False) -> Optional[Path]:
    if not config.get("assets.generate_nfo", True):
        return None
    name = config.get("assets.artist_nfo_name", "artist.nfo")
    path = artist_dir / name
    if path.exists() and not force and _policy("nfo") == "missingOnly":
        return path
    content = (
        NFO_HEADER +
        "<artist>\n"
        f"  <name>{_xml_escape(artist)}</name>\n"
        f"  <genre>{_xml_escape(meta.genre)}</genre>\n"
        "</artist>\n"
    )
    path.write_text(content, encoding="utf-8")
    return path
