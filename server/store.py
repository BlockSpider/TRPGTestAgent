"""盤面(地形+エンティティ)の状態と SQLite への永続化."""

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field

TERRAIN_TYPES = ("wall", "water", "rough", "high")

# マスとマスの間(辺)に置く薄い仕切り
EDGE_TYPES = ("wall", "window", "door", "door_open")
# 辺の向き。"h" はマス (x, y) の上辺、"v" はマス (x, y) の左辺。
# 盤の右端・下端は x == width の "v"、y == height の "h" で表す。
EDGE_DIRS = ("h", "v")

SCHEMA = """
CREATE TABLE IF NOT EXISTS board (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    width INTEGER NOT NULL,
    height INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS terrain (
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    type TEXT NOT NULL,
    PRIMARY KEY (x, y)
);
CREATE TABLE IF NOT EXISTS edges (
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    dir TEXT NOT NULL,
    type TEXT NOT NULL,
    PRIMARY KEY (x, y, dir)
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    faction TEXT NOT NULL,
    color TEXT NOT NULL,
    stats TEXT NOT NULL,
    tags TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class BoardError(Exception):
    """盤面に対して不正な操作."""


class NotFound(BoardError):
    """存在しないエンティティを指定した."""


@dataclass
class Entity:
    id: int
    name: str
    x: int
    y: int
    faction: str = ""
    color: str = "#e5484d"
    stats: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)


class BoardStore:
    """盤面の状態をメモリに持ち、変更のたびに SQLite へ書き込む."""

    def __init__(self, path, width=50, height=50):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        row = self.conn.execute(
            "SELECT width, height FROM board WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO board (id, width, height) VALUES (1, ?, ?)",
                (width, height))
            self.conn.commit()
            row = (width, height)
        self.width, self.height = row

        self.terrain = {
            (x, y): t for x, y, t in
            self.conn.execute("SELECT x, y, type FROM terrain")}
        self.edges = {
            (x, y, d): t for x, y, d, t in
            self.conn.execute("SELECT x, y, dir, type FROM edges")}
        self.entities = {}
        for r in self.conn.execute(
                "SELECT id, name, x, y, faction, color, stats, tags "
                "FROM entities"):
            e = Entity(*r[:6], stats=json.loads(r[6]), tags=json.loads(r[7]))
            self.entities[e.id] = e

    # ---- 参照 ----

    def snapshot(self):
        return {
            "width": self.width,
            "height": self.height,
            "terrain": [
                {"x": x, "y": y, "terrain": t}
                for (x, y), t in sorted(self.terrain.items())],
            "edges": [
                {"x": x, "y": y, "dir": d, "edge": t}
                for (x, y, d), t in sorted(self.edges.items())],
            "entities": [asdict(e) for e in self.entities.values()],
        }

    def get_entity(self, entity_id):
        try:
            return self.entities[entity_id]
        except KeyError:
            raise NotFound(f"entity {entity_id} not found") from None

    def entity_at(self, x, y):
        for e in self.entities.values():
            if e.x == x and e.y == y:
                return e
        return None

    def recent_logs(self, limit=50):
        rows = self.conn.execute(
            "SELECT id, ts, kind, payload FROM logs "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"id": i, "ts": ts, "kind": k, "payload": json.loads(p)}
            for i, ts, k, p in reversed(rows)]

    # ---- 変更 ----

    def create_entity(self, name, x, y, faction="", color="#e5484d",
                      stats=None, tags=None):
        self._check_free(x, y)
        stats = stats or {}
        tags = tags or []
        cur = self.conn.execute(
            "INSERT INTO entities (name, x, y, faction, color, stats, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, x, y, faction, color, json.dumps(stats),
             json.dumps(tags)))
        e = Entity(cur.lastrowid, name, x, y, faction, color, stats, tags)
        self.entities[e.id] = e
        self._log("entity.create", asdict(e))
        return e

    def update_entity(self, entity_id, **changes):
        """指定フィールドだけ書き換える。x/y の変更は移動として検証する."""
        e = self.get_entity(entity_id)
        x = changes.get("x", e.x)
        y = changes.get("y", e.y)
        if (x, y) != (e.x, e.y):
            self._check_free(x, y, ignore=e)
        for key, value in changes.items():
            setattr(e, key, value)
        self.conn.execute(
            "UPDATE entities SET name = ?, x = ?, y = ?, faction = ?, "
            "color = ?, stats = ?, tags = ? WHERE id = ?",
            (e.name, e.x, e.y, e.faction, e.color, json.dumps(e.stats),
             json.dumps(e.tags), e.id))
        self._log("entity.update", {"id": e.id, **changes})
        return e

    def delete_entity(self, entity_id):
        e = self.get_entity(entity_id)
        del self.entities[entity_id]
        self.conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        self._log("entity.delete", {"id": entity_id, "name": e.name})

    def paint(self, cells):
        """cells: (x, y, terrain) の列。terrain が None なら床に戻す."""
        applied = []
        for x, y, t in cells:
            self._check_bounds(x, y)
            if t is not None and t not in TERRAIN_TYPES:
                raise BoardError(f"unknown terrain: {t}")
            if self.terrain.get((x, y)) == t:
                continue
            if t is None:
                self.terrain.pop((x, y), None)
                self.conn.execute(
                    "DELETE FROM terrain WHERE x = ? AND y = ?", (x, y))
            else:
                self.terrain[(x, y)] = t
                self.conn.execute(
                    "INSERT OR REPLACE INTO terrain (x, y, type) "
                    "VALUES (?, ?, ?)", (x, y, t))
            applied.append({"x": x, "y": y, "terrain": t})
        if applied:
            self._log("terrain.paint", {"cells": applied})
        else:
            self.conn.commit()
        return applied

    def paint_edges(self, edges):
        """edges: (x, y, dir, type) の列。type が None なら仕切りを消す."""
        applied = []
        for x, y, d, t in edges:
            self._check_edge(x, y, d)
            if t is not None and t not in EDGE_TYPES:
                raise BoardError(f"unknown edge type: {t}")
            if self.edges.get((x, y, d)) == t:
                continue
            if t is None:
                self.edges.pop((x, y, d), None)
                self.conn.execute(
                    "DELETE FROM edges WHERE x = ? AND y = ? AND dir = ?",
                    (x, y, d))
            else:
                self.edges[(x, y, d)] = t
                self.conn.execute(
                    "INSERT OR REPLACE INTO edges (x, y, dir, type) "
                    "VALUES (?, ?, ?, ?)", (x, y, d, t))
            applied.append({"x": x, "y": y, "dir": d, "edge": t})
        if applied:
            self._log("edges.paint", {"edges": applied})
        else:
            self.conn.commit()
        return applied

    # ---- 内部 ----

    def _check_edge(self, x, y, d):
        if d not in EDGE_DIRS:
            raise BoardError(f"unknown edge dir: {d}")
        max_x = self.width if d == "v" else self.width - 1
        max_y = self.height if d == "h" else self.height - 1
        if not (0 <= x <= max_x and 0 <= y <= max_y):
            raise BoardError(f"edge ({x}, {y}, {d}) is outside the board")

    def _check_bounds(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise BoardError(f"({x}, {y}) is outside the board")

    def _check_free(self, x, y, ignore=None):
        self._check_bounds(x, y)
        other = self.entity_at(x, y)
        if other is not None and other is not ignore:
            raise BoardError(f"({x}, {y}) is occupied by {other.name!r}")

    def _log(self, kind, payload):
        self.conn.execute(
            "INSERT INTO logs (ts, kind, payload) VALUES (?, ?, ?)",
            (time.time(), kind, json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()
