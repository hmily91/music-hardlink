"""定时任务调度：增量扫描 + 自动刮削。"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import config
from app.core.database import get_db
from app.core.logger import logger
from app.services import hardlink, scraper

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def _scheduled_sync() -> None:
    logger.info("定时增量扫描开始")
    result, _ = hardlink.execute_sync(dry_run=False)
    get_db().add_log("info", "system",
                     f"定时同步: 新建{result.created} 重命名{result.renamed} "
                     f"重建{result.rebuilt} 删除{result.deleted} 跳过{result.skipped}")
    # 对新建/重建的文件自动刮削
    new_tracks = []
    for row in get_db().list_hardlinks("active"):
        s = get_db().get_scrape(row["target_path"])
        if s is None or s["status"] != "done":
            new_tracks.append(row["target_path"])
    if new_tracks and config.get("scrape.enabled", True):
        await scraper.scrape_batch(new_tracks)


def start_scheduler() -> None:
    interval = config.get("hardlink.scan_interval_minutes", 30)
    if interval <= 0:
        logger.info("自动扫描已禁用")
        return
    scheduler.add_job(_scheduled_sync, "interval", minutes=interval,
                      id="sync", replace_existing=True)
    scheduler.start()
    logger.info(f"调度器已启动，扫描间隔 {interval} 分钟")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
