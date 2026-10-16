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


@pytest.fixture(scope="session")
def mem(tmp_path_factory):
    """ChromaMemory 单例指向临时目录，不碰 data/chroma。"""
    from chroma_memory import ChromaMemory

    previous = ChromaMemory._instance
    ChromaMemory._instance = None
    instance = ChromaMemory.get_instance(persist_path=str(tmp_path_factory.mktemp("chroma")))
    yield instance
    ChromaMemory._instance = previous


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
