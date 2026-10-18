"""日志与统一异常处理工具。"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("music-hardlink")

# 降低第三方库噪音
for noisy in ("musicbrainzngs", "httpx", "httpcore", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
