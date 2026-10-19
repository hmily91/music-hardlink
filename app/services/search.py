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
    source: str = ""           # 平台代号: wy / tx / kg / kw / api / netease
    cover_url: str = ""
    music_info: dict = field(default_factory=dict)
    qualitys: list = field(default_factory=list)   # 真实可得音质档位（高->低）


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
    """搜索歌曲。

    顺序（可通过 `scrape.search_mode` 调整）：
      1. 官方平台直连搜索（wy / tx / kg / kw），带 media_mid 等取链关键字段
      2. 自定义中转音源 API（如 ikun）
      3. 网易云 fallback

    `source` 可指定平台代号；留空则用配置 `scrape.search_platform`，
    再留空则按 `scrape.search_platforms` 依次尝试。
    """
    if not keyword.strip():
        return []

    mode = config.get("scrape.search_mode", "auto")

    # 1) 官方平台直连
    if mode in ("auto", "official"):
        try:
            from app.services import platforms as pf

            if source and source in pf.CHANNEL_FACTORIES:
                plats = [source]
            elif source and source in ("wy", "tx", "kg", "kw"):
                plats = [source]
            else:
                plats = config.get("scrape.search_platforms", []) or []
                single = config.get("scrape.search_platform", "")
                if single:
                    plats = [single]
                elif not plats:
                    plats = ["tx", "wy", "kg", "kw"]
            tracks: list = []
            for p in plats:
                if p not in pf.CHANNEL_FACTORIES:
                    continue
                tracks = await pf.search_platform(p, keyword, 1)
                if tracks:
                    break
            if tracks:
                return [t.as_song_result() for t in tracks[:limit]]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"官方平台搜索失败，回落中转音源: {e}")

    if mode == "official":
        return []

    # 2) 自定义中转音源 API
    results = await _search_api(keyword, limit, source)
    if results:
        return results
    # 3) 网易云 fallback
    return await _search_netease(keyword, limit)


def _quality_attempts(song: SongResult, preferred: str = "") -> list[str]:
    """生成音质尝试顺序：从高到低，优先用户指定档位。

    档位来源是搜索阶段后置增强给出的**真实可得档位**；
    拿不到时退化为从高到低全试（音源不支持的档会自行失败）。
    """
    ladder = ["flac24bit", "flac", "320k", "192k", "128k"]
    avail = [q for q in song.qualitys if q in ladder] or list(song.qualitys or [])
    ordered = [q for q in ladder if q in avail] or (avail or list(ladder))
    pref = preferred or config.get("scrape.download_quality", "highest")
    if pref and pref != "highest":
        return [pref] + [q for q in ordered if q != pref]
    return ordered


async def get_download_url(song: SongResult, quality: str = "") -> str:
    """获取歌曲下载地址。

    1. 洛雪音源桥接：按音质阶梯从高到低尝试，由桥接内部轮询全部音源脚本
    2. 中转音源 API（ikun 等）
    3. 网易云直链兜底
    """
    attempts = _quality_attempts(song, quality)

    # 1) 洛雪音源桥接（平台代号 + 音质阶梯）
    if config.get("scrape.lx_enabled", True) and song.source in ("wy", "tx", "kg", "kw", "mg"):
        try:
            from app.services.providers.lx import LxSourceProvider

            lx = LxSourceProvider()
            for q in attempts:
                dl = await lx.fetch_music_url(song.source, song.music_info, quality=q)
                if dl:
                    logger.info(f"取链成功: {song.title} @ {q}")
                    return dl
            logger.warning(f"洛雪音源未能取得 {song.title} 的直链（已试 {attempts}）")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"洛雪音源取链异常: {e}")

    # 2) 自定义中转音源 API
    key = song.music_info.get("songmid") or song.mid
    if key:
        url = _api_url()
        if url:
            try:
                src = config.get("scrape.api_source", "tx")
                for q in attempts[:1] or ["flac"]:
                    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                        r = await client.post(
                            f"{url}/music/url",
                            headers=_api_headers(),
                            json={"source": src, "musicId": key, "quality": q},
                        )
                        data = r.json()
                        if data.get("code") == 200 and data.get("url"):
                            return data["url"]
                        logger.warning(f"中转音源取链接失败: {data.get('message')}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"中转音源取链接异常: {e}")

    # 3) 网易云直链兜底（免费歌曲可用）
    if song.source in ("wy", "netease") and song.id:
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
