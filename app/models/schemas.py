"""Pydantic 数据模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HardlinkRecord(BaseModel):
    """硬链接记录。"""
    id: Optional[int] = None
    source_path: str
    target_path: str
    inode: int = 0
    size: int = 0
    status: str = "active"          # active / orphan / missing
    created_at: str = ""

    class Config:
        from_attributes = True


class ScrapeRecord(BaseModel):
    """刮削记录。"""
    id: Optional[int] = None
    target_path: str                # 硬链接文件路径
    provider: str = ""
    match_score: float = 0.0
    matched_key: str = ""           # 匹配到的专辑/歌曲标识
    status: str = "pending"         # pending / done / failed / skipped
    cover_path: str = ""
    lyrics_path: str = ""
    album_nfo_path: str = ""
    artist_nfo_path: str = ""
    error: str = ""
    updated_at: str = ""

    class Config:
        from_attributes = True


class LogEntry(BaseModel):
    """操作日志。"""
    id: Optional[int] = None
    timestamp: str
    level: str = "info"             # info / warn / error
    category: str = "hardlink"      # hardlink / scrape / assets / system
    message: str
