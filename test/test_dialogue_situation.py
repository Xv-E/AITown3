"""NPC 回话时知道自己的处境：在忙什么、缺什么、挂着什么委托、店里缺什么。"""

from __future__ import annotations


class World:
    def run_query(self, q, **k):
        if "p.doing" in q:
            return [{"doing": "get some fish (because Routine: fish off the pier)", "coins": 5, "trader": False}]
        if "Request {status: 'open'}" in q:
            return [{"r": {"id": "Pip-fish-1", "poster": "Pip", "kind": "fish", "reward": 4, "reason": "no rod"}}]
        return []


def test_the_reply_knows_what_the_speaker_needs():
    import dialogue_service as ds

    brief = ds.situation_brief(World(), "Pip")
    assert "Right now you are trying to get some fish" in brief
    assert "You NEED fish" in brief and "pay 4 coins" in brief
    assert ds.situation_brief(World(), "") == ""
