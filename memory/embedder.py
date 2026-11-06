"""
SentenceTransformer 嵌入模型封装：texts -> vectors。

    STEmbedder()(["一句话", ...]) -> List[List[float]]

模型默认路径来自 .env 中的 DEFAULT_EMBEDDING_MODEL_PATH，
等价于 sentence-transformers/all-MiniLM-L6-v2（首次运行会从 HuggingFace 下载）。
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional


_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class STEmbedder:
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

    def __call__(self, texts: List[str]) -> List[List[float]]:
        model = self._get_model(self.model_path)
        emb = model.encode(
            list(texts),
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return emb.tolist()
