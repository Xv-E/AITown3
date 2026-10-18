"""
Neo4jMemory：把 NPC 语义记忆存进 Neo4j（与 ChromaMemory 同接口）

设计：
  - 一条记忆 = (:Memory {id, owner, text, timestamp, type, ..., embedding})
  - 只靠 owner 属性归属，不连 (:Person)-[:REMEMBERS]->：
    世界里大量 `(a)-[r]-(b)` 不限关系类型的查询，连了边就会把记忆带进提示词 / 图谱
  - 向量仍由本地 STEmbedder 计算，Neo4j 只存与算相似度（不需要 GenAI 插件）
  - search 先按 owner 过滤再精确算余弦（vector.similarity.cosine），
    不用 db.index.vector.queryNodes：后者是全局 top-k 再过滤，多 NPC 时会漏召回
  - world.clear_all()（MATCH (n) DETACH DELETE n）会顺带清掉记忆，与重建世界同步

API 与 ChromaMemory 一致：
    mem = Neo4jMemory.get_instance()
    mem.add(npc, "Alice 在 Tavern 见到了 Bob", type="observation", location="Tavern")
    hits = mem.search(npc, "我在 Tavern 见过谁？", top_k=3, where={"type": "observation"})
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    _HERE = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(os.path.abspath(os.path.join(_HERE, "..")), ".env"))
except Exception:
    pass


_RESERVED = frozenset({"id", "owner", "text", "embedding"})
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VECTOR_INDEX = "memory_embedding"


def _sanitize_props(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Neo4j 属性只收标量或同类标量列表；其余转字符串，并丢掉保留字段 / 非法键。"""
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if v is None or k in _RESERVED or not _KEY_RE.match(str(k)):
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, (list, tuple)) and v and all(isinstance(x, str) for x in v):
            out[k] = list(v)
        else:
            out[k] = str(v)
    return out


def _item(node: Dict[str, Any], score: Optional[float] = None) -> Dict[str, Any]:
    props = dict(node)
    props.pop("embedding", None)
    mid = props.pop("id", None)
    text = props.pop("text", "")
    out: Dict[str, Any] = {"id": mid, "text": text, "metadata": props}
    if score is not None:
        # vector.similarity.cosine 返回 (1+cos)/2；换回 Chroma 的 cosine distance = 1-cos
        out["distance"] = 2.0 * (1.0 - score)
    return out


class Neo4jMemory:
    """Neo4j 上的 NPC 语义记忆封装（单例）。"""

    _instance: Optional["Neo4jMemory"] = None
    _lock = threading.Lock()

    def __init__(self, driver=None, *, database: Optional[str] = None,
                 embedding_model_path: Optional[str] = None):
        if Neo4jMemory._instance is not None:
            raise RuntimeError("Neo4jMemory 是单例，请用 get_instance()")
        if driver is None:
            from neo4j_world import Neo4jWorld
            driver = Neo4jWorld.get_instance().driver
        self.driver = driver
        self.database = database or os.getenv("MEMORY_NEO4J_DATABASE") or None

        from embedder import STEmbedder
        self.embedder = STEmbedder(model_path=embedding_model_path)
        self._schema_ready = False
        Neo4jMemory._instance = self

    # ---------- 单例 / 生命周期 ----------

    @classmethod
    def get_instance(cls, driver=None, *, database: Optional[str] = None,
                     embedding_model_path: Optional[str] = None) -> "Neo4jMemory":
        with cls._lock:
            if cls._instance is None:
                Neo4jMemory(driver, database=database,
                            embedding_model_path=embedding_model_path)
            return cls._instance  # type: ignore[return-value]

    def close(self) -> None:
        """丢弃单例（driver 归 Neo4jWorld 管，不在这里关）。"""
        Neo4jMemory._instance = None

    # ---------- helpers ----------

    def _run(self, cypher: str, **params: Any) -> List[Dict[str, Any]]:
        self._ensure_schema()
        with self.driver.session(database=self.database) as session:
            return session.run(cypher, **params).data()

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        dims = len(self._embed(["dim probe"])[0])
        with self.driver.session(database=self.database) as session:
            session.run("CREATE CONSTRAINT memory_id IF NOT EXISTS "
                        "FOR (m:Memory) REQUIRE m.id IS UNIQUE")
            session.run("CREATE INDEX memory_owner_type IF NOT EXISTS "
                        "FOR (m:Memory) ON (m.owner, m.type)")
            # 现在 search 不走它；单个 NPC 记忆量很大时再切到索引检索
            session.run(
                f"CREATE VECTOR INDEX {_VECTOR_INDEX} IF NOT EXISTS "
                "FOR (m:Memory) ON m.embedding OPTIONS {indexConfig: {"
                "`vector.dimensions`: $dims, `vector.similarity_function`: 'cosine'}}",
                dims=dims,
            )
        self._schema_ready = True

    def _embed(self, texts: List[str]) -> List[List[float]]:
        return [list(map(float, v)) for v in self.embedder(texts)]

    # ---------- CRUD ----------

    def add(self, npc: str, text: str, *,
            mem_id: Optional[str] = None, **metadata: Any) -> str:
        """添加一条记忆。返回该条记忆的 id。"""
        return self.add_many(npc, [text], [metadata], ids=[mem_id or uuid.uuid4().hex])[0]

    def add_many(self, npc: str, texts: List[str],
                 metadatas: Optional[List[Dict[str, Any]]] = None,
                 *, ids: Optional[List[str]] = None) -> List[str]:
        if not texts:
            return []
        if metadatas is None:
            metadatas = [{} for _ in texts]
        ids = ids or [uuid.uuid4().hex for _ in texts]
        now = datetime.now().isoformat(timespec="seconds")
        vectors = self._embed(texts)  # 先算向量，事务里只写
        rows = []
        for mid, text, meta, vec in zip(ids, texts, metadatas, vectors):
            props = _sanitize_props(meta)
            props.setdefault("timestamp", now)
            rows.append({"id": mid, "text": text, "props": props, "embedding": vec})
        self._run(
            """
            UNWIND $rows AS row
            MERGE (m:Memory {id: row.id})
            SET m = row.props, m.id = row.id, m.owner = $npc, m.text = row.text
            WITH m, row
            CALL db.create.setNodeVectorProperty(m, 'embedding', row.embedding)
            """,
            rows=rows, npc=npc,
        )
        return ids

    def search(self, npc: str, query: str, top_k: int = 5,
               where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """语义检索 npc 的记忆。返回 [{id, text, metadata, distance}, ...]（distance 越小越近）。"""
        filters, params = [], {}
        for i, (k, v) in enumerate((where or {}).items()):
            if not _KEY_RE.match(str(k)):
                raise ValueError(f"非法的 where 字段: {k!r}")
            filters.append(f"m.`{k}` = $w{i}")
            params[f"w{i}"] = v
        cond = (" WHERE " + " AND ".join(filters)) if filters else ""
        rows = self._run(
            f"""
            MATCH (m:Memory {{owner: $npc}}){cond}
            WITH m, vector.similarity.cosine(m.embedding, $q) AS s
            ORDER BY s DESC LIMIT $k
            RETURN m, s
            """,
            npc=npc, q=self._embed([query])[0], k=max(1, int(top_k)), **params,
        )
        return [_item(r["m"], r["s"]) for r in rows]

    def list(self, npc: str) -> List[Dict[str, Any]]:
        """列出某 NPC 的所有记忆，按 timestamp 从旧到新。"""
        rows = self._run(
            "MATCH (m:Memory {owner: $npc}) RETURN m ORDER BY m.timestamp, elementId(m)",
            npc=npc,
        )
        return [_item(r["m"]) for r in rows]

    def count(self, npc: str) -> int:
        rows = self._run("MATCH (m:Memory {owner: $npc}) RETURN count(m) AS c", npc=npc)
        return int(rows[0]["c"]) if rows else 0

    def delete(self, npc: str, mem_id: str) -> None:
        self._run("MATCH (m:Memory {owner: $npc, id: $id}) DETACH DELETE m", npc=npc, id=mem_id)

    def clear(self, npc: str) -> None:
        """清空某 NPC 的全部记忆。"""
        self._run("MATCH (m:Memory {owner: $npc}) DETACH DELETE m", npc=npc)

    def clear_all(self) -> None:
        self._run("MATCH (m:Memory) DETACH DELETE m")

    def list_npcs(self) -> List[str]:
        rows = self._run("MATCH (m:Memory) RETURN DISTINCT m.owner AS o ORDER BY o")
        return [r["o"] for r in rows if r.get("o")]
