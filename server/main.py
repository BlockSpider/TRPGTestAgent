"""盤面サーバー: REST API + WebSocket による全員への変更配信.

起動: uvicorn server.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .store import BoardError, BoardStore, NotFound

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DEFAULT_DB = ROOT / "data" / "board.sqlite3"

Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Name = Annotated[str, Field(max_length=32)]
Terrain = Optional[Literal["wall", "water", "rough", "high"]]


# ---- リクエストの型 ----

class EntityCreate(BaseModel):
    name: Name = ""
    x: int
    y: int
    faction: Name = ""
    color: Color = "#e5484d"
    stats: dict = {}
    tags: list[str] = []


class EntityUpdate(BaseModel):
    name: Optional[Name] = None
    x: Optional[int] = None
    y: Optional[int] = None
    faction: Optional[Name] = None
    color: Optional[Color] = None
    stats: Optional[dict] = None
    tags: Optional[list[str]] = None


class Cell(BaseModel):
    x: int
    y: int
    terrain: Terrain


class TerrainPaint(BaseModel):
    cells: list[Cell] = Field(max_length=2500)


# WebSocket でクライアントから届く操作
class CreateOp(BaseModel):
    op: Literal["entity.create"]
    data: EntityCreate


class UpdateOp(BaseModel):
    op: Literal["entity.update"]
    id: int
    data: EntityUpdate


class DeleteOp(BaseModel):
    op: Literal["entity.delete"]
    id: int


class PaintOp(BaseModel):
    op: Literal["terrain.paint"]
    data: TerrainPaint


ClientOp = TypeAdapter(Annotated[
    Union[CreateOp, UpdateOp, DeleteOp, PaintOp],
    Field(discriminator="op")])


# ---- 接続管理 ----

class Hub:
    """接続中の WebSocket を束ね、イベントを全員へ配る."""

    def __init__(self):
        self.clients = set()

    async def broadcast(self, message):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def broadcast_presence(self):
        await self.broadcast({"type": "presence", "clients": len(self.clients)})


def create_app(db_path=None):
    if db_path is None:
        db_path = os.environ.get("BOARD_DB", DEFAULT_DB)
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="TRPG Board")
    store = BoardStore(str(db_path))
    hub = Hub()
    lock = asyncio.Lock()
    app.state.store = store
    app.state.hub = hub

    # REST と WebSocket で共通の変更処理。成功したら全員へ配信する。
    async def create_entity(body: EntityCreate):
        async with lock:
            e = store.create_entity(**body.model_dump())
        await hub.broadcast({"type": "entity.created", "entity": asdict(e)})
        return e

    async def update_entity(entity_id: int, body: EntityUpdate):
        async with lock:
            e = store.update_entity(entity_id, **body.model_dump(
                exclude_none=True))
        await hub.broadcast({"type": "entity.updated", "entity": asdict(e)})
        return e

    async def delete_entity(entity_id: int):
        async with lock:
            store.delete_entity(entity_id)
        await hub.broadcast({"type": "entity.deleted", "id": entity_id})

    async def paint(body: TerrainPaint):
        async with lock:
            applied = store.paint(
                [(c.x, c.y, c.terrain) for c in body.cells])
        if applied:
            await hub.broadcast({"type": "terrain.painted", "cells": applied})
        return applied

    def http_error(exc):
        status = 404 if isinstance(exc, NotFound) else 409
        return HTTPException(status_code=status, detail=str(exc))

    # ---- REST ----

    @app.get("/board")
    def get_board():
        return store.snapshot()

    @app.get("/entities/{entity_id}")
    def get_entity(entity_id: int):
        try:
            return asdict(store.get_entity(entity_id))
        except BoardError as exc:
            raise http_error(exc)

    @app.post("/entities", status_code=201)
    async def post_entity(body: EntityCreate):
        try:
            return asdict(await create_entity(body))
        except BoardError as exc:
            raise http_error(exc)

    @app.patch("/entities/{entity_id}")
    async def patch_entity(entity_id: int, body: EntityUpdate):
        try:
            return asdict(await update_entity(entity_id, body))
        except BoardError as exc:
            raise http_error(exc)

    @app.delete("/entities/{entity_id}", status_code=204)
    async def remove_entity(entity_id: int):
        try:
            await delete_entity(entity_id)
        except BoardError as exc:
            raise http_error(exc)

    @app.put("/terrain")
    async def put_terrain(body: TerrainPaint):
        try:
            return {"cells": await paint(body)}
        except BoardError as exc:
            raise http_error(exc)

    @app.get("/logs")
    def get_logs(limit: int = 50):
        return store.recent_logs(max(1, min(limit, 500)))

    # ---- WebSocket ----

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        hub.clients.add(ws)
        await ws.send_json({"type": "snapshot", "board": store.snapshot()})
        await hub.broadcast_presence()
        try:
            while True:
                raw = await ws.receive_json()
                try:
                    op = ClientOp.validate_python(raw)
                    if isinstance(op, CreateOp):
                        await create_entity(op.data)
                    elif isinstance(op, UpdateOp):
                        await update_entity(op.id, op.data)
                    elif isinstance(op, DeleteOp):
                        await delete_entity(op.id)
                    elif isinstance(op, PaintOp):
                        await paint(op.data)
                except (BoardError, ValidationError) as exc:
                    # 楽観的に更新したクライアントを正しい状態へ戻す
                    message = (str(exc) if isinstance(exc, BoardError)
                               else "invalid message")
                    await ws.send_json({"type": "error", "message": message})
                    await ws.send_json(
                        {"type": "snapshot", "board": store.snapshot()})
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)
            await hub.broadcast_presence()

    # ---- UI ----

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
