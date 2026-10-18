"""FastAPI 主应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import config
from app.core.logger import logger
from app.core.scheduler import start_scheduler, stop_scheduler

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("音乐库硬链接刮削工具启动")
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("已停止")


app = FastAPI(title="Music Hardlink Scraper", version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"success": True, "message": "ok"}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
