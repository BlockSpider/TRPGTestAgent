# TRPGTestAgent

四角マスTRPG向けの盤面サーバー(FastAPI)と、ブラウザ用の盤面UI。
接続している全員で同じ盤面を共有し、変更は WebSocket でリアルタイムに配信される。
設計方針は [CLAUDE.md](CLAUDE.md) を参照。

## セットアップ

Python 3.10 以上。venv はプロジェクト直下の `irc_space`。

```bash
python -m venv irc_space
source irc_space/bin/activate        # Windows: irc_space\Scripts\activate
pip install -r requirements-dev.txt  # 本番だけなら requirements.txt
```

## 起動

```bash
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

ブラウザで `http://<ホスト>:8000/` を開く。盤面は `data/board.sqlite3` に保存される
(環境変数 `BOARD_DB` で変更可)。API の一覧は `/docs`。

## UI の操作

- **コマ**モード: 空きマスをクリックで配置、ドラッグで移動、右クリック/ダブルクリックで削除
- **地形**モード: パレット(床・壁・水・悪路・高台)を選んでドラッグで塗る。地形の「壁」はマスごと占有する厚い壁
- **仕切り**モード: マスの境目をドラッグして薄い仕切り(壁・窓・扉・扉(開))を引く。最初に拾った向きに沿って直線になる
- コマモードで扉をクリックすると開閉する
- 共通: 右ドラッグでスクロール、ホイールで拡大縮小

## API

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/board` | 盤面全体(サイズ・地形・エンティティ) |
| GET | `/entities/{id}` | エンティティ1件 |
| POST | `/entities` | 作成(`name`, `x`, `y`, `faction`, `color`, `stats`, `tags`) |
| PATCH | `/entities/{id}` | 部分更新。`x`/`y` を変えると移動(盤外・占有マスは 409) |
| DELETE | `/entities/{id}` | 削除 |
| PUT | `/terrain` | 地形を塗る `{"cells": [{"x", "y", "terrain"}]}`。`terrain: null` で床 |
| PUT | `/edges` | 仕切りを置く `{"edges": [{"x", "y", "dir", "edge"}]}`。`edge: null` で消す |
| GET | `/logs` | 行動ログ(新しい順に最大 `limit` 件を古い順で返す) |
| WS | `/ws` | 接続時に `snapshot`、以降は変更イベントを受信。操作も送れる |

仕切りはマス (x, y) の辺として表す。`dir: "h"` は上辺、`dir: "v"` は左辺。
盤の右端は `x == width` の `"v"`、下端は `y == height` の `"h"`。
種類は `wall`(壁)、`window`(窓)、`door`(扉・閉)、`door_open`(扉・開)。

REST での変更も WebSocket の全員に配信されるので、外部プロセス(NPC)の操作がそのまま UI に出る。

現時点でサーバーが検証するのは「盤内か」「他のコマがいないか」だけ。移動ルール(`legal_actions`)は今後追加する。

## テスト

```bash
pytest
```
