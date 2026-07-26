"""
ChromaMemory：基于 ChromaDB 的 NPC 语义记忆存储

设计：
  - 单例：所有 NPC 共享同一个 PersistentClient（同一个磁盘目录）
  - 每个 NPC 一个 collection：memory_<NPC_NAME>，避免互相污染
  - 一条记忆 = (id, document, metadata, embedding)
        document : 自然语言文本（喂给 embedder）
        metadata : {"timestamp": iso, "type": "...", "actor": "...", "target": "...", "location": "..."}
                   （Chroma 限制 metadata 只能是 str/int/float/bool）
        id       : 自动用 uuid4

API:
    mem = ChromaMemory.get_instance()
    mem.add(npc, "Alice 在 Tavern 见到了 Bob", type="observation", location="Tavern")
    hits = mem.search(npc, "我在 Tavern 见过谁？", top_k=3)
    mem.list(npc)
    mem.clear(npc)
    mem.clear_all()
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# 加载 AITown3/.env（不依赖 neo4j 子目录的 env_loader）
try:
    from dotenv import load_dotenv
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _AITOWN3_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
    load_dotenv(os.path.join(_AITOWN3_ROOT, ".env"))
except Exception:
    pass


_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_collection_name(npc_name: str) -> str:
    """
    Chroma 对 collection 名有要求：3-512 字符、首尾字母/数字、只能含字母数字 _ - .
    这里统一前缀 + 净化非法字符。
    """
    cleaned = _NAME_RE.sub("_", npc_name).strip("_") or "anon"
    return f"memory_{cleaned}"


def _sanitize_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Chroma 只接受标量元数据；把不合法的值转成字符串。"""
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


class ChromaMemory:
    """ChromaDB 上的 NPC 语义记忆封装（单例）。"""

    _instance: Optional["ChromaMemory"] = None
    _lock = threading.Lock()

    def __init__(self, persist_path: Optional[str] = None,
                 embedding_model_path: Optional[str] = None):
        if ChromaMemory._instance is not None:
            raise RuntimeError("ChromaMemory 是单例，请用 get_instance()")

        # 解析持久化目录：默认 AITown3/data/chroma
        if persist_path is None:
            persist_path = os.getenv("CHROMA_PATH", "data/chroma")
        if not os.path.isabs(persist_path):
            # 相对路径锚定到 AITown3/ 根
            here = os.path.dirname(os.path.abspath(__file__))
            aitown3_root = os.path.abspath(os.path.join(here, ".."))
            persist_path = os.path.abspath(os.path.join(aitown3_root, persist_path))
        os.makedirs(persist_path, exist_ok=True)
        self.persist_path = persist_path

        # 延迟引入 chromadb，避免在不需要时多花启动开销
        import chromadb
        from chromadb.config import Settings

        self.client = chromadb.PersistentClient(
            path=persist_path,
            settings=Settings(anonymized_telemetry=False),
        )

        from embedder import STEmbedder
        self.embedder = STEmbedder(model_path=embedding_model_path)

        ChromaMemory._instance = self

    # ---------- 单例 / 生命周期 ----------

    @classmethod
    def get_instance(cls, persist_path: Optional[str] = None,
                     embedding_model_path: Optional[str] = None) -> "ChromaMemory":
        with cls._lock:
            if cls._instance is None:
                ChromaMemory(persist_path=persist_path,
                             embedding_model_path=embedding_model_path)
            return cls._instance  # type: ignore[return-value]

    def close(self) -> None:
        """关闭并丢弃单例，方便测试 reset。Chroma 客户端本身无需显式关闭。"""
        ChromaMemory._instance = None

    # ---------- collection helpers ----------

    def _get_or_create(self, npc: str):
        return self.client.get_or_create_collection(
            name=_safe_collection_name(npc),
            embedding_function=self.embedder,
            metadata={"hnsw:space": "cosine"},
        )

    # ---------- CRUD ----------

    def add(self, npc: str, text: str, *,
            mem_id: Optional[str] = None, **metadata: Any) -> str:
        """添加一条记忆。返回该条记忆的 id。"""
        col = self._get_or_create(npc)
        meta = _sanitize_metadata({
            "owner": npc,
            "timestamp": metadata.pop("timestamp", datetime.now().isoformat(timespec="seconds")),
            **metadata,
        })
        mid = mem_id or uuid.uuid4().hex
        col.add(ids=[mid], documents=[text], metadatas=[meta])
        return mid

    def add_many(self, npc: str, texts: List[str],
                 metadatas: Optional[List[Dict[str, Any]]] = None) -> List[str]:
        col = self._get_or_create(npc)
        ids = [uuid.uuid4().hex for _ in texts]
        metas = []
        if metadatas is None:
            metadatas = [{} for _ in texts]
        for m in metadatas:
            m = dict(m)
            m.setdefault("owner", npc)
            m.setdefault("timestamp",
                         datetime.now().isoformat(timespec="seconds"))
            metas.append(_sanitize_metadata(m))
        col.add(ids=ids, documents=texts, metadatas=metas)
        return ids

    def search(self, npc: str, query: str, top_k: int = 5,
               where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """语义检索 npc 的记忆。返回 [{id, text, metadata, distance}, ...]。"""
        col = self._get_or_create(npc)
        # 如果集合还是空的，避免 query 报错
        if col.count() == 0:
            return []
        result = col.query(
            query_texts=[query],
            n_results=min(top_k, col.count()),
            where=where,
        )
        items: List[Dict[str, Any]] = []
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        for i, doc in enumerate(docs):
            items.append({
                "id": ids[i] if i < len(ids) else None,
                "text": doc,
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else None,
            })
        return items

    def list(self, npc: str) -> List[Dict[str, Any]]:
        """列出某 NPC 的所有记忆（不做语义检索，返回顺序不保证）。"""
        col = self._get_or_create(npc)
        if col.count() == 0:
            return []
        result = col.get()
        items: List[Dict[str, Any]] = []
        for i, doc in enumerate(result.get("documents", [])):
            items.append({
                "id": result["ids"][i],
                "text": doc,
                "metadata": result.get("metadatas", [])[i] if result.get("metadatas") else {},
            })
        return items

    def count(self, npc: str) -> int:
        return self._get_or_create(npc).count()

    def delete(self, npc: str, mem_id: str) -> None:
        self._get_or_create(npc).delete(ids=[mem_id])

    def clear(self, npc: str) -> None:
        """清空某 NPC 的全部记忆，并删除其 collection。"""
        name = _safe_collection_name(npc)
        try:
            self.client.delete_collection(name)
        except Exception:
            pass

    def clear_all(self) -> None:
        """删除当前数据库下所有 collection。"""
        try:
            cols = self.client.list_collections()
        except Exception:
            return
        for c in cols:
            # 兼容 chromadb 不同版本：可能返回 Collection 对象或字符串
            cname = getattr(c, "name", None) or (c if isinstance(c, str) else None)
            if cname:
                try:
                    self.client.delete_collection(cname)
                except Exception:
                    pass

    def list_npcs(self) -> List[str]:
        """返回所有已建过 collection 的 NPC 名（去掉 memory_ 前缀）。"""
        out = []
        try:
            cols = self.client.list_collections()
        except Exception:
            return out
        for c in cols:
            cname = getattr(c, "name", None) or (c if isinstance(c, str) else "")
            if cname.startswith("memory_"):
                out.append(cname[len("memory_"):])
        return out
