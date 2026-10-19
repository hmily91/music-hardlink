"""官方平台直连搜索：通道矩阵 + 业务态降级 + 后置增强。

设计要点（参考 miyin 的实现思路）：

1. **搜索不走洛雪音源脚本**。洛雪音源脚本只负责 `musicUrl` 取直链，
   搜索由服务端直连各平台官方接口完成 —— 音源脚本本身不提供搜索能力。
2. **每个平台配置多优先级通道**（主通道 / 备用 / 末级），任一通道失败自动降级。
   失败判定用「业务态探测」而非仅看 HTTP 200：很多上游接口出错时也返回 200，
   只在响应体里带 `code` / `error_code` / `status`。
3. **后置增强 enrich**（本模块的关键价值）：
   - QQ 新端点 `search_for_qq_cp` 不返回 `strMediaMid`，而 `media_mid` 是
     洛雪音源取无损（flac / Hi-Res）的必需参数 —— 缺了它音源只能给 320k。
     解决办法：`u.y.qq.com/cgi-bin/musicu.fcg` 的
     `music.trackInfo.UniformRuleCtrl / CgiGetTrackInfo` 支持一次批量查询，
     同时返回 `file.size_flac` / `size_hires` 等字段，可反推**真实可得档位**。
   - 网易云备用通道的 `album` 只有 `picId` 没有 `picUrl`，封面缺失时批量补回。
"""

from __future__ import annotations

import ast
import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from app.core.logger import logger

# 音质从高到低的降级阶梯（与洛雪音质档位对齐）
QUALITY_LADDER = ["flac24bit", "flac", "320k", "192k", "128k"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PAGE_SIZE = 30

#: 平台代号 -> 展示名
PLATFORM_LABELS = {
    "wy": "网易云音乐",
    "tx": "QQ音乐",
    "kg": "酷狗音乐",
    "kw": "酷我音乐",
}


@dataclass
class SearchTrack:
    """统一搜索结果条目（与平台无关）。"""

    platform: str = ""
    external_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_id: str = ""
    duration: int = 0            # 秒
    cover: str = ""
    qualitys: list[str] = field(default_factory=list)
    music_info: dict = field(default_factory=dict)  # 交给洛雪音源取链用

    def as_song_result(self):
        """转换为下载模块使用的 SongResult。"""
        from app.services.search import SongResult

        return SongResult(
            id=self.music_info.get("songid", "") or self.external_id,
            mid=self.music_info.get("songmid", "") or self.external_id,
            title=self.title,
            artist=self.artist,
            album=self.album,
            duration=float(self.duration),
            source=self.platform,
            cover_url=self.cover,
            music_info=self.music_info,
            qualitys=list(self.qualitys),
        )


def _fmt_interval(sec: int) -> str:
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def _clean_artist(raw: str) -> str:
    """清洗脏歌手字段：如「周杰伦- / A-LNK」→「周杰伦」。"""
    import re

    s = str(raw or "").strip()
    if not s:
        return "未知"
    s = re.sub(r"\s*-\s*/\s*.+$", "", s)
    s = re.sub(r"[\s\-/]+$", "", s)
    s = re.sub(r"\s*/\s*", " / ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or "未知"


def _artists_join(list_obj: Any, key: str = "name") -> str:
    if not list_obj:
        return "未知"
    if isinstance(list_obj, str):
        return _clean_artist(list_obj)
    if isinstance(list_obj, list):
        return _clean_artist("/".join(str(a.get(key, "") or a) for a in list_obj if a) or "未知")
    return _clean_artist(str(list_obj))


# --------------------------------------------------------------------------- #
# 各平台结果映射
# --------------------------------------------------------------------------- #

def map_wy_song(s: dict) -> SearchTrack:
    """网易云。兼容 cloudsearch/pc（ar / al）与 search/get/web（artists / album）。"""
    sid = str(s.get("id", ""))
    album = s.get("al") or s.get("album") or {}
    artist = _artists_join(s.get("ar") or s.get("artists"))
    cover = album.get("picUrl") or ""
    return SearchTrack(
        platform="wy",
        external_id=sid,
        title=s.get("name", "") or "未知",
        artist=artist,
        album=album.get("name", "") or "",
        album_id=str(album.get("id", "")) if album.get("id") else "",
        duration=round((s.get("dt") or s.get("duration") or 0) / 1000),
        cover=cover,
        qualitys=["128k", "320k", "flac"],
        music_info={
            "name": s.get("name", ""),
            "singer": artist,
            "albumName": album.get("name", "") or "",
            "songmid": sid,
            "id": sid,
            "hash": sid,
            "source": "wy",
            "img": cover,
            "interval": _fmt_interval(round((s.get("dt") or s.get("duration") or 0) / 1000)),
        },
    )


def map_tx_song(s: dict) -> SearchTrack:
    """QQ 音乐。cover 由 albummid 拼出；songmid 为取链主键。"""
    mid = str(s.get("songmid") or s.get("mid") or "")
    albummid = s.get("albummid") or ""
    cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{albummid}.jpg" if albummid else ""
    artist = _artists_join(s.get("singer"))
    duration = int(s.get("interval") or 0)
    return SearchTrack(
        platform="tx",
        external_id=mid,
        title=s.get("songname") or s.get("name") or "未知",
        artist=artist,
        album=s.get("albumname") or "",
        album_id=str(albummid),
        duration=duration,
        cover=cover,
        qualitys=["128k", "320k"],
        music_info={
            "name": s.get("songname") or s.get("name") or "",
            "singer": artist,
            "albumName": s.get("albumname") or "",
            "songmid": mid,
            "id": mid,
            "hash": mid,
            "songid": str(s.get("songid") or s.get("id") or ""),
            # 新端点不带 strMediaMid，由 enrich_tx_tracks 通过 CgiGetTrackInfo 补齐
            "strMediaMid": s.get("strMediaMid") or s.get("media_mid") or "",
            "source": "tx",
            "img": cover,
            "interval": _fmt_interval(duration),
        },
    )


def map_kg_song(s: dict) -> SearchTrack:
    """酷狗 v3 小驼峰结构。封面藏在 trans_param.union_cover，需替换 {size}。"""
    h = str(s.get("hash") or s.get("sqhash") or s.get("320hash") or "")
    raw = ((s.get("trans_param") or {}).get("union_cover")) or s.get("Image") or ""
    cover = ""
    if isinstance(raw, str) and raw:
        cover = raw.replace("{size}", "240") if "{size}" in raw else raw
    title = s.get("songname") or s.get("songname_original") or "未知"
    artist = s.get("singername") or "未知"
    duration = int(s.get("duration") or 0)
    return SearchTrack(
        platform="kg",
        external_id=h,
        title=title,
        artist=artist,
        album=s.get("album_name") or "",
        album_id=str(s.get("album_id") or ""),
        duration=duration,
        cover=cover,
        qualitys=["128k", "320k"],
        music_info={
            "name": title,
            "singer": artist,
            "albumName": s.get("album_name") or "",
            "songmid": h,
            "id": h,
            "hash": h,
            "source": "kg",
            "img": cover,
            "interval": _fmt_interval(duration),
        },
    )


def map_kw_song(s: dict) -> SearchTrack:
    """酷我 search.kuwo.cn/r.s（伪 JSON，字段为 URL 编码）。"""
    from urllib.parse import unquote

    def dec(v: Any) -> str:
        if v is None:
            return ""
        try:
            s = unquote(str(v).replace("+", "%20"))
        except Exception:  # noqa: BLE001
            s = str(v)
        # 酷我字段里混有 HTML 实体（&nbsp;）与字面量转义（\u0026，可能是双反斜杠）
        s = html.unescape(s)
        s = re.sub(r"\\+u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
        # 非断空格 -> 普通空格；清理孤立反斜杠
        s = s.replace("\xa0", " ").replace("\u3000", " ")
        s = re.sub(r"\\(?![\\a-zA-Z])", "", s)
        return re.sub(r"\s{2,}", " ", s).strip()

    sid = str(s.get("MUSICRID") or s.get("DC_TARGETID") or "").replace("MUSIC_", "")
    pic = s.get("web_albumpic_short") or ""
    cover = f"https://img2.kuwo.cn/star/albumcover/{pic}" if pic else ""
    duration = int(s.get("DURATION") or 0)
    return SearchTrack(
        platform="kw",
        external_id=sid,
        title=dec(s.get("NAME") or s.get("SONGNAME")),
        artist=dec(s.get("ARTIST")),
        album=dec(s.get("ALBUM")),
        album_id=str(s.get("ALBUMID") or "").replace("ALBUM_", ""),
        duration=duration,
        cover=cover,
        qualitys=["128k", "320k"],
        music_info={
            "name": dec(s.get("NAME") or s.get("SONGNAME")),
            "singer": dec(s.get("ARTIST")),
            "albumName": dec(s.get("ALBUM")),
            "songmid": sid,
            "id": sid,
            "hash": sid,
            "source": "kw",
            "img": cover,
            "interval": _fmt_interval(duration),
        },
    )


# --------------------------------------------------------------------------- #
# 通道定义
# --------------------------------------------------------------------------- #

@dataclass
class Channel:
    id: str
    platform: str
    priority: int
    build_url: Callable[[str, int], str]
    parse: Callable[[dict], list[SearchTrack]]
    headers: dict = field(default_factory=dict)
    timeout: float = 10.0
    ok_statuses: Optional[list[int]] = None      # 业务态 status 白名单
    detect_error: Optional[Callable[[dict], Optional[str]]] = None


def _detect_code_200(d: Any) -> Optional[str]:
    """网易云：code 必须为 200。"""
    if not isinstance(d, dict):
        return "响应不是对象"
    code = d.get("code")
    if code is not None and code != 200:
        return f"code {code}"
    return None


def _detect_qq(d: Any) -> Optional[str]:
    if not isinstance(d, dict):
        return "响应不是对象"
    code = d.get("code")
    if code is not None and code != 0:
        return f"code {code}"
    return None


def _detect_kg(d: Any, ok: list[int]) -> Optional[str]:
    if not isinstance(d, dict):
        return "响应不是对象"
    if d.get("error_code") and str(d["error_code"]) != "0":
        return f"{d.get('error_msg') or d.get('error') or 'upstream error'} (error_code={d['error_code']})"
    if d.get("errcode") and str(d["errcode"]) != "0":
        return f"{d.get('error') or 'upstream error'} (errcode={d['errcode']})"
    status = d.get("status")
    if status is not None and int(status) not in ok:
        return f"upstream status={status}"
    return None


def _detect_kw(d: Any) -> Optional[str]:
    """酷我：HTTP 200 + HIT:'0' 是合法的「没搜到」，不能判为错误。"""
    if not isinstance(d, dict):
        return "响应不是对象"
    if d.get("success") is False:
        return str(d.get("message") or d.get("msg") or "request rejected")
    if d.get("error_code") and str(d["error_code"]) != "0":
        return f"{d.get('error_msg') or 'upstream error'}"
    code = d.get("code")
    if code is not None and int(code) >= 400:
        return f"code {code}"
    return None


def wy_channels() -> list[Channel]:
    def build(kw: str, page: int) -> str:
        offset = (page - 1) * PAGE_SIZE
        return (
            "https://music.163.com/api/cloudsearch/pc"
            f"?s={quote(kw)}&type=1&limit={PAGE_SIZE}&offset={offset}"
        )

    def build_alt(kw: str, page: int) -> str:
        offset = (page - 1) * PAGE_SIZE
        return (
            "https://music.163.com/api/search/get/web"
            f"?s={quote(kw)}&type=1&limit={PAGE_SIZE}&offset={offset}"
        )

    return [
        Channel(
            id="wy:cloudsearch-pc", platform="wy", priority=1,
            build_url=build,
            headers={"Referer": "https://music.163.com/"},
            detect_error=_detect_code_200,
            parse=lambda d: [map_wy_song(s) for s in (d.get("result") or {}).get("songs", [])],
        ),
        Channel(
            id="wy:search-get-web", platform="wy", priority=2,
            build_url=build_alt,
            headers={"Referer": "https://music.163.com/"},
            detect_error=_detect_code_200,
            parse=lambda d: [map_wy_song(s) for s in (d.get("result") or {}).get("songs", [])],
        ),
    ]


def tx_channels() -> list[Channel]:
    qh = {"Referer": "https://y.qq.com/"}

    def build_v2(kw: str, page: int) -> str:
        return (
            "https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp"
            f"?w={quote(kw)}&p={page}&n={PAGE_SIZE}&format=json"
        )

    def build_legacy(kw: str, page: int) -> str:
        return (
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
            f"?w={quote(kw)}&p={page}&n={PAGE_SIZE}&format=json"
        )

    def parse(d: dict) -> list[SearchTrack]:
        return [
            t for t in (map_tx_song(s) for s in ((d.get("data") or {}).get("song") or {}).get("list", []))
            if t.external_id
        ]

    return [
        Channel(id="tx:soso-v2", platform="tx", priority=1, build_url=build_v2,
                headers=qh, detect_error=_detect_qq, parse=parse),
        # 老端点明显更慢，仅作备用
        Channel(id="tx:soso-cp", platform="tx", priority=2, build_url=build_legacy,
                headers=qh, detect_error=_detect_qq, parse=parse, timeout=15.0),
    ]


def kg_channels() -> list[Channel]:
    kh = {"Referer": "https://www.kugou.com/"}

    def make(cid: str, origin: str, prio: int, extra: str) -> Channel:
        def build(kw: str, page: int) -> str:
            return (
                f"{origin}/api/v3/search/song?format=json&keyword={quote(kw)}"
                f"&page={page}&pagesize={PAGE_SIZE}&showtype=1{extra}"
            )

        return Channel(
            id=cid, platform="kg", priority=prio, build_url=build, headers=kh,
            detect_error=lambda d: _detect_kg(d, [1]),
            parse=lambda d: [
                t for t in (map_kg_song(s) for s in (d.get("data") or {}).get("info", []))
                if t.external_id
            ],
        )

    return [
        make("kg:mobileservice-v3", "https://mobileservice.kugou.com", 1, ""),
        make("kg:mobilecdn-v3", "http://mobilecdn.kugou.com", 2, "&version=9108"),
    ]


def kw_channels() -> list[Channel]:
    def build(kw: str, page: int) -> str:
        return (
            f"https://search.kuwo.cn/r.s?all={quote(kw)}&ft=music&client=kt"
            f"&pn={page - 1}&rn={PAGE_SIZE}&rformat=json&encoding=utf8"
        )

    return [
        Channel(
            id="kw:r.s", platform="kw", priority=1, build_url=build,
            detect_error=_detect_kw,
            parse=lambda d: [t for t in (map_kw_song(s) for s in (d.get("abslist") or [])) if t.external_id],
        ),
    ]


CHANNEL_FACTORIES: dict[str, Callable[[], list[Channel]]] = {
    "wy": wy_channels,
    "tx": tx_channels,
    "kg": kg_channels,
    "kw": kw_channels,
}


def list_platforms() -> list[dict]:
    return [
        {"id": k, "label": PLATFORM_LABELS.get(k, k), "channels": len(f())}
        for k, f in CHANNEL_FACTORIES.items()
    ]


# --------------------------------------------------------------------------- #
# 后置增强
# --------------------------------------------------------------------------- #

def _chunk(arr: list, size: int):
    for i in range(0, len(arr), size):
        yield arr[i:i + size]


def tx_qualitys_from_file(file: dict) -> list[str]:
    """由 file.size_* 反推真实可得档位。"""
    if not isinstance(file, dict):
        return ["128k", "320k"]
    out: list[str] = []

    def n(k: str) -> int:
        try:
            return int(file.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    if n("size_128mp3") > 0:
        out.append("128k")
    if n("size_320mp3") > 0:
        out.append("320k")
    if n("size_flac") > 0 or n("size_ape") > 0:
        out.append("flac")
    if n("size_hires") > 0:
        out.append("flac24bit")
    return out or ["128k", "320k"]


async def fetch_tx_track_info(mids: list[str]) -> dict[str, dict]:
    """批量拉取 songmid 的 file 信息（含 media_mid 与各档位体积）。

    单次最多 50 首；整页一次请求即可，实测远快于逐个查询。
    """
    result: dict[str, dict] = {}
    if not mids:
        return result
    for part in _chunk(mids, 50):
        payload = {
            "comm": {"ct": 24, "cv": 0},
            "req_1": {
                "module": "music.trackInfo.UniformRuleCtrl",
                "method": "CgiGetTrackInfo",
                "param": {"ids": [0] * len(part), "types": [0] * len(part), "mids": part},
            },
        }
        url = "https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&data=" + quote(json.dumps(payload))
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": DEFAULT_UA, "Referer": "https://y.qq.com/"})
                r.raise_for_status()
                data = r.json()
            if (data.get("req_1") or {}).get("code") not in (0, None):
                logger.warning(f"CgiGetTrackInfo 返回 code={data['req_1'].get('code')}")
                continue
            for t in ((data.get("req_1") or {}).get("data") or {}).get("tracks", []):
                if t.get("mid"):
                    result[str(t["mid"])] = t
        except Exception as e:  # noqa: BLE001
            logger.warning(f"CgiGetTrackInfo 失败（降级为不增强）: {e}")
    return result


async def enrich_tx_tracks(items: list[SearchTrack]) -> list[SearchTrack]:
    """补 media_mid（无损取链必需）与真实音质档位。查不到的条目原样保留。"""
    mids = [t.external_id for t in items if t.external_id]
    if not mids:
        return items
    info = await fetch_tx_track_info(mids)
    if not info:
        return items
    for t in items:
        track = info.get(t.external_id)
        file = (track or {}).get("file") or {}
        media_mid = file.get("media_mid")
        if media_mid:
            t.music_info["strMediaMid"] = media_mid
            t.music_info["media_mid"] = media_mid
        if file:
            t.qualitys = tx_qualitys_from_file(file)
            t.music_info["qualitys"] = t.qualitys
            if int(file.get("size_flac") or 0) > 0 or int(file.get("size_hires") or 0) > 0:
                t.music_info["hasLossless"] = True
    return items


async def enrich_wy_tracks(items: list[SearchTrack]) -> list[SearchTrack]:
    """仅在封面缺失时批量补封面（走备用通道才会触发）。"""
    missing = [t for t in items if not t.cover and t.external_id]
    if not missing:
        return items
    covers: dict[str, str] = {}
    for part in _chunk([t.external_id for t in missing], 50):
        nums = [int(x) for x in part if x.isdigit()]
        if not nums:
            continue
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                r = await client.get(
                    "https://music.163.com/api/song/detail",
                    params={"ids": str(nums).replace("'", '"')},
                    headers={"User-Agent": DEFAULT_UA, "Referer": "https://music.163.com/"},
                )
                r.raise_for_status()
                data = r.json()
            for s in data.get("songs", []):
                pic = (s.get("album") or {}).get("picUrl") or (s.get("al") or {}).get("picUrl")
                if s.get("id") and pic:
                    covers[str(s["id"])] = pic
        except Exception as e:  # noqa: BLE001
            logger.warning(f"网易云批量补封面失败: {e}")
    for t in missing:
        pic = covers.get(t.external_id)
        if pic:
            t.cover = pic
            t.music_info["img"] = pic
    return items


ENRICHERS: dict[str, Callable[[list[SearchTrack]], Any]] = {
    "tx": enrich_tx_tracks,
    "wy": enrich_wy_tracks,
}


# --------------------------------------------------------------------------- #
# 搜索入口
# --------------------------------------------------------------------------- #

_search_cache: dict[str, tuple[float, list[SearchTrack]]] = {}
CACHE_TTL = 60.0
CACHE_MAX = 200


def clear_cache() -> None:
    _search_cache.clear()


def parse_loose_json(text: str) -> Any:
    """容错 JSON 解析。

    部分上游（酷我 `search.kuwo.cn/r.s`）返回**伪 JSON**：属性名与值用单引号包裹，
    `json.loads` 直接失败。这里先按标准 JSON 解析，失败则退化为 Python 字面量解析。
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        return ast.literal_eval(text)
    except Exception:  # noqa: BLE001
        pass
    # 末级尝试：把结构性单引号替换为双引号（值内部含单引号时可能仍失败）
    try:
        fixed = re.sub(r"'([^']*)'", r'"\1"', text)
        return json.loads(fixed)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"响应解析失败: {e}") from e


async def _try_channel(client: httpx.AsyncClient, ch: Channel, keyword: str, page: int) -> list[SearchTrack]:
    url = ch.build_url(keyword, page)
    r = await client.get(url, headers={"User-Agent": DEFAULT_UA, **ch.headers})
    r.raise_for_status()
    data = parse_loose_json(r.text)
    if not isinstance(data, dict):
        raise ValueError(f"响应不是对象: {type(data).__name__}")
    if ch.detect_error:
        err = ch.detect_error(data)
        if err:
            raise ValueError(f"业务态失败 {err}")
    items = ch.parse(data) or []
    return [t for t in items if t.title]


async def search_platform(
    platform: str, keyword: str, page: int = 1, enrich: bool = True
) -> list[SearchTrack]:
    """走通道矩阵搜索；全通道失败返回空列表并记录原因（不抛异常给上层）。"""
    factory = CHANNEL_FACTORIES.get(platform)
    if factory is None:
        logger.warning(f"不支持的搜索平台: {platform}")
        return []
    kw = keyword.strip()
    if not kw:
        return []

    key = f"{platform}:{kw}:{page}"
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    errors: list[str] = []
    for ch in sorted(factory(), key=lambda c: c.priority):
        try:
            async with httpx.AsyncClient(timeout=ch.timeout, follow_redirects=True) as client:
                items = await _try_channel(client, ch, kw, page)
            if not items:
                errors.append(f"{ch.id}: 空结果")
                continue
            if enrich and platform in ENRICHERS:
                try:
                    items = await ENRICHERS[platform](items)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"{platform} 后置增强失败（不影响结果）: {e}")
            for t in items:
                t.artist = _clean_artist(t.artist)
                if t.music_info.get("singer"):
                    t.music_info["singer"] = _clean_artist(t.music_info["singer"])
            if len(_search_cache) >= CACHE_MAX:
                _search_cache.pop(next(iter(_search_cache)), None)
            _search_cache[key] = (time.time(), items)
            if ch.priority > 1:
                logger.info(f"[{platform}] 主通道不可用，已通过备用通道 {ch.id} 命中")
            return items
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ch.id}: {e}")
            logger.warning(f"搜索通道 {ch.id} 失败: {e}")

    logger.error(f"[{platform}] 全部通道失败: {' | '.join(errors[:4])}")
    return []


async def search_multi(platforms: list[str], keyword: str, page: int = 1) -> list[SearchTrack]:
    """并发搜索多个平台并合并（保持平台顺序）。"""
    import asyncio

    if not platforms:
        return []
    results = await asyncio.gather(
        *(search_platform(p, keyword, page) for p in platforms),
        return_exceptions=True,
    )
    out: list[SearchTrack] = []
    for p, res in zip(platforms, results):
        if isinstance(res, Exception):
            logger.warning(f"平台 {p} 搜索异常: {res}")
            continue
        out.extend(res)
    return out
