"""元数据刮削数据源基类与多数据源管理。

数据源优先级可配置，自动 fallback。全部通过 HTTP 只读请求，绝不修改音频。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.core.audio import AudioInfo, TrackMetadata
from app.core.logger import logger


class BaseProvider(ABC):
    name = "base"

    @abstractmethod
    async def search(self, info: AudioInfo) -> Optional[TrackMetadata]:
        """根据音频信息搜索匹配的元数据。"""
        raise NotImplementedError


def fuzzy_score(query: str, candidate: str) -> float:
    """简单的模糊匹配打分（0~1）。"""
    if not query or not candidate:
        return 0.0
    q = query.lower().strip()
    c = candidate.lower().strip()
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.7
    # 子串重叠度
    qset = set(q)
    cset = set(c)
    if not cset:
        return 0.0
    overlap = len(qset & cset) / len(cset)
    return round(overlap * 0.5, 3)


def match_album_score(info: AudioInfo, meta: TrackMetadata) -> float:
    """综合匹配打分：标题、专辑、歌手、时长。"""
    score = 0.0
    if info.album and meta.album:
        score += fuzzy_score(info.album, meta.album) * 0.35
    if info.effective_title and meta.title:
        score += fuzzy_score(info.effective_title, meta.title) * 0.30
    if info.effective_artist and meta.artist:
        score += fuzzy_score(info.effective_artist, meta.artist) * 0.20
    # 时长匹配
    if info.duration > 0 and meta.duration > 0:
        diff = abs(info.duration - meta.duration)
        if diff < 3:
            score += 0.15
        elif diff < 10:
            score += 0.05
    return round(score, 3)
