"""硬链接管理核心服务。

核心原则：只读源文件，绝不修改音频内容字节。
仅操作硬链接的「文件名」和「目录路径」，不触碰 inode 指向的数据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.audio import read_audio_info
from app.core.config import config
from app.core.database import get_db
from app.core.logger import logger


@dataclass
class PlannedAction:
    """Dry-Run 预览操作。"""
    action: str          # create_link / rename_link / rebuild_link / delete_link / none
    source: str
    target: str
    reason: str = ""


@dataclass
class SyncResult:
    total: int = 0
    created: int = 0
    renamed: int = 0
    rebuilt: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _is_audio(path: Path) -> bool:
    return path.suffix.lower() in config.audio_extensions


def _sanitize(name: str) -> str:
    """清理文件名中的非法字符，但保留中文。"""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip().strip(".")
    return name or "unknown"


def _inode(path: Path) -> int:
    try:
        return os.stat(path).st_ino
    except OSError:
        return 0


def build_target_path(src: Path) -> Optional[Path]:
    """根据命名规则，计算源文件应映射到的目标硬链接路径。

    规则：媒体库/歌手名/专辑名/曲目号 - 歌曲名.后缀
    """
    library = config.library_dir
    if not library:
        return None
    info = read_audio_info(src)
    artist = _sanitize(info.effective_artist)
    album = _sanitize(info.album or "未知专辑")
    title = _sanitize(info.effective_title)
    track = info.track

    naming = config.get("naming", {})
    artist_dir = naming.get("artist", "{artist}").format(artist=artist)
    album_dir = naming.get("album", "{album} ({year})").format(
        album=album, year=info.year or "", artist=artist
    )
    track_name = naming.get("track", "{track:02d} - {title}").format(
        track=track, title=title, artist=artist, album=album
    )
    if not track_name.endswith(src.suffix.lower()):
        track_name += src.suffix.lower()

    return Path(library) / artist_dir / album_dir / track_name


def scan_source_files(source_dirs: list[str] | None = None) -> list[Path]:
    """扫描源目录下所有音频文件。"""
    dirs = source_dirs or config.source_dirs
    files: list[Path] = []
    seen: set[tuple[int, int]] = set()   # (inode, size) 去重，避免同一文件多路径
    for d in dirs:
        root = Path(d)
        if not root.exists():
            logger.warning(f"源目录不存在: {d}")
            continue
        for p in root.rglob("*"):
            if not p.is_file() or not _is_audio(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            key = (st.st_ino, st.st_size)
            if key in seen:
                continue
            seen.add(key)
            files.append(p)
    return files


def plan_sync(source_dirs: list[str] | None = None) -> list[PlannedAction]:
    """计算增量同步计划（dry-run）。"""
    actions: list[PlannedAction] = []
    db = get_db()
    library = config.library_dir
    if not library:
        return actions

    # 1) 源 → 目标 创建/重建
    for src in scan_source_files(source_dirs):
        target = build_target_path(src)
        if target is None:
            continue
        src_inode = _inode(src)
        existing = db.get_hardlink_by_source(str(src))

        if existing:
            old_target = Path(existing["target_path"])
            if not old_target.exists():
                actions.append(PlannedAction(
                    "rebuild_link", str(src), str(target),
                    "源硬链接已失效，重新创建"
                ))
            elif _inode(old_target) != src_inode:
                actions.append(PlannedAction(
                    "rebuild_link", str(src), str(target),
                    "inode 不一致，重建硬链接"
                ))
            elif old_target != target:
                actions.append(PlannedAction(
                    "rename_link", str(src), str(target),
                    f"目录/命名变化: {old_target.name} -> {target.name}"
                ))
            else:
                actions.append(PlannedAction("none", str(src), str(target), "已同步"))
            continue

        # 无记录：若目标已存在且 inode 一致，则跳过
        if target.exists() and _inode(target) == src_inode:
            actions.append(PlannedAction("none", str(src), str(target), "硬链接已存在"))
            continue
        if target.exists():
            actions.append(PlannedAction(
                "rebuild_link", str(src), str(target),
                "目标已存在但非硬链接，需重建"
            ))
        else:
            actions.append(PlannedAction(
                "create_link", str(src), str(target), "新建硬链接"
            ))

    # 2) 目标中源已删除的文件
    if config.get("hardlink.sync_delete", False):
        active_sources = {str(p) for p in scan_source_files(source_dirs)}
        for row in db.list_hardlinks("active"):
            if row["source_path"] not in active_sources:
                actions.append(PlannedAction(
                    "delete_link", row["source_path"], row["target_path"],
                    "源文件已删除"
                ))
    return actions


def execute_sync(dry_run: bool = False, source_dirs: list[str] | None = None) -> tuple[SyncResult, list[PlannedAction]]:
    """执行同步。dry_run=True 时仅返回计划不执行。"""
    actions = plan_sync(source_dirs)
    if dry_run:
        return SyncResult(total=len(actions)), actions

    result = SyncResult(total=len(actions))
    db = get_db()
    for a in actions:
        try:
            src = Path(a.source)
            target = Path(a.target)
            if a.action == "create_link" or a.action == "rebuild_link":
                if a.action == "rebuild_link" and target.exists():
                    target.unlink()  # 删除失效链接（只删链接，不动源数据）
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(src, target)
                st = src.stat()
                db.upsert_hardlink(str(src), str(target), st.st_ino, st.st_size)
                if a.action == "rebuild_link":
                    result.rebuilt += 1
                else:
                    result.created += 1
                db.add_log("info", "hardlink", f"{a.action}: {src.name} -> {target}")
            elif a.action == "rename_link":
                old_target = Path(db.get_hardlink_by_source(str(src))["target_path"])
                if old_target.exists() and old_target != target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(old_target, target)
                st = src.stat()
                db.upsert_hardlink(str(src), str(target), st.st_ino, st.st_size)
                result.renamed += 1
                db.add_log("info", "hardlink", f"rename: {old_target.name} -> {target}")
            elif a.action == "delete_link":
                t = Path(a.target)
                if t.exists():
                    t.unlink()
                db.delete_hardlink(a.target)
                result.deleted += 1
                db.add_log("info", "hardlink", f"delete_link: {a.target}")
            else:
                result.skipped += 1
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"{a.source}: {e}")
            logger.error(f"同步失败 {a.source}: {e}")
            db.add_log("error", "hardlink", f"失败 {a.source}: {e}")
    return result, actions


def rebuild_all(dry_run: bool = False) -> tuple[SyncResult, list[PlannedAction]]:
    """全量重建：删除所有失效硬链接后重新同步。"""
    db = get_db()
    actions: list[PlannedAction] = []
    if not dry_run:
        for row in db.list_hardlinks():
            t = Path(row["target_path"])
            if t.exists():
                try:
                    t.unlink()
                except OSError:
                    pass
        # 清空记录
        for row in db.list_hardlinks():
            db.delete_hardlink(row["target_path"])
    return execute_sync(dry_run=dry_run)


def cleanup_orphans(dry_run: bool = False) -> list[PlannedAction]:
    """清理失效硬链接（inode 与源不一致或源已删除）。"""
    actions: list[PlannedAction] = []
    db = get_db()
    for row in db.list_hardlinks():
        src = Path(row["source_path"])
        tgt = Path(row["target_path"])
        if not src.exists():
            actions.append(PlannedAction("delete_link", str(src), str(tgt), "源文件不存在"))
        elif not tgt.exists():
            actions.append(PlannedAction("delete_link", str(src), str(tgt), "硬链接不存在"))
        elif _inode(src) != _inode(tgt):
            actions.append(PlannedAction("rebuild_link", str(src), str(tgt), "inode 不一致"))
    if not dry_run:
        for a in actions:
            t = Path(a.target)
            if a.action == "delete_link":
                if t.exists():
                    t.unlink()
                db.delete_hardlink(a.target)
    return actions
