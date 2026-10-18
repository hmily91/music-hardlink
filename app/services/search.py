"""音乐搜索 + 下载服务。

数据流：搜索（内置源）→ 获取下载地址（洛雪音源 musicUrl / 网易云直链）→ 下载到源目录。
下载完成后由硬链接同步模块自动建立硬链接，再由刮削模块外置刮削。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app.core.config import config
from app.core.logger import logger


@dataclass
class SongResult:
    """搜索结果条目。"""
    id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    source: str = ""            # 数据源: netease / qq / ...
    cover_url: str = ""
    music_info: dict = field(default_factory=dict)  # 传给洛雪音源 musicUrl 的原始信息


def _fmt_duration(ms: float) -> str:
    if not ms:
        return ""
    s = int(ms)
    return f"{s // 60:02d}:{s % 60:02d}"


async def search_songs(keyword: str, limit: int = 20) -> list[SongResult]:
    """搜索歌曲（当前用网易云公开接口，后续可扩展多源）。"""
    results: list[SongResult] = []
    if not keyword.strip():
        return results

    # 网易云搜索
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://music.163.com",
        }
        cookie = config.get("scrape.netease_cookie", "")
        if cookie:
            headers["Cookie"] = cookie
        async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
            r = await client.get(
                "https://music.163.com/api/search/get/web",
                params={"s": keyword, "type": 1, "offset": 0, "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
            songs = (data.get("result") or {}).get("songs", [])
            for s in songs:
                artists = "/".join(a.get("name", "") for a in s.get("artists", []))
                album = (s.get("album") or {}).get("name", "")
                album_pic = (s.get("album") or {}).get("picUrl", "")
                sid = str(s.get("id", ""))
                results.append(SongResult(
                    id=sid,
                    title=s.get("name", ""),
                    artist=artists,
                    album=album,
                    duration=(s.get("duration", 0) or 0) / 1000.0,
                    source="netease",
                    cover_url=album_pic,
                    music_info={"id": sid, "name": s.get("name", ""), "singer": artists, "album": album},
                ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"网易云搜索失败: {e}")

    return results


async def get_download_url(song: SongResult) -> str:
    """获取歌曲下载地址。优先洛雪音源 musicUrl，fallback 网易云直链。"""
    # 1) 洛雪音源桥接
    if config.get("scrape.lx_enabled", True):
        try:
            from app.services.providers.lx import LxSourceProvider
            lx = LxSourceProvider()
            sources = lx.available_sources()
            if sources:
                use = config.get("scrape.lx_source", "") or sources[0]
                url = await lx.fetch_music_url(use, song.music_info)
                if url:
                    return url
        except Exception as e:  # noqa: BLE001
            logger.debug(f"洛雪音源 musicUrl 失败: {e}")

    # 2) 网易云直链（免费歌曲可用）
    if song.source == "netease" and song.id:
        return f"https://music.163.com/song/media/outer/url?id={song.id}.mp3"

    return ""


def _safe_filename(title: str, artist: str, ext: str = ".mp3") -> str:
    name = f"{artist} - {title}" if artist else title
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip().strip(".")
    return (name or "unknown") + ext


async def download_song(url: str, song: SongResult) -> Optional[str]:
    """下载歌曲到源目录（/source），返回源目录下的文件绝对路径。"""
    if not url:
        return None
    source_dir = config.source_dirs[0] if config.source_dirs else ""
    if not source_dir:
        logger.error("未配置源目录，无法下载")
        return None

    filename = _safe_filename(song.title, song.artist)
    target = Path(source_dir) / filename
    # 避免覆盖已存在文件
    if target.exists():
        logger.info(f"文件已存在，跳过下载: {target.name}")
        return str(target)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
        # 校验是不是真的音频（避免下到 HTML 错误页）
        if len(data) < 1000 or data[:4] in (b"<htm", b"<HTM", b"<!DO"):
            logger.warning(f"下载内容异常（可能非音频）: {url}")
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        logger.info(f"下载完成: {target}")
        return str(target)
    except Exception as e:  # noqa: BLE001
        logger.error(f"下载失败 {url}: {e}")
        return None


async def search_and_download(keyword: str, song_id: str) -> dict:
    """搜索后按 song_id 下载单曲，返回结果描述。"""
    songs = await search_songs(keyword)
    song = next((s for s in songs if s.id == song_id), None)
    if not song:
        return {"success": False, "message": "未找到对应歌曲"}
    url = await get_download_url(song)
    if not url:
        return {"success": False, "message": "无法获取下载地址（可能需要音源或该歌曲需付费）"}
    path = await download_song(url, song)
    if not path:
        return {"success": False, "message": "下载失败"}
    return {"success": True, "message": f"已下载: {Path(path).name}", "path": path}
