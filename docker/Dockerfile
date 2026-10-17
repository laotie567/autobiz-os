# AutoBiz OS 应用镜像（E-3）：粉丝侧只装 Docker 即可用。
# 构建：docker compose -f docker-compose.app.yml build app
# 说明：构建时经 hf-mirror 预置 512 维向量模型，首次运行不再联网下载。
FROM python:3.12-slim

ENV HF_ENDPOINT=https://hf-mirror.com \
    AUTOBIZ_HOME=/workspace \
    AUTOBIZ_MODEL_CACHE=/models \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY autobiz ./autobiz
RUN uv sync --frozen --no-dev

# 预置向量模型（离线环境构建失败不阻断，运行时首次使用会再尝试下载）
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-zh-v1.5', cache_dir='/models')" \
    || echo "warn: 模型预下载失败，容器首次运行时会重试"

ENTRYPOINT ["uv", "run", "autobiz"]
