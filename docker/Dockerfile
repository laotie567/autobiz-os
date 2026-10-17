# AutoBiz OS 应用镜像（E-3）：粉丝侧只装 Docker 即可用。
# 构建：docker compose -f docker-compose.app.yml build app
# 发布：ghcr.io/laotie567/autobiz-os（国际仓，install.sh 默认拉取，构建仅作回退）
# 说明：构建时预置 512 维向量模型（HF_ENDPOINT 默认 huggingface.co；网络受限可改 hf-mirror.com）。
FROM python:3.12-slim

ENV HF_ENDPOINT=https://huggingface.co \
    AUTOBIZ_HOME=/workspace \
    AUTOBIZ_MODEL_CACHE=/models \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
# 运行时资产：本体 / 提示词 / 模型路由表 / 模板（缺一则 check 直接失败）
COPY autobiz ./autobiz
COPY ontology ./ontology
COPY prompts ./prompts
COPY templates ./templates
COPY config/models.yaml ./config/models.yaml
RUN uv sync --frozen --no-dev

# 预置向量模型（超时/失败不阻断构建，运行时首次使用会再尝试下载）
RUN timeout 300 uv run python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-zh-v1.5', cache_dir='/models')" \
    || echo "warn: 模型预下载失败/超时，容器首次运行时会重试"

ENTRYPOINT ["uv", "run", "autobiz"]
