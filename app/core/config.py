"""全局配置管理。

配置优先级：环境变量 > config.yaml > 默认值。
所有配置项支持通过 Web 界面动态修改（保存到 data/config.json）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from app.core.logger import logger


DEFAULT_CONFIG: dict[str, Any] = {
    # 目录
    "source_dirs": [],                 # 源目录列表（硬链接来源，只读）
    "library_dir": "",                 # 媒体库根目录（硬链接目标）
    "data_dir": "/data",               # 数据目录（缓存/数据库）

    # 命名规则
    "naming": {
        "artist": "{artist}",                      # 歌手目录
        "album": "{album} ({year})",               # 专辑目录
        "track": "{track:02d} - {title}",          # 曲目文件名
        "various_artists": "群星",                  # 合辑歌手目录名
    },

    # 硬链接
    "hardlink": {
        "audio_extensions": [
            ".mp3", ".flac", ".wav", ".m4a", ".aac",
            ".alac", ".ogg", ".ape", ".opus", ".wma",
            ".aiff", ".dsf", ".dff", ".tak", ".wv", ".tta",
        ],
        "sync_delete": False,          # 源文件删除后是否同步删除硬链接
        "rebuild_on_inode_change": True,  # inode 不一致时自动重建
        "scan_interval_minutes": 30,   # 增量扫描间隔（分钟）
        "copy_fallback": True,         # 跨文件系统无法硬链接时降级为复制（源文件仍只读）
    },

    # 刮削数据源优先级
    "scrape": {
        "enabled": True,
        "providers": ["musicbrainz", "netease", "qq"],   # 优先级顺序，自动 fallback
        "netease_cookie": "",
        "qq_cookie": "",
        "language": "zh",              # 歌词语言偏好
        "bilingual_lyrics": True,      # 双语歌词
        "timeout_seconds": 20,
        "lx_enabled": True,            # 是否启用洛雪音源桥接（补齐歌词/封面）
        "lx_bridge_url": "http://127.0.0.1:2692",   # lx-bridge 地址
        "lx_source": "kw",             # 默认使用的洛雪音源 key（kw/kg/tx/wy/mg/local）
        # 自定义音源中转 API（ikun 等聚合音源）
        "api_url": "https://c.wwwweb.top",          # 中转 API 地址
        "api_key": "",                 # API Key（用户音源的 key）
        "api_source": "tx",            # 取链接时用的曲库 source（tx=QQ/kw=酷我/wy=网易云）
        "api_quality": "128k",         # 下载音质
    },

    # 外置资源
    "assets": {
        "cover_name": "cover.jpg",     # 封面名
        "cover_aliases": ["folder.jpg", "cover.jpg"],  # 别名
        "cover_min_size": 800,         # 封面最小分辨率
        "lyrics_ext": ".lrc",          # 歌词扩展名
        "generate_nfo": True,          # 生成 NFO
        "album_nfo_name": "album.nfo",
        "artist_nfo_name": "artist.nfo",
    },

    # Web 服务
    "server": {
        "host": "0.0.0.0",
        "port": 2691,
    },
}


class Config:
    """配置单例，支持从 yaml / json / 环境变量加载并动态读写。"""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._json_path: Optional[Path] = None
        self._load()

    def _load(self) -> None:
        cfg = _deep_copy(DEFAULT_CONFIG)

        # 1) config.yaml
        yaml_path = Path(os.environ.get("CONFIG_FILE", "/app/config/config.yaml"))
        if yaml_path.exists():
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                _deep_merge(cfg, loaded)
                logger.info(f"已加载配置文件: {yaml_path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"读取 config.yaml 失败: {e}")

        # 2) data/config.json（Web 界面保存的运行时配置，优先级更高）
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        self._json_path = data_dir / "config.json"
        if self._json_path.exists():
            try:
                with open(self._json_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                _deep_merge(cfg, loaded)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"读取运行时配置失败: {e}")

        # 3) 环境变量覆盖（MUSIC_ 前缀，双下划线映射嵌套）
        for key, value in os.environ.items():
            if key.startswith("MUSIC_"):
                path = key[6:].lower().split("__")
                _set_nested(cfg, path, _coerce(value))

        # 4) 规范化：列表类型字段统一转为 list，避免前端 .join() 报错
        for dotted in ("source_dirs", "scrape.providers"):
            _normalize_list(cfg, dotted)

        self._config = cfg

    # ---- 通用访问 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        node = self._config
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any) -> None:
        _set_nested(self._config, dotted.split("."), value)

    def merge(self, override: dict) -> None:
        """深度合并配置（嵌套字典只覆盖提供的键，不整体替换）。"""
        _deep_merge(self._config, override)
        for dotted in ("source_dirs", "scrape.providers"):
            _normalize_list(self._config, dotted)

    def as_dict(self) -> dict[str, Any]:
        return _deep_copy(self._config)

    def save(self) -> None:
        """把运行时配置持久化到 data/config.json。"""
        if not self._json_path:
            return
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._json_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
        tmp.replace(self._json_path)

    def reload(self) -> None:
        self._load()

    # ---- 便捷属性 ----
    @property
    def source_dirs(self) -> list[str]:
        val = self.get("source_dirs", [])
        if isinstance(val, str):
            val = [v.strip() for v in val.split(",") if v.strip()]
        return val if isinstance(val, list) else [val]

    @property
    def library_dir(self) -> str:
        return self.get("library_dir", "")

    @property
    def data_dir(self) -> str:
        return self.get("data_dir", "/data")

    @property
    def audio_extensions(self) -> list[str]:
        return [e.lower() if e.startswith(".") else f".{e.lower()}"
                for e in self.get("hardlink.audio_extensions", [])]


def _deep_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False))


def _normalize_list(cfg: dict, dotted: str) -> None:
    """把 dotted 路径对应的值规范化为 list。"""
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            return
        node = node[part]
    key = parts[-1]
    val = node.get(key)
    if val is None:
        node[key] = []
    elif isinstance(val, str):
        node[key] = [v.strip() for v in val.split(",") if v.strip()]
    elif isinstance(val, list):
        node[key] = val
    else:
        node[key] = [val]


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _set_nested(cfg: dict, path: list[str], value: Any) -> None:
    cur = cfg
    for part in path[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[path[-1]] = value


def _coerce(value: str) -> Any:
    """把环境变量字符串转为合适的类型。"""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        pass
    # 逗号分隔的列表
    if "," in value:
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


config = Config()
