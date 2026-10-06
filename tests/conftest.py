import os

# server.main の import 時に作られるアプリが data/ を汚さないようにする
os.environ.setdefault("BOARD_DB", ":memory:")
