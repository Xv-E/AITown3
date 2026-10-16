"""服务端第一次拿到空数据库时自动建主世界（营地）；已有世界不动。"""

from __future__ import annotations

import pytest


@pytest.fixture
def server(world, monkeypatch):
    import server as server_module

    monkeypatch.setattr(server_module, "_world", None)
    return server_module


def test_empty_database_gets_the_main_world(server, world):
    world.clear_all()
    w = server.get_world()
    assert set(w.list_entities("Location")) == {"CampfireClearing", "Forest", "BerryGrove", "RiverBank", "Shelter"}
    assert w.run_query("MATCH (p:Person {name: 'Observer'}) RETURN p.x AS x")[0]["x"] is not None


def test_an_existing_world_is_left_alone(server, world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("key_and_door"), clear=True)
    w = server.get_world()
    assert "Vault" in w.list_entities("Location")
