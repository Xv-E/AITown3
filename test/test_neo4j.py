"""
Neo4j 工具类测试

覆盖：
1. 连接测试（多 URI 兜底）
2. Neo4jWorld 单例初始化
3. 基本 CRUD（add_entity / list_entities / delete_entity）
4. 关系操作（link_entities / has_relationship）
5. 自然语言关系文本（get_entity_relationships_text）

运行方式（从仓库根目录）：
    python AITown3/test/test_neo4j.py
"""

import os
import sys

# 把 `AITown3/neo4j/` 加到 sys.path，从而能 `from neo4j_world import Neo4jWorld`
# 注意：直接把 `AITown3/` 加进 sys.path 会让本地 `neo4j/` 文件夹屏蔽掉
# 已安装的 neo4j Python 包，所以这里精确到 neo4j 子目录。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_NEO4J_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "neo4j"))
if _NEO4J_DIR not in sys.path:
    sys.path.insert(0, _NEO4J_DIR)

from neo4j_world import Neo4jWorld  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402


# 测试用的实体/关系命名空间，避免污染真实数据
_NS = "_test_aitown3_"
PERSON_A = f"{_NS}Alice"
PERSON_B = f"{_NS}Bob"
PLACE = f"{_NS}Tavern"


def _print_header(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def test_raw_connection() -> bool:
    """直接用 GraphDatabase 驱动测试连通性，便于快速定位问题。"""
    _print_header("1) 原生驱动连接测试")

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")

    print(f"URI:  {uri}")
    print(f"User: {user}")
    print(f"Pwd:  {'*' * len(password) if password else '(empty)'}")

    candidates = list(dict.fromkeys([
        uri,
        "bolt://localhost:7687",
        "bolt://127.0.0.1:7687",
    ]))

    for candidate in candidates:
        try:
            driver = GraphDatabase.driver(candidate, auth=(user, password))
            driver.verify_connectivity()
            with driver.session() as session:
                ok = session.run("RETURN 1 AS x").single()["x"] == 1
            driver.close()
            if ok:
                print(f"OK  连接成功: {candidate}")
                return True
        except Exception as e:
            print(f"FAIL {candidate}: {str(e)[:120]}")

    print("所有候选 URI 都连接失败，请检查 Neo4j 是否启动以及 .env 配置。")
    return False


def test_singleton() -> bool:
    """Neo4jWorld 单例语义。"""
    _print_header("2) Neo4jWorld 单例测试")

    # 先确保没有残留实例
    if Neo4jWorld._instance is not None:
        Neo4jWorld._instance.close()

    w1 = Neo4jWorld.get_instance()
    w2 = Neo4jWorld.get_instance()
    same = w1 is w2
    print(f"get_instance() 返回同一实例: {same}")
    return same


def test_crud() -> bool:
    """节点 CRUD 测试。"""
    _print_header("3) 节点 CRUD 测试")

    world = Neo4jWorld.get_instance()

    world.add_entity(PERSON_A, entity_type="Person", age=30)
    world.add_entity(PERSON_B, entity_type="Person", age=25)
    world.add_entity(PLACE, entity_type="Place")

    names = world.list_entities("Person")
    print(f"Person 列表: {names}")
    assert PERSON_A in names and PERSON_B in names, "新建的 Person 节点未出现在列表中"

    node = world.get_entity_by_name(PERSON_A, label="Person")
    print(f"查询 {PERSON_A}: {node}")
    assert node is not None and node.get("name") == PERSON_A

    print("CRUD 基本断言通过")
    return True


def test_relationships() -> bool:
    """关系操作和自然语言文本生成。"""
    _print_header("4) 关系测试")

    world = Neo4jWorld.get_instance()

    world.link_entities(PERSON_A, "Person", PERSON_B, "Person", "KNOWS")
    world.link_entities(PERSON_A, "Person", PLACE, "Place", "LOCATED_AT")

    assert world.has_relationship(PERSON_A, "Person", PERSON_B, "Person", "KNOWS"), \
        "KNOWS 关系应当存在"
    assert world.has_relationship(PERSON_A, "Person", PLACE, "Place", "LOCATED_AT"), \
        "LOCATED_AT 关系应当存在"

    text = world.get_entity_relationships_text(PERSON_A, label="Person")
    print(text)
    assert PERSON_B in text and PLACE in text, "自然语言文本应包含相关实体名"
    return True


def cleanup() -> None:
    """清理本测试创建的所有节点。"""
    _print_header("5) 清理测试数据")

    world = Neo4jWorld.get_instance()
    for name, label in [
        (PERSON_A, "Person"),
        (PERSON_B, "Person"),
        (PLACE, "Place"),
    ]:
        try:
            world.delete_entity(name, label=label)
        except Exception as e:
            print(f"清理 {label}({name}) 时失败: {e}")

    world.close()
    print("已删除测试节点并关闭连接")


def main() -> int:
    if not test_raw_connection():
        return 1

    try:
        test_singleton()
        test_crud()
        test_relationships()
    except AssertionError as e:
        print(f"\n断言失败: {e}")
        cleanup()
        return 1
    except Exception as e:
        print(f"\n运行异常: {e}")
        cleanup()
        return 1

    cleanup()
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
