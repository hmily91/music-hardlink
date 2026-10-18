"""音频文件元数据读取工具。

原则：只读，绝不写入音频文件。优先使用 ffprobe，降级到 mutagen。
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logger import logger


@dataclass
class AudioInfo:
    """音频文件只读元信息。"""
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track: int = 0
    disc: int = 0
    year: Optional[int] = None
    genre: str = ""
    duration: float = 0.0
    format: str = ""

    @property
    def effective_artist(self) -> str:
        return self.album_artist or self.artist or "未知歌手"

    @property
    def effective_title(self) -> str:
        return self.title or self.path.stem


def _parse_filename_fallback(path: Path, info: AudioInfo) -> AudioInfo:
    """无标签时从文件名解析 'NN - Title' 或 'NN.Title' 格式。"""
    if info.title or info.track:
        return info
    stem = path.stem
    import re
    m = re.match(r"^\s*(\d{1,3})\s*[-.]\s*(.+)$", stem)
    if m:
        info.track = int(m.group(1))
        info.title = m.group(2).strip()
    else:
        info.title = stem
    return info


@dataclass
class TrackMetadata:
    """刮削到的曲目元数据。"""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: Optional[int] = None
    genre: str = ""
    duration: float = 0.0
    cover_url: str = ""
    lyrics: str = ""              # 歌词文本（可能是带时间轴或纯文本）
    translated_lyrics: str = ""   # 翻译歌词
    provider: str = ""
    match_score: float = 0.0


def _has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def _probe_with_ffprobe(path: Path) -> AudioInfo:
    info = AudioInfo(path=path)
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip())
        import json
        data = json.loads(out.stdout)
        fmt = data.get("format", {})
        tags = fmt.get("tags", {})
        info.title = tags.get("title", "")
        info.artist = tags.get("artist", "")
        info.album = tags.get("album", "")
        info.album_artist = tags.get("album_artist", "")
        info.track = int(tags.get("track", "0").split("/")[0] or 0)
        info.disc = int(tags.get("disc", "0").split("/")[0] or 0)
        year = tags.get("date", "") or tags.get("year", "")
        try:
            info.year = int(str(year)[:4])
        except (ValueError, TypeError):
            info.year = None
        info.genre = tags.get("genre", "")
        info.format = fmt.get("format_name", "")
        info.duration = float(fmt.get("duration", 0) or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"ffprobe 读取失败 {path.name}: {e}")
    return info


def _probe_with_mutagen(path: Path) -> AudioInfo:
    info = AudioInfo(path=path)
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(path))
        if mf is None:
            return info
        info.duration = float(getattr(mf.info, "length", 0) or 0)
        tags = getattr(mf, "tags", None)
        if tags:
            def _first(tag):
                v = tags.get(tag)
                if isinstance(v, list) and v:
                    return str(v[0])
                return str(v) if v else ""
            info.title = _first("title")
            info.artist = _first("artist")
            info.album = _first("album")
            info.album_artist = _first("albumartist") or _first("album_artist")
            try:
                info.track = int(str(_first("tracknumber")).split("/")[0] or 0)
            except ValueError:
                info.track = 0
            try:
                info.year = int(str(_first("date"))[:4])
            except (ValueError, TypeError):
                info.year = None
            info.genre = _first("genre")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mutagen 读取失败 {path.name}: {e}")
    return info


def read_audio_info(path: Path) -> AudioInfo:
    """读取音频元数据（只读）。"""
    if _has_ffprobe():
        info = _probe_with_ffprobe(path)
        if info.title or info.duration:
            return _parse_filename_fallback(path, info)
    info = _probe_with_mutagen(path)
    return _parse_filename_fallback(path, info)


def read_duration(path: Path) -> float:
    """仅读取时长（快速路径）。"""
    info = read_audio_info(path)
    return info.duration
