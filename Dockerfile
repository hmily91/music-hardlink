# 音乐库硬链接刮削工具 Dockerfile
# 基础镜像 python:3.11-slim + Node.js（用于洛雪音源桥接）

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    NODE_VERSION=22

# 安装音频读取工具 + Node.js（运行洛雪音源脚本）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mediainfo \
        curl \
        ca-certificates \
        tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖，利用缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用与桥接脚本
COPY app ./app
COPY node-bridge ./node-bridge
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 数据目录（缓存/数据库/配置/音源脚本）
VOLUME ["/data", "/source", "/library"]

EXPOSE 2691

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:2691/api/health',timeout=3)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
