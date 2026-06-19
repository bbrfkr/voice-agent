"""会話セッションのディスク永続化。

Web UI の会話状態（LLM 履歴・表示ログ・opencode セッションID）を sid 単位で
JSON ファイル（`<sid>.json`）に保存し、サーバ再起動後に復元する。これにより
ブラウザのリロードだけでなく、サーバの再起動をまたいでも会話を引き継げる。

`SESSION_STORE_DIR` が空のときは呼び出し側でこのストアを作らず、メモリのみで運用する。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from typing import Any

from core.opencode import OpenCode

# sid はクライアント生成のためファイル名に使う前に検証する（パストラバーサル防止）。
# UUID（英数字・ハイフン）を想定。安全でない sid は永続化対象外（メモリのみ）にする。
_SAFE_SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class SessionStore:
    """sid ごとに 1 ファイル（`<sid>.json`）で会話状態を保存・復元する。"""

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self._lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, sid: str) -> str | None:
        if not _SAFE_SID.match(sid):
            return None
        return os.path.join(self.dir, f"{sid}.json")

    def load_all(self) -> dict[str, dict[str, Any]]:
        """ディレクトリ内の全セッションを読み込み {sid: session} を返す。"""
        sessions: dict[str, dict[str, Any]] = {}
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return sessions
        for name in names:
            if not name.endswith(".json"):
                continue
            sid = name[:-5]
            if not _SAFE_SID.match(sid):
                continue
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue  # 壊れた / 読めないファイルは無視
            sessions[sid] = _deserialize(data)
        return sessions

    def save(self, sid: str, session: dict[str, Any]) -> None:
        """セッションを一時ファイルへ書いてから原子的に差し替える。"""
        path = self._path(sid)
        if path is None:
            return
        data = _serialize(session)
        tmp = f"{path}.tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)

    def delete(self, sid: str) -> None:
        """セッションファイルを削除する（履歴クリア用）。"""
        path = self._path(sid)
        if path is None:
            return
        with self._lock, contextlib.suppress(FileNotFoundError):
            os.remove(path)


def _serialize(session: dict[str, Any]) -> dict[str, Any]:
    opencode: OpenCode = session["opencode"]
    return {
        "messages": session["messages"],
        "display": session["display"],
        "opencode_session_id": opencode.session_id,
    }


def _deserialize(data: dict[str, Any]) -> dict[str, Any]:
    opencode = OpenCode()
    opencode.session_id = data.get("opencode_session_id")
    return {
        "messages": data.get("messages", []),
        "display": data.get("display", []),
        "opencode": opencode,
    }
