import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "director", "web"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.append(p)


# ---------- 共享 fixture ----------

@pytest.fixture(scope="session")
def neo4j_world():
    """开发用 Neo4j；连不上就跳过。注意：图测试会清空并重建这个库。"""
    from neo4j_world import Neo4jWorld

    try:
        world = Neo4jWorld.get_instance()
        world.run_query("RETURN 1 AS ok")
    except Exception as e:
        pytest.skip(f"Neo4j unavailable: {e}")
    return world


@pytest.fixture
def world(neo4j_world):
    return neo4j_world


class BagOfWordsEmbedder:
    """测试用的假嵌入：词袋哈希，共享的词越多越近；不下载模型。"""

    dims = 64

    def __call__(self, texts):
        import math
        import re

        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in re.findall(r"[a-z0-9]+", str(text).lower()):
                vec[sum(map(ord, word)) % self.dims] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm if any(vec) else 1.0 / math.sqrt(self.dims) for x in vec])
        return out


@pytest.fixture
def mem(neo4j_world):
    """Neo4jMemory 单例，换成假嵌入；测试结束清掉 test_ 开头的 NPC 的记忆。"""
    from neo4j_memory import Neo4jMemory

    previous = Neo4jMemory._instance
    Neo4jMemory._instance = None
    instance = Neo4jMemory.get_instance(neo4j_world.driver, embedder=BagOfWordsEmbedder())
    yield instance
    neo4j_world.run_query("MATCH (m:Memory) WHERE m.owner STARTS WITH 'test_' DETACH DELETE m")
    Neo4jMemory._instance = previous


# ---------- 脚本式测试 ----------

def pytest_pyfunc_call(pyfuncitem):
    """不少测试同时是可直接运行的脚本，用 return True/False 表示结果：返回 False 视为失败。"""
    func = pyfuncitem.obj
    if inspect.iscoroutinefunction(func):
        return None
    names = pyfuncitem._fixtureinfo.argnames
    result = func(**{n: pyfuncitem.funcargs[n] for n in names})
    if result is False:
        pytest.fail(f"{pyfuncitem.name} returned False (see captured output for the failing check)")
    return True
