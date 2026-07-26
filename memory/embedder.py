"""
SentenceTransformer 嵌入模型封装

实现 chromadb 0.4+ 的 EmbeddingFunction 协议：
    __call__(input: List[str]) -> List[List[float]]

模型默认路径来自 .env 中的 DEFAULT_EMBEDDING_MODEL_PATH，
等价于 sentence-transformers/all-MiniLM-L6-v2（首次运行会从 HuggingFace 下载）。

为了避免 chromadb 1.x 的兼容性检查抱怨「未实现 name() 方法」，
这里继承 chromadb 暴露出来的 EmbeddingFunction 基类。
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional

try:
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
except Exception:  # 老版本 chromadb 路径不同
    Documents = List[str]  # type: ignore
    Embeddings = List[List[float]]  # type: ignore

    class EmbeddingFunction:  # type: ignore
        pass


_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class STEmbedder(EmbeddingFunction):
    """SentenceTransformer 单例（按模型路径缓存）。"""

    _models = {}
    _lock = threading.Lock()

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path or os.getenv(
            "DEFAULT_EMBEDDING_MODEL_PATH", _DEFAULT_MODEL
        )

    @classmethod
    def _get_model(cls, path: str):
        with cls._lock:
            if path not in cls._models:
                from sentence_transformers import SentenceTransformer
                cls._models[path] = SentenceTransformer(path)
            return cls._models[path]

    # ---- chromadb 协议 ----
    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        model = self._get_model(self.model_path)
        emb = model.encode(
            list(input),
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return emb.tolist()

    @classmethod
    def name(cls) -> str:
        return "sentence-transformer"
