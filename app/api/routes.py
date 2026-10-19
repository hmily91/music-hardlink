"""API 路由：配置、硬链接、刮削、日志、媒体库浏览、音源管理。"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from app.core.config import config
from app.core.database import get_db
from app.core.logger import logger
from app.services import hardlink, scraper, search

router = APIRouter(prefix="/api")


class ConfigBody(BaseModel):
    config: dict


class SourceUrlBody(BaseModel):
    url: str
    filename: Optional[str] = None


def _sources_dir() -> Path:
    d = Path(config.data_dir) / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 配置 ----------
@router.get("/config")
def get_config():
    return {"data": config.as_dict()}


@router.put("/config")
def update_config(body: ConfigBody):
    # 深度合并：嵌套字典只覆盖提供的键，避免丢掉 audio_extensions 等未提交字段
    config.merge(body.config)
    config.save()
    return {"success": True, "message": "配置已保存"}


# ---------- 硬链接 ----------
@router.get("/hardlink/plan")
def plan_hardlink():
    actions = hardlink.plan_sync()
    return {"data": [
        {"action": a.action, "source": a.source, "target": a.target, "reason": a.reason}
        for a in actions
    ]}


@router.post("/hardlink/sync")
def sync_hardlink(dry_run: bool = False):
    result, actions = hardlink.execute_sync(dry_run=dry_run)
    return {
        "data": {
            "result": result.__dict__,
            "actions": [
                {"action": a.action, "source": a.source, "target": a.target, "reason": a.reason}
                for a in actions
            ] if dry_run else [],
        }
    }


@router.post("/hardlink/rebuild")
def rebuild_hardlink(dry_run: bool = False):
    result, _ = hardlink.rebuild_all(dry_run=dry_run)
    return {"data": result.__dict__}


@router.post("/hardlink/cleanup")
def cleanup_hardlink(dry_run: bool = False):
    actions = hardlink.cleanup_orphans(dry_run=dry_run)
    return {"data": [a.__dict__ for a in actions]}


@router.get("/hardlink/list")
def list_hardlink(status: Optional[str] = None):
    rows = get_db().list_hardlinks(status)
    return {"data": [dict(r) for r in rows]}


# ---------- 刮削 ----------
@router.post("/scrape/track")
async def scrape_track(target_path: str, force: bool = False):
    result = await scraper.scrape_track(target_path, force=force)
    return {"data": result.__dict__}


@router.post("/scrape/album")
async def scrape_album(album_dir: str, force: bool = False):
    result = await scraper.scrape_album(album_dir, force=force)
    return {"data": {"total": result.total, "done": result.done,
                     "failed": result.failed, "skipped": result.skipped}}


@router.post("/scrape/all")
async def scrape_all(force: bool = False):
    result = await scraper.scrape_all(force=force)
    return {"data": {"total": result.total, "done": result.done,
                     "failed": result.failed, "skipped": result.skipped}}


@router.get("/scrape/list")
def list_scrape(status: Optional[str] = None):
    rows = get_db().list_scrapes(status)
    return {"data": [dict(r) for r in rows]}


# ---------- 日志 ----------
@router.get("/log/list")
def list_log(level: Optional[str] = None, category: Optional[str] = None, limit: int = 200):
    rows = get_db().list_logs(level=level, category=category, limit=limit)
    return {"data": [dict(r) for r in rows]}


# ---------- 媒体库结构 ----------
@router.get("/library/tree")
def library_tree():
    """返回媒体库目录树（歌手/专辑/曲目/外置资源）。"""
    library = config.library_dir
    if not library or not Path(library).exists():
        return {"data": []}
    tree = []
    root = Path(library)
    for artist_dir in sorted([d for d in root.iterdir() if d.is_dir()]):
        artist_node = {"name": artist_dir.name, "type": "artist",
                       "path": str(artist_dir), "children": []}
        for album_dir in sorted([d for d in artist_dir.iterdir() if d.is_dir()]):
            album_node = {"name": album_dir.name, "type": "album",
                          "path": str(album_dir), "children": []}
            for item in sorted(album_dir.iterdir()):
                if item.is_file():
                    album_node["children"].append({
                        "name": item.name,
                        "type": "asset" if item.suffix in (".jpg", ".lrc", ".nfo") else "track",
                        "path": str(item),
                    })
            artist_node["children"].append(album_node)
        tree.append(artist_node)
    return {"data": tree}


# ---------- 统计 ----------
@router.get("/stats")
def stats():
    db = get_db()
    return {
        "data": {
            "hardlinks": db.count_hardlinks(),
            "scraped": len(db.list_scrapes("done")),
            "pending": len(db.list_scrapes("pending")),
            "failed": len(db.list_scrapes("failed")),
        }
    }


# ---------- 洛雪音源管理 ----------
@router.get("/source/list")
def list_sources():
    """列出已导入的洛雪音源脚本文件。"""
    d = _sources_dir()
    files = []
    for f in d.glob("*.js"):
        files.append({
            "name": f.name,
            "size": f.stat().st_size,
            "modified": f.stat().st_mtime,
        })
    return {"data": files}


@router.post("/source/upload")
async def upload_source(file: UploadFile = File(...)):
    """上传洛雪音源 .js 文件。"""
    if not file.filename.endswith(".js"):
        raise HTTPException(400, "仅支持 .js 音源脚本")
    d = _sources_dir()
    target = d / Path(file.filename).name
    with open(target, "wb") as f:
        shutil.copyfileobj(file.file, f)
    _reload_bridge()
    return {"success": True, "message": f"已导入 {target.name}"}


@router.post("/source/import")
async def import_source(body: SourceUrlBody):
    """从在线 URL 导入洛雪音源脚本。"""
    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            r = await client.get(body.url)
            r.raise_for_status()
            content = r.text
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"下载失败: {e}")

    if "<" in content[:200] and "html" in content[:200].lower():
        raise HTTPException(400, "下载到的不是 JS 脚本（可能是网页）")

    filename = body.filename or Path(body.url.split("?")[0]).name or "source.js"
    if not filename.endswith(".js"):
        filename += ".js"
    d = _sources_dir()
    (d / filename).write_text(content, encoding="utf-8")
    _reload_bridge()
    return {"success": True, "message": f"已导入 {filename}"}


@router.delete("/source/{filename}")
def delete_source(filename: str):
    d = _sources_dir()
    target = d / filename
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    target.unlink()
    _reload_bridge()
    return {"success": True, "message": f"已删除 {filename}"}


@router.post("/source/reload")
def reload_source():
    _reload_bridge()
    return {"success": True, "message": "已重载音源"}


@router.get("/source/status")
def source_status():
    """查询 lx-bridge 中已初始化的音源 key。"""
    try:
        import httpx
        r = httpx.get(f"{config.get('scrape.lx_bridge_url')}/sources", timeout=5)
        if r.status_code == 200:
            return {"success": True, "data": r.json().get("data", [])}
    except Exception:  # noqa: BLE001
        pass
    return {"success": True, "data": []}


def _reload_bridge():
    """通知 lx-bridge 重载音源。"""
    try:
        import httpx
        httpx.post(f"{config.get('scrape.lx_bridge_url')}/reload", timeout=5)
    except Exception:  # noqa: BLE001
        pass


# ---------- 搜索与下载 ----------
@router.get("/search")
async def search_music(keyword: str, limit: int = 20, source: str = ""):
    """搜索音乐，返回歌曲列表。source 可选曲库（tx/kw/wy/kg/mg）。"""
    results = await search.search_songs(keyword, limit=limit, source=source)
    return {"data": [
        {
            "id": s.id, "mid": s.mid, "title": s.title, "artist": s.artist,
            "album": s.album, "duration": s.duration,
            "source": s.source, "cover_url": s.cover_url,
        }
        for s in results
    ]}


@router.get("/search/sources")
def search_sources():
    """返回可用曲库列表。"""
    return {"data": [
        {"key": "tx", "name": "QQ音乐"},
        {"key": "kw", "name": "酷我音乐"},
        {"key": "wy", "name": "网易云音乐"},
        {"key": "kg", "name": "酷狗音乐"},
        {"key": "mg", "name": "咪咕音乐"},
    ]}


@router.post("/download")
async def download_music(keyword: str, song_id: str, source: str = ""):
    """下载单曲到源目录，并自动触发硬链接同步 + 刮削。"""
    result = await search.search_and_download(keyword, song_id, source=source)
    if not result.get("success"):
        return {"success": False, "message": result.get("message", "下载失败")}

    path = result.get("path")
    # 自动硬链接同步
    try:
        sync_result, _ = hardlink.execute_sync(dry_run=False)
        get_db().add_log("info", "hardlink",
                         f"下载后同步: 新建{sync_result.created} 补录{sync_result.recorded} 重建{sync_result.rebuilt}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"下载后同步失败: {e}")

    # 自动刮削：优先查库，查不到直接按命名规则算目标路径
    if path:
        try:
            from app.services import scraper as _scraper
            target = None
            row = get_db().get_hardlink_by_source(path)
            if row:
                target = row["target_path"]
            else:
                built = hardlink.build_target_path(Path(path))
                if built and built.exists():
                    target = str(built)
            if target:
                scrape_res = await _scraper.scrape_track(target, force=True)
                logger.info(f"下载后刮削: {target} -> {scrape_res.status}")
            else:
                logger.warning(f"下载后未找到硬链接目标，跳过刮削: {path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"下载后刮削失败: {e}")

    return {"success": True, "message": result.get("message", "下载完成"),
            "path": path}
