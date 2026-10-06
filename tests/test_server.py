import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.store import BoardStore


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "board.sqlite3"


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path)) as c:
        yield c


def test_empty_board(client):
    board = client.get("/board").json()
    assert board == {"width": 50, "height": 50, "terrain": [], "entities": []}


def test_create_move_delete_entity(client):
    r = client.post("/entities", json={
        "name": "ニンジャA", "x": 3, "y": 4, "stats": {"hp": 10}})
    assert r.status_code == 201
    e = r.json()
    assert (e["x"], e["y"], e["stats"]) == (3, 4, {"hp": 10})

    r = client.patch(f"/entities/{e['id']}", json={"x": 10, "y": 11})
    assert r.status_code == 200
    assert (r.json()["x"], r.json()["y"]) == (10, 11)

    assert client.delete(f"/entities/{e['id']}").status_code == 204
    assert client.get(f"/entities/{e['id']}").status_code == 404


def test_rejects_out_of_bounds_and_occupied(client):
    assert client.post("/entities", json={"x": 50, "y": 0}).status_code == 409
    a = client.post("/entities", json={"x": 1, "y": 1}).json()
    b = client.post("/entities", json={"x": 2, "y": 1}).json()
    assert client.post("/entities", json={"x": 1, "y": 1}).status_code == 409
    r = client.patch(f"/entities/{b['id']}", json={"x": 1, "y": 1})
    assert r.status_code == 409
    # 自分のいるマスへの「移動」は許可
    r = client.patch(f"/entities/{a['id']}", json={"x": 1, "y": 1})
    assert r.status_code == 200


def test_rejects_bad_color(client):
    r = client.post("/entities", json={"x": 0, "y": 0, "color": "red"})
    assert r.status_code == 422


def test_paint_terrain(client):
    r = client.put("/terrain", json={"cells": [
        {"x": 0, "y": 0, "terrain": "wall"},
        {"x": 1, "y": 0, "terrain": "water"}]})
    assert len(r.json()["cells"]) == 2
    client.put("/terrain", json={"cells": [{"x": 1, "y": 0, "terrain": None}]})
    assert client.get("/board").json()["terrain"] == [
        {"x": 0, "y": 0, "terrain": "wall"}]
    r = client.put("/terrain", json={"cells": [
        {"x": 0, "y": 0, "terrain": "lava"}]})
    assert r.status_code == 422


def test_state_persists(db_path):
    with TestClient(create_app(db_path)) as c:
        c.post("/entities", json={"name": "A", "x": 5, "y": 5,
                                  "tags": ["npc"]})
        c.put("/terrain", json={"cells": [{"x": 2, "y": 2,
                                           "terrain": "high"}]})
    store = BoardStore(str(db_path))
    snap = store.snapshot()
    assert snap["entities"][0]["name"] == "A"
    assert snap["entities"][0]["tags"] == ["npc"]
    assert snap["terrain"] == [{"x": 2, "y": 2, "terrain": "high"}]


def test_logs_record_actions(client):
    e = client.post("/entities", json={"x": 0, "y": 0}).json()
    client.patch(f"/entities/{e['id']}", json={"x": 1})
    kinds = [log["kind"] for log in client.get("/logs").json()]
    assert kinds == ["entity.create", "entity.update"]


def test_websocket_broadcasts_to_all(client):
    with client.websocket_connect("/ws") as a, \
            client.websocket_connect("/ws") as b:
        assert a.receive_json()["type"] == "snapshot"
        assert a.receive_json() == {"type": "presence", "clients": 1}
        assert a.receive_json() == {"type": "presence", "clients": 2}
        assert b.receive_json()["type"] == "snapshot"
        assert b.receive_json() == {"type": "presence", "clients": 2}

        a.send_json({"op": "entity.create", "data": {"x": 7, "y": 8}})
        for ws in (a, b):
            msg = ws.receive_json()
            assert msg["type"] == "entity.created"
            assert (msg["entity"]["x"], msg["entity"]["y"]) == (7, 8)

        # REST での変更も WebSocket へ流れる(外部プロセス=NPC からの操作)
        eid = msg["entity"]["id"]
        client.patch(f"/entities/{eid}", json={"x": 9})
        for ws in (a, b):
            assert ws.receive_json()["entity"]["x"] == 9


def test_websocket_error_resends_snapshot(client):
    client.post("/entities", json={"x": 0, "y": 0})
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        ws.receive_json()  # presence
        ws.send_json({"op": "entity.create", "data": {"x": 0, "y": 0}})
        assert ws.receive_json()["type"] == "error"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"op": "nonsense"})
        assert ws.receive_json() == {"type": "error",
                                     "message": "invalid message"}
