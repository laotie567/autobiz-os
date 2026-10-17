"""向量化：本地模型（config/models.yaml 的 embedding 节）。数据不出本机。"""
from __future__ import annotations

import os
from functools import lru_cache

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


@lru_cache(maxsize=1)
def _cache_dir() -> str:
    d = os.environ.get("AUTOBIZ_MODEL_CACHE")          # 应用镜像构建时预置模型的路径
    if not d:
        from autobiz.paths import sub
        d = sub(".cache", "fastembed")                 # D-6：模型缓存在工作区，代码目录只读
    os.makedirs(d, exist_ok=True)
    if os.listdir(d):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")   # 已缓存则不再联网检查，连接器启动更快
    return d


@lru_cache(maxsize=1)
def dim() -> int:
    """F-3：向量维度唯一来源是 models.yaml；与数据库列不一致时 doctor 会提示 reembed。"""
    from autobiz.llm import models_config
    return int(models_config()["embedding"].get("dim", 512))


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding
    from autobiz.llm import models_config
    return TextEmbedding(models_config()["embedding"]["model"], cache_dir=_cache_dir())


def embed(texts: list[str]) -> list[list[float]]:
    if os.environ.get("AUTOBIZ_FAKE_EMBED"):   # 测试用：确定性假向量
        return [_fake(t) for t in texts]
    return [v.tolist() for v in _model().embed(texts)]


def _fake(text: str) -> list[float]:
    import hashlib
    n = dim()
    v = [0.0] * n
    for ch in text:
        v[int(hashlib.md5(ch.encode()).hexdigest(), 16) % n] += 1.0
    s = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / s for x in v]
