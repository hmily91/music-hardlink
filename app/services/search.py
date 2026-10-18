"""音乐搜索 + 下载服务。

数据流：搜索（自定义中转音源 API / 网易云 fallback）→ 获取下载地址 → 下载到源目录。
下载完成后由硬链接同步模块自动建立硬链接，再由刮削模块外置刮削。

自定义音源中转 API（如 ikun 音源）接口契约：
  - 搜索: POST {API_URL}/music/search  body: {source, keyword, page, pageSize}
    返回: {code:200, data:[{id, mid, name, artist, album, albumid, albummid}]}
  - 取链接: POST {API_URL}/music/url  body: {source, musicId(mid), quality}
    返回: {code:200, url, quality, ...}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app.core.config import config
from app.core.logger import logger


@dataclass
class SongResult:
    """搜索结果条目。"""
    id: str = ""               # 数字 id
    mid: str = ""              # 歌曲 mid（取下载链接用）
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    source: str = ""           # 数据源: api / netease
    cover_url: str = ""
    music_info: dict = field(default_factory=dict)


def _api_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "lx-music-desktop/1.0.0",
    }
    key = config.get("scrape.api_key", "")
    if key:
        headers["X-Api-Key"] = key
    return headers


def _api_url() -> str:
    return config.get("scrape.api_url", "").rstrip("/")


async def _search_api(keyword: str, limit: int, source: str = "") -> list[SongResult]:
    """通过自定义中转音源 API 搜索。"""
    results: list[SongResult] = []
    url = _api_url()
    if not url:
        return results
    src = source or config.get("scrape.api_source", "tx")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.post(
                f"{url}/music/search",
                headers=_api_headers(),
                json={"source": src, "keyword": keyword, "page": 1, "pageSize": limit},
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 200:
                logger.warning(f"中转音源搜索失败: {data.get('message')}")
                return results
            for s in data.get("data", []):
                results.append(SongResult(
                    id=str(s.get("id", "")),
                    mid=s.get("mid", "") or "",
                    title=s.get("name", "") or s.get("title", ""),
                    artist=s.get("artist", "") or s.get("singer", ""),
                    album=s.get("album", "") or "",
                    source="api",
                    cover_url=s.get("pic", "") or s.get("cover", "") or "",
                    music_info={"songmid": s.get("mid", ""), "id": str(s.get("id", "")),
                                "name": s.get("name", ""), "singer": s.get("artist", ""),
                                "album": s.get("album", "")},
                ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"中转音源搜索异常: {e}")
    return results


async def _search_netease(keyword: str, limit: int) -> list[SongResult]:
    """网易云搜索（fallback）。"""
    results: list[SongResult] = []
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
                    id=sid, mid="", title=s.get("name", ""), artist=artists,
                    album=album, duration=(s.get("duration", 0) or 0) / 1000.0,
                    source="netease", cover_url=album_pic,
                    music_info={"id": sid, "name": s.get("name", ""),
                                "singer": artists, "album": album},
                ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"网易云搜索失败: {e}")
    return results


async def search_songs(keyword: str, limit: int = 20, source: str = "") -> list[SongResult]:
    """搜索歌曲：优先自定义中转音源，fallback 网易云。"""
    if not keyword.strip():
        return []
    # 1) 自定义中转音源 API
    results = await _search_api(keyword, limit, source)
    if results:
        return results
    # 2) fallback 网易云
    return await _search_netease(keyword, limit)


async def get_download_url(song: SongResult) -> str:
    """获取歌曲下载地址。优先中转音源 API，fallback 洛雪桥接/网易云。"""
    # 1) 自定义中转音源 API
    if song.source == "api" and song.mid:
        url = _api_url()
        if url:
            try:
                src = config.get("scrape.api_source", "tx")
                quality = config.get("scrape.api_quality", "128k")
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    r = await client.post(
                        f"{url}/music/url",
                        headers=_api_headers(),
                        json={"source": src, "musicId": song.mid, "quality": quality},
                    )
                    data = r.json()
                    if data.get("code") == 200 and data.get("url"):
                        return data["url"]
                    logger.warning(f"中转音源取链接失败: {data.get('message')}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"中转音源取链接异常: {e}")

    # 2) 洛雪音源桥接
    if config.get("scrape.lx_enabled", True):
        try:
            from app.services.providers.lx import LxSourceProvider
            lx = LxSourceProvider()
            sources = lx.available_sources()
            if sources:
                use = config.get("scrape.lx_source", "") or sources[0]
                dl = await lx.fetch_music_url(use, song.music_info)
                if dl:
                    return dl
        except Exception as e:  # noqa: BLE001
            logger.debug(f"洛雪音源 musicUrl 失败: {e}")

    # 3) 网易云直链（免费歌曲可用）
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
    if target.exists():
        logger.info(f"文件已存在，跳过下载: {target.name}")
        return str(target)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(headers=headers, timeout=120, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
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


async def search_and_download(keyword: str, song_id: str, source: str = "") -> dict:
    """搜索后按 song_id 下载单曲，返回结果描述。"""
    songs = await search_songs(keyword, source=source)
    song = next((s for s in songs if s.id == song_id), None)
    if not song:
        return {"success": False, "message": "未找到对应歌曲"}
    url = await get_download_url(song)
    if not url:
        return {"success": False, "message": "无法获取下载地址（可能需要音源 Key 或该歌曲需付费）"}
    path = await download_song(url, song)
    if not path:
        return {"success": False, "message": "下载失败"}
    return {"success": True, "message": f"已下载: {Path(path).name}", "path": path}
