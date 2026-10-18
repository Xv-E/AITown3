"""
Neo4j 数据库连接管理类

提供单例模式的Neo4j连接管理，支持基本的图数据库操作。
"""

from neo4j import GraphDatabase
from contextlib import contextmanager
from typing import Iterator, Optional, List, Dict, Any
import copy
import os
import logging
import re
import threading
from env_loader import load_project_env

# 从仓库根目录加载 `.env`（在任意子目录下运行脚本也能读到）
load_project_env()

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 可能改图的 Cypher（保守：CALL 一律当写）；命中则不缓存并让读缓存失效
_WRITE_RE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP|FOREACH|CALL)\b", re.IGNORECASE)

# 读缓存按「标签 / 关系类型」失效：写 Item 不清 Person 的读。
# 模式里有不带标签的具名节点 (x)、不带类型的关系、DETACH DELETE（顺带删掉各种关系），
# 或用到 Entity（会和别的标签叠在同一节点上）的查询算「全局」（匿名的 () 没法 SET / DELETE，不算）：
# 全局的写清空整个缓存，全局的读被任何写清掉。宁可多清，不读到旧值。
_TOKEN_RE = re.compile(r"[:|]\s*`?([A-Za-z_][A-Za-z0-9_]*)`?")
_UNLABELED_NODE_RE = re.compile(r"(?<![\w.`])\(\s*[A-Za-z_]\w*\s*(\{[^}]*\})?\s*\)")
_UNTYPED_REL_RE = re.compile(r"--|-\[\s*\w*\s*(\*[^\]]*)?\]-")
_GLOBAL_TOKENS = frozenset({"Entity"})
_scope_memo: Dict[str, Any] = {}


def _query_scope(cypher: str):
    """(标签和关系类型集合, 是否全局)。"""
    hit = _scope_memo.get(cypher)
    if hit is None:
        tokens = frozenset(_TOKEN_RE.findall(cypher))
        is_global = (
            not tokens
            or bool(tokens & _GLOBAL_TOKENS)
            or bool(_UNLABELED_NODE_RE.search(cypher))
            or bool(_UNTYPED_REL_RE.search(cypher))
            or bool(re.search(r"\bCALL\b|\bDETACH\b", cypher, re.IGNORECASE))
        )
        hit = (tokens, is_global)
        if len(_scope_memo) < 4096:
            _scope_memo[cypher] = hit
    return hit


def entity_query(label: str) -> str:
    """get_entity_by_name 用的查询（tick 预加载按同一字符串填缓存）。"""
    return f"MATCH (n:{label} {{name: $name}}) RETURN n"


class Neo4jWorld:
    """
    Neo4j 世界模型单例类
    
    管理Neo4j数据库连接，提供基本的图数据库操作接口。
    使用单例模式确保全局只有一个数据库连接实例。
    """
    
    _instance: Optional['Neo4jWorld'] = None
    
    def __init__(self, uri: Optional[str] = None, user: Optional[str] = None, 
                 password: Optional[str] = None):
        """
        初始化Neo4j连接
        
        Args:
            uri: Neo4j数据库URI，默认从环境变量读取或使用默认值
            user: 用户名，默认从环境变量读取或使用默认值
            password: 密码，默认从环境变量读取或使用默认值
        
        Raises:
            RuntimeError: 如果实例已经存在（单例模式）
        """
        if Neo4jWorld._instance is not None:
            raise RuntimeError("Neo4jWorld 是单例，不能重复初始化！")
        
        # 从环境变量读取配置，提供默认值
        # 支持两种协议格式：bolt:// 和 neo4j://
        # - bolt:// 用于直接连接（推荐）
        # - neo4j:// 用于路由连接（集群环境）
        default_uri = os.getenv('NEO4J_URI', 'bolt://localhost:7687')
        self.uri = uri or default_uri
        
        # 如果用户提供了 neo4j:// 格式，保持原样（驱动支持）
        # 如果只提供了 IP/端口，自动添加 bolt:// 前缀
        if self.uri and '://' not in self.uri:
            self.uri = f'bolt://{self.uri}'
        
        self.user = user or os.getenv('NEO4J_USER', 'neo4j')
        self.password = password or os.getenv('NEO4J_PASSWORD', 'password')
        
        # 创建数据库驱动
        try:
            self.driver = GraphDatabase.driver(
                self.uri, 
                auth=(self.user, self.password)
            )
            # 验证连接
            self.driver.verify_connectivity()
        except Exception as e:
            error_msg = (
                f"无法连接到Neo4j数据库 ({self.uri})\n"
                f"错误: {str(e)}\n\n"
                f"请确保：\n"
                f"1. Neo4j数据库服务已启动\n"
                f"2. 连接信息正确（URI: {self.uri}, User: {self.user}）\n"
                f"3. 防火墙允许连接到端口7687\n\n"
                f"启动Neo4j的方法：\n"
                f"- 如果使用Docker: docker run -p 7474:7474 -p 7687:7687 neo4j\n"
                f"- 如果已安装Neo4j Desktop: 在应用中启动数据库\n"
                f"- 如果使用Neo4j Community Edition: 运行 bin/neo4j start"
            )
            logger.error(error_msg)
            raise ConnectionError(error_msg) from e
        
        # 读缓存（见 read_cache）
        self._cache_lock = threading.Lock()
        self._cache_depth = 0
        self._cache_gen = 0
        self._cache: Dict[Any, List[Dict[str, Any]]] = {}
        # key -> 这条读查询的 (标签/关系类型, 是否全局)，写的时候按它挑要清的
        self._cache_scope: Dict[Any, Any] = {}
        # 按标签 / 关系类型记的写版本号：读的过程中只有「相关的」写才让结果作废
        self._tok_gen: Dict[str, int] = {}
        self._global_gen = 0

        # 保存单例实例
        Neo4jWorld._instance = self
        
        logger.info(f"Neo4jWorld initialized: {self.uri}")
    
    @staticmethod
    def get_instance(uri: Optional[str] = None, user: Optional[str] = None, 
                     password: Optional[str] = None) -> 'Neo4jWorld':
        """
        获取全局单例实例
        
        支持直接传参，兼容老代码的使用方式：
        - Neo4jWorld.get_instance(password="12345678")
        - Neo4jWorld.get_instance(uri="bolt://localhost:7687", user="neo4j", password="12345678")
        
        Args:
            uri: Neo4j数据库URI（可选）
            user: 用户名（可选）
            password: 密码（可选）
        
        Returns:
            Neo4jWorld: Neo4jWorld实例
        """
        if Neo4jWorld._instance is None:
            Neo4jWorld(uri=uri, user=user, password=password)
        return Neo4jWorld._instance
    
    def close(self):
        """关闭数据库连接并清除单例实例"""
        if self.driver:
            self.driver.close()
            logger.info("Neo4jWorld connection closed")
        Neo4jWorld._instance = None
    
    def clear_all(self):
        """
        删除所有节点和关系（⚠️ 危险操作，仅用于测试）
        """
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        self.invalidate_cache()
        logger.warning("All nodes and relationships cleared")

    @contextmanager
    def read_cache(self) -> Iterator[None]:
        """作用域内相同的只读查询只打一次库；写查询清掉和它标签 / 关系类型相交的缓存（见 _query_scope）。

        一次模拟 tick 里各角色反复读同样的属性（是否死亡、物品脚本……），
        缓存后结果与不缓存一致：只复用「上次相关的写之后」读到的结果。可嵌套、可跨线程。
        """
        with self._cache_lock:
            self._cache_depth += 1
        try:
            yield
        finally:
            with self._cache_lock:
                self._cache_depth -= 1
                if self._cache_depth == 0:
                    self._cache.clear()
                    self._cache_scope.clear()
                    self._cache_gen += 1

    def invalidate_cache(self, cypher: Optional[str] = None) -> None:
        """cypher 为空：全清；否则只清和这条写查询标签 / 关系类型相交的读（全局的写仍全清）。"""
        with self._cache_lock:
            self._cache_gen += 1
            tokens, is_global = _query_scope(cypher) if cypher is not None else (frozenset(), True)
            if is_global:
                self._global_gen += 1
            else:
                for t in tokens:
                    self._tok_gen[t] = self._tok_gen.get(t, 0) + 1
            if not self._cache:
                return
            if is_global:
                self._cache.clear()
                self._cache_scope.clear()
                return
            stale = [
                k for k, (rtokens, rglobal) in self._cache_scope.items()
                if rglobal or rtokens & tokens
            ]
            for k in stale:
                self._cache.pop(k, None)
                self._cache_scope.pop(k, None)

    def seed_cache(self, cypher: str, rows: List[Dict[str, Any]], *, version: Any, **params) -> bool:
        """把批量查出来的结果预先填成某条单条查询的缓存（必须和那条查询的结果完全一致）。

        version 是批量查询开始前的 cache_version(cypher)；之后有过相关的写就不填（结果可能已过期）。
        """
        key = (cypher, repr(sorted(params.items())))
        with self._cache_lock:
            if not self._cache_depth or self._version_locked(cypher) != version:
                return False
            self._cache[key] = copy.deepcopy(rows)
            self._cache_scope[key] = _query_scope(cypher)
            return True

    def cache_version(self, cypher: str) -> Any:
        """和这条读查询相关的写版本：变了就说明期间有会影响它的写。"""
        with self._cache_lock:
            return self._version_locked(cypher)

    def _version_locked(self, cypher: str) -> Any:
        tokens, is_global = _query_scope(cypher)
        if is_global:
            return ("all", self._cache_gen)
        return (self._global_gen, tuple(self._tok_gen.get(t, 0) for t in sorted(tokens)))

    def _run(self, cypher: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        with self.driver.session() as session:
            result = session.run(cypher, **params)
            return [record.data() for record in result]

    def run_query(self, cypher: str, **params) -> List[Dict[str, Any]]:
        """
        执行Cypher查询
        
        Args:
            cypher: Cypher查询语句
            **params: 查询参数
        
        Returns:
            List[Dict[str, Any]]: 查询结果记录列表
        """
        if _WRITE_RE.search(cypher):
            try:
                return self._run(cypher, params)
            finally:
                self.invalidate_cache(cypher)
        if not self._cache_depth:
            return self._run(cypher, params)

        key = (cypher, repr(sorted(params.items())))
        with self._cache_lock:
            hit = self._cache.get(key)
            version = self._version_locked(cypher)
        if hit is not None:
            return copy.deepcopy(hit)
        rows = self._run(cypher, params)
        with self._cache_lock:
            # 读的过程中若有相关的写发生，结果可能已过期，不入缓存
            if self._cache_depth and self._version_locked(cypher) == version:
                self._cache[key] = rows
                self._cache_scope[key] = _query_scope(cypher)
        return copy.deepcopy(rows)

    def get_entity_by_name(self, name: str, label: str = "Entity") -> Optional[Dict[str, Any]]:
        """
        根据名称查找实体节点
        
        Args:
            name: 实体名称
            label: 节点标签
        
        Returns:
            Optional[Dict]: 节点数据，如果不存在则返回None
        """
        result = self.run_query(entity_query(label), name=name)
        if result:
            return result[0].get("n")
        return None
    
    def add_entity(self, name: str, entity_type: str = "Entity", **props) -> None:
        """
        添加实体节点到数据库
        
        Args:
            name: 实体名称
            entity_type: 实体类型（节点标签，支持多标签如 "Person:Entity"）
            **props: 额外的节点属性
        """
        # 构建属性字符串
        props_str = ", ".join([f"{k}: ${k}" for k in props.keys()])
        
        # 构建查询
        query = f"MERGE (n:{entity_type} {{name: $name}})"
        if props_str:
            query += f" SET n += {{{props_str}}}"
        
        self.run_query(query, name=name, **props)
        logger.info(f"[DB] 创建节点 {entity_type}({name})")
    
    def link_entities(self, name_a: str, label_a: str, name_b: str, 
                     label_b: str, rel_type: str, **rel_props) -> None:
        """
        创建两个实体之间的关系
        
        Args:
            name_a: 实体A的名称
            label_a: 实体A的标签
            name_b: 实体B的名称
            label_b: 实体B的标签
            rel_type: 关系类型
            **rel_props: 关系的额外属性
        """
        # 构建关系属性
        rel_props_str = ""
        if rel_props:
            rel_props_list = [f"{k}: ${k}" for k in rel_props.keys()]
            rel_props_str = " {" + ", ".join(rel_props_list) + "}"
        
        query = f"""
        MATCH (a:{label_a} {{name: $a_name}})
        MATCH (b:{label_b} {{name: $b_name}})
        MERGE (a)-[r:{rel_type}{rel_props_str}]->(b)
        """
        
        self.run_query(query, a_name=name_a, b_name=name_b, **rel_props)
        logger.info(f"[DB] {name_a} -[{rel_type}]-> {name_b}")
    
    def list_entities(self, label: str = "Entity") -> List[str]:
        """
        列出指定标签的所有实体名称
        
        Args:
            label: 节点标签
        
        Returns:
            List[str]: 实体名称列表
        """
        query = f"MATCH (n:{label}) RETURN DISTINCT n.name AS name ORDER BY name"
        result = self.run_query(query)
        names = [record["name"] for record in result if record.get("name")]
        # 同名节点去重（防止历史脏数据导致 UI 重复）
        seen: set = set()
        out: List[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out
    
    @staticmethod
    def get_all_names(label: str) -> List[str]:
        """
        获取某类节点的所有名称列表（静态方法）
        
        Args:
            label: 节点标签
        
        Returns:
            List[str]: 名称列表，如果标签不存在则返回空列表
        """
        world = Neo4jWorld.get_instance()
        try:
            # 先检查标签是否存在，避免警告
            check_query = "CALL db.labels() YIELD label RETURN label"
            existing_labels = world.run_query(check_query)
            label_names = [record["label"] for record in existing_labels]
            
            if label not in label_names:
                # 标签不存在，直接返回空列表
                logger.debug(f"标签 {label} 不存在于数据库中，返回空列表")
                return []
            
            # 标签存在，执行查询
            query = f"MATCH (n:{label}) RETURN collect(n.name) AS names"
            result = world.run_query(query)
            names = result[0]["names"] if result else []
            # 过滤掉None值（如果有的话）
            return [name for name in names if name is not None]
        except Exception as e:
            # 如果查询失败，返回空列表
            logger.debug(f"获取标签 {label} 的节点名称失败: {e}")
            return []
    
    def delete_entity(self, name: str, label: str = "Entity") -> None:
        """
        删除实体节点及其所有关系
        
        Args:
            name: 实体名称
            label: 节点标签
        """
        query = f"MATCH (n:{label} {{name: $name}}) DETACH DELETE n"
        self.run_query(query, name=name)
        logger.info(f"[DB] 删除节点 {label}({name})")
    
    def has_relationship(self, name_a: str, label_a: str, name_b: str, 
                       label_b: str, rel_type: str) -> bool:
        """
        检查两个实体之间是否存在指定类型的关系（任意方向）
        
        Args:
            name_a: 实体A的名称
            label_a: 实体A的标签
            name_b: 实体B的名称
            label_b: 实体B的标签
            rel_type: 关系类型
        
        Returns:
            bool: 如果存在关系返回True，否则返回False
        """
        query = f"""
        MATCH (a:{label_a} {{name: $a_name}})-[r:{rel_type}]-(b:{label_b} {{name: $b_name}})
        RETURN COUNT(r) > 0 AS exists
        """
        result = self.run_query(query, a_name=name_a, b_name=name_b)
        return result and result[0]["exists"]

    def has_directed_relationship(
        self,
        name_a: str,
        label_a: str,
        name_b: str,
        label_b: str,
        rel_type: str,
    ) -> bool:
        """检查是否存在 a -[:rel]-> b（单向）。"""
        query = f"""
        MATCH (a:{label_a} {{name: $a_name}})-[r:{rel_type}]->(b:{label_b} {{name: $b_name}})
        RETURN COUNT(r) > 0 AS exists
        """
        result = self.run_query(query, a_name=name_a, b_name=name_b)
        return bool(result and result[0]["exists"])
    
    def _relation_to_natural_language(self, source_name: str, source_type: str,
                                     rel_type: str, target_name: str, target_type: str,
                                     rel_props: dict = None) -> str:
        """
        将关系转换为自然语言描述
        
        Args:
            source_name: 源实体名称
            source_type: 源实体类型
            rel_type: 关系类型
            target_name: 目标实体名称
            target_type: 目标实体类型
            rel_props: 关系属性
        
        Returns:
            str: 自然语言描述
        """
        # 关系类型到自然语言的映射
        relation_templates = {
            "LOCATED_AT": "{source} is located at {target}",
            "ADJACENT_TO": "{source} is adjacent to {target}",
            "OWN": "{source} owns {target}",
            "KNOWS": "{source} knows {target}",
            "HAS_KNOWLEDGE": "{source} knows about {target}",
            "HAS_ITEM": "{source} has {target}",
            "HAS_EFFECT": "{target} affects {source}",
            "VISITED": "{source} has visited {target}",
            "ATTACKED": "{source} attacked {target}",
            "CHAT_WITH": "{source} chatted with {target}",
            "BOUGHT_FROM": "{source} bought {target}",
            "SOLD_TO": "{source} sold {target}",
            "HAS": "{source} has {target}",
            "BELONGS_TO": "{source} belongs to {target}",
            "CONTAINS": "{source} contains {target}",
            "LEADS_TO": "{source} leads to {target}",
            "CONNECTED_TO": "{source} is connected to {target}",
        }
        
        # 获取模板或使用默认格式
        template = relation_templates.get(rel_type)
        if template:
            # 使用预定义模板
            description = template.format(
                source=source_name,
                target=target_name,
                rel_type=rel_type
            )
        else:
            # 对于未知关系类型，尝试生成自然语言描述
            # 将关系类型转换为小写并用空格分隔
            rel_words = rel_type.lower().replace("_", " ").split()
            if len(rel_words) > 1:
                # 多词关系：使用第一个词作为动词
                verb = rel_words[0]
                description = f"{source_name} {verb} {target_name}"
            else:
                # 单词关系：直接使用
                description = f"{source_name} {rel_type.lower()} {target_name}"
        
        # 如果有关系属性，添加到描述中
        if rel_props:
            props_str = ", ".join([f"{k} is {v}" for k, v in rel_props.items() 
                                  if v is not None and k != "name"])
            if props_str:
                description += f" ({props_str})"
        
        return description
    
    def get_entity_relationships_text(self, entity_name: str, label: str = "Entity", 
                                      max_depth: int = 1, natural_language: bool = True) -> str:
        """
        获取实体的所有关系并转换为自然语言文本（用于LLM输入）
        
        Args:
            entity_name: 实体名称
            label: 实体标签
            max_depth: 最大深度（跳数）
        
        Returns:
            str: 关系文本描述
        """
        # 查询实体的所有关系（包括出边和入边）
        # 使用更简单的查询，直接获取关系和节点信息
        query = f"""
        MATCH (start:{label} {{name: $name}})-[r]-(related)
        RETURN start.name as start_name, 
               type(r) as rel_type, 
               related.name as related_name,
               labels(related) as related_labels,
               properties(r) as rel_props
        LIMIT 100
        """
        
        result = self.run_query(query, name=entity_name)
        
        if not result:
            return f"{entity_name} has no relationships."
        
        # 收集所有关系
        relationships = []
        seen = set()
        
        for record in result:
            start_name = record.get("start_name", "Unknown")
            related_name = record.get("related_name", "Unknown")
            rel_type = record.get("rel_type", "RELATED_TO")
            related_labels = record.get("related_labels", [])
            rel_props = record.get("rel_props", {})
            
            # 获取相关实体的类型（排除Entity标签）
            related_type = [l for l in related_labels if l != "Entity"]
            related_type = related_type[0] if related_type else "Entity"
            
            # 避免重复
            rel_key = (start_name, rel_type, related_name)
            if rel_key in seen:
                continue
            seen.add(rel_key)
            
            # 构建关系描述
            if natural_language:
                # 使用自然语言描述
                rel_desc = self._relation_to_natural_language(
                    start_name, "Entity", rel_type, related_name, related_type, rel_props
                )
            else:
                # 使用技术格式
                if rel_props:
                    # 过滤掉空值和name属性
                    props_str = ", ".join([f"{k}={v}" for k, v in rel_props.items() 
                                         if v is not None and k != "name"])
                    if props_str:
                        rel_desc = f"{start_name} -[{rel_type} ({props_str})]-> {related_name} ({related_type})"
                    else:
                        rel_desc = f"{start_name} -[{rel_type}]-> {related_name} ({related_type})"
                else:
                    rel_desc = f"{start_name} -[{rel_type}]-> {related_name} ({related_type})"
            
            relationships.append(rel_desc)
        
        if not relationships:
            return f"{entity_name} has no relationships."
        
        # 构建文本描述
        text_parts = [f"Relationships for {entity_name}:"]
        for rel in relationships:
            text_parts.append(f"  - {rel}")
        
        return "\n".join(text_parts)
    
    def get_relationships_text(self, entity_name: Optional[str] = None, 
                              label: Optional[str] = None,
                              rel_types: Optional[List[str]] = None,
                              max_relationships: int = 50,
                              natural_language: bool = True) -> str:
        """
        获取关系并转换为自然语言文本（用于LLM输入）
        
        Args:
            entity_name: 实体名称（如果指定，只返回与该实体相关的关系）
            label: 实体标签（如果指定，只返回该标签的实体关系）
            rel_types: 关系类型列表（如果指定，只返回这些类型的关系）
            max_relationships: 最大关系数量
        
        Returns:
            str: 关系文本描述
        """
        # 构建查询 - 直接返回节点名称和标签，而不是节点对象
        if entity_name:
            # 查询特定实体的关系
            if label:
                query = f"""
                MATCH (a:{label} {{name: $name}})-[r]-(b)
                RETURN a.name as a_name, 
                       labels(a) as a_labels,
                       type(r) as rel_type, 
                       b.name as b_name,
                       labels(b) as b_labels,
                       properties(r) as rel_props
                LIMIT $limit
                """
                params = {"name": entity_name, "limit": max_relationships}
            else:
                query = """
                MATCH (a {name: $name})-[r]-(b)
                RETURN a.name as a_name,
                       labels(a) as a_labels,
                       type(r) as rel_type, 
                       b.name as b_name,
                       labels(b) as b_labels,
                       properties(r) as rel_props
                LIMIT $limit
                """
                params = {"name": entity_name, "limit": max_relationships}
        else:
            # 查询所有关系
            if label:
                if rel_types:
                    rel_types_str = "|".join(rel_types)
                    query = f"""
                    MATCH (a:{label})-[r:{rel_types_str}]-(b)
                    RETURN a.name as a_name,
                           labels(a) as a_labels,
                           type(r) as rel_type, 
                           b.name as b_name,
                           labels(b) as b_labels,
                           properties(r) as rel_props
                    LIMIT $limit
                    """
                else:
                    query = f"""
                    MATCH (a:{label})-[r]-(b)
                    RETURN a.name as a_name,
                           labels(a) as a_labels,
                           type(r) as rel_type, 
                           b.name as b_name,
                           labels(b) as b_labels,
                           properties(r) as rel_props
                    LIMIT $limit
                    """
            else:
                if rel_types:
                    rel_types_str = "|".join(rel_types)
                    query = f"""
                    MATCH (a)-[r:{rel_types_str}]-(b)
                    RETURN a.name as a_name,
                           labels(a) as a_labels,
                           type(r) as rel_type, 
                           b.name as b_name,
                           labels(b) as b_labels,
                           properties(r) as rel_props
                    LIMIT $limit
                    """
                else:
                    query = """
                    MATCH (a)-[r]-(b)
                    RETURN a.name as a_name,
                           labels(a) as a_labels,
                           type(r) as rel_type, 
                           b.name as b_name,
                           labels(b) as b_labels,
                           properties(r) as rel_props
                    LIMIT $limit
                    """
            params = {"limit": max_relationships}
        
        result = self.run_query(query, **params)
        
        if not result:
            return "No relationships found."
        
        # 构建文本描述
        text_parts = []
        seen = set()
        
        for record in result:
            a_name = record.get("a_name", "Unknown")
            b_name = record.get("b_name", "Unknown")
            rel_type = record.get("rel_type", "RELATED_TO")
            rel_props = record.get("rel_props", {})
            
            # 获取标签
            a_labels = record.get("a_labels", [])
            b_labels = record.get("b_labels", [])
            a_type = [l for l in a_labels if l != "Entity"][0] if a_labels else "Entity"
            b_type = [l for l in b_labels if l != "Entity"][0] if b_labels else "Entity"
            
            # 避免重复
            rel_key = (a_name, rel_type, b_name)
            if rel_key in seen:
                continue
            seen.add(rel_key)
            
            # 构建关系描述
            if natural_language:
                # 使用自然语言描述
                rel_desc = self._relation_to_natural_language(
                    a_name, a_type, rel_type, b_name, b_type, rel_props
                )
            else:
                # 使用技术格式
                if rel_props:
                    props_str = ", ".join([f"{k}={v}" for k, v in rel_props.items() if k != "name"])
                    if props_str:
                        rel_desc = f"{a_name} ({a_type}) -[{rel_type} ({props_str})]-> {b_name} ({b_type})"
                    else:
                        rel_desc = f"{a_name} ({a_type}) -[{rel_type}]-> {b_name} ({b_type})"
                else:
                    rel_desc = f"{a_name} ({a_type}) -[{rel_type}]-> {b_name} ({b_type})"
            
            text_parts.append(rel_desc)
        
        if not text_parts:
            return "No relationships found."
        
        # 构建最终文本
        header = f"Relationships in the knowledge graph"
        if entity_name:
            header += f" (related to {entity_name})"
        if label:
            header += f" (label: {label})"
        if rel_types:
            header += f" (types: {', '.join(rel_types)})"
        
        return header + ":\n" + "\n".join([f"  - {rel}" for rel in text_parts])
    
    def get_entity_context_text(self, entity_name: str, label: str = "Entity", 
                               include_properties: bool = True,
                               natural_language: bool = True) -> str:
        """
        获取实体的完整上下文文本（包括属性和关系），用于LLM输入
        
        Args:
            entity_name: 实体名称
            label: 实体标签
            include_properties: 是否包含实体属性
        
        Returns:
            str: 实体上下文文本
        """
        # 获取实体信息
        entity = self.get_entity_by_name(entity_name, label)
        if not entity:
            return f"Entity {entity_name} not found."
        
        text_parts = [f"Entity: {entity_name}"]
        
        # 添加属性
        if include_properties:
            # entity 是字典格式
            if isinstance(entity, dict):
                props = {k: v for k, v in entity.items() if k != "name" and v is not None}
            else:
                props = dict(entity.items()) if hasattr(entity, 'items') else {}
            
            if props:
                props_text = ", ".join([f"{k}={v}" for k, v in props.items()])
                if props_text:
                    text_parts.append(f"Properties: {props_text}")
        
        # 添加关系
        relationships_text = self.get_entity_relationships_text(
            entity_name, label, max_depth=1, natural_language=natural_language
        )
        if relationships_text and "has no relationships" not in relationships_text:
            text_parts.append("\n" + relationships_text)
        
        return "\n".join(text_parts)

