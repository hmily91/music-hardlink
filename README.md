# 音乐库硬链接刮削工具 (Docker 版)

对标洛雪音乐的元数据刮削能力，核心面向 **PT 保种场景**。采用 **硬链接零拷贝 + 元数据全外置** 架构，**绝对不修改源音乐文件**，保障做种完整性。

## 核心原则

### 1. 硬链接不修改原则
- 媒体库中的音频文件仅为源文件的**硬链接**，程序绝不修改硬链接音频的任何内容字节（ID3 标签、元数据区块、内嵌封面/歌词）
- 仅允许修改硬链接的**文件名、目录路径**，不触碰文件数据本身（inode 不变）

### 2. 元数据全外置原则
- 所有刮削产物（封面、歌词、专辑信息、歌手信息）以**独立文件**存储，绝不内嵌
- 外置资源与硬链接同目录存放，兼容 Plex/Emby/Jellyfin/Kodi
- 目录结构：`媒体库/歌手/专辑/曲目号 - 歌曲名.后缀`

## 功能特性

| 模块 | 说明 |
|------|------|
| 硬链接管理 | 扫描音频（MP3/FLAC/WAV/AAC/ALAC/OGG/APE 等）、按规则建硬链接、增量同步、inode 重复校验、批量重建、清理失效链接 |
| 元数据刮削 | 多数据源（MusicBrainz / 网易云 / QQ音乐）+ 洛雪音源桥接，自动 fallback，本地缓存 |
| 外置资源 | `cover.jpg`/`folder.jpg`、同名 `.lrc` 双语歌词、`album.nfo`/`artist.nfo` |
| Web 管理 | 媒体库可视化、刮削进度、硬链接状态、配置管理、日志筛选、Dry Run 预览 |
| 洛雪音源 | 导入与洛雪音乐**相通**的 `.js` 音源脚本，容器内置 Node.js + lx 运行时 |

## 快速开始

### 1. 编写 docker-compose.yml

```yaml
services:
  music-hardlink:
    image: hamily/music-hardlink:latest
    container_name: music-hardlink
    restart: unless-stopped
    ports:
      - "2691:2691"
    volumes:
      - ./data:/data                    # 数据目录（数据库/缓存/音源脚本）
      - /volume1/downloads/music:/source:ro   # 源目录（只读，程序绝不写源文件）
      - /volume1/media/music:/library          # 媒体库目录（硬链接目标）
    environment:
      - TZ=Asia/Shanghai
      - MUSIC_SOURCE_DIRS=/source
      - MUSIC_LIBRARY_DIR=/library
      - MUSIC_DATA_DIR=/data
```

> ⚠️ **重要**：`/source` 和 `/library` 必须挂载在**同一文件系统**上，硬链接才能生效（跨文件系统无法创建硬链接，会报错）。

### 2. 启动

```bash
docker compose up -d
# 访问 http://<NAS-IP>:2691
```

## 配置

所有配置可在 Web 界面「设置」页修改，也可通过环境变量 `MUSIC_` 前缀覆盖（下划线用 `__` 分隔层级）：

| 环境变量 | 说明 |
|----------|------|
| `MUSIC_SOURCE_DIRS` | 源目录（逗号分隔多个） |
| `MUSIC_LIBRARY_DIR` | 媒体库目录 |
| `MUSIC_DATA_DIR` | 数据目录 |
| `MUSIC_HARDLINK__SCAN_INTERVAL_MINUTES` | 增量扫描间隔（分钟，0=禁用） |
| `MUSIC_HARDLINK__SYNC_DELETE` | 源删除后是否同步删除硬链接 |
| `MUSIC_SCRAPE__PROVIDERS` | 数据源优先级（逗号分隔） |

## 洛雪音源导入

在 Web 界面「音源」页，可像洛雪桌面端一样导入 `.js` 音源脚本：

- **本地导入**：上传 `.js` 文件
- **在线导入**：粘贴音源 URL（如 `https://raw.githubusercontent.com/pdone/lx-music-source/main/xxx/latest.js`）

导入后点击「重载音源」，容器内的 Node.js lx-bridge 会加载脚本并初始化音源（`kw/kg/tx/wy/mg/local` 等）。

> **说明**：洛雪音源脚本通过 `globalThis.lx` API 与宿主交互。本项目内置了一个完整的 `lx` 运行时 shim（`request/on/send/utils.buffer/utils.crypto/utils.zlib`），因此**洛雪音源脚本可以原样运行**，无需修改。
>
> 洛雪第三方音源（kw/kg/tx/wy/mg）通常只提供 `musicUrl`（播放地址），歌词/封面主要由内置数据源（MusicBrainz/网易云/QQ）承担，部分音源的 `local` 源可补充歌词/封面。

## 数据源说明

- **MusicBrainz**：免登录，可靠的 fallback（专辑/歌手/封面/年份）
- **网易云音乐 / QQ音乐**：免登录公开接口，可在设置页填 Cookie 提升精度
- **洛雪音源**：用户导入的 `.js` 脚本，桥接后参与刮削

## 硬链接机制说明

- 通过 `os.link()` 创建硬链接，源文件与媒体库文件共享同一 inode
- 程序**只删除失效的链接**（`unlink`），不触碰源文件数据
- 源文件删除后，硬链接仍保留数据（这就是做种的本质），可通过 `sync_delete` 配置是否清理

## 目录结构示例

```
/library                          # 媒体库根目录
├── 周杰伦/                        # 歌手目录
│   ├── 叶惠美 (2003)/             # 专辑目录
│   │   ├── 01 - 以父之名.flac      # 硬链接（源文件的硬链接）
│   │   ├── 01 - 以父之名.lrc       # 外置歌词
│   │   ├── cover.jpg              # 外置封面（含 folder.jpg 别名）
│   │   └── album.nfo              # 专辑信息
│   └── artist.nfo                 # 歌手信息
```

## 开发

```bash
# 本地运行（需 Python 3.11+ 与 Node.js）
pip install -r requirements.txt
node node-bridge/lx-bridge.js &     # 启动洛雪音源桥接
uvicorn app.main:app --reload       # 启动主应用
```

## 构建镜像

```bash
docker build -t music-hardlink:latest .
```
