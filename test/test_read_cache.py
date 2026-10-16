"""Neo4jWorld.read_cache：同一 tick 内重复只读查询只打一次库，写查询让缓存失效。"""

from __future__ import annotations

import pytest


@pytest.fixture
def counted(world, monkeypatch):
    calls = []
    run = type(world)._run

    def spy(self, cypher, params):
        calls.append(cypher)
        return run(self, cypher, params)

    monkeypatch.setattr(type(world), "_run", spy)
    world.run_query("MERGE (:CacheProbe {name: 'probe'}) ")
    yield world, calls
    world.run_query("MATCH (n:CacheProbe) DETACH DELETE n")


READ = "MATCH (n:CacheProbe {name: $n}) RETURN n.v AS v"


def test_repeated_reads_hit_cache(counted):
    world, calls = counted
    calls.clear()
    with world.read_cache():
        a = world.run_query(READ, n="probe")
        b = world.run_query(READ, n="probe")
    assert a == b
    assert len(calls) == 1


def test_write_invalidates(counted):
    world, calls = counted
    with world.read_cache():
        world.run_query("MATCH (n:CacheProbe {name: 'probe'}) SET n.v = 1")
        assert world.run_query(READ, n="probe") == [{"v": 1}]
        world.run_query("MATCH (n:CacheProbe {name: 'probe'}) SET n.v = 2")
        assert world.run_query(READ, n="probe") == [{"v": 2}]


def test_callers_cannot_corrupt_cache(counted):
    world, _ = counted
    with world.read_cache():
        rows = world.run_query(READ, n="probe")
        rows[0]["v"] = "mutated"
        assert world.run_query(READ, n="probe")[0]["v"] != "mutated"


def test_no_caching_outside_scope(counted):
    world, calls = counted
    calls.clear()
    world.run_query(READ, n="probe")
    world.run_query(READ, n="probe")
    assert len(calls) == 2
