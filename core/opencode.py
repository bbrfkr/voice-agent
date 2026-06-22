"""opencode serve への作業委譲（[[TASK]] 用）。

公式の opencode Python SDK（`opencode-ai`）を使う。旧版は自前の requests 呼び出しで
応答 JSON を再帰的に総なめして `type=="text"` を全部かき集めていたが、それだと
思考(reasoning)・ツール(tool)・送信したプロンプトのエコーまで拾ってしまい、要約 LLM に
渡るテキストがノイズだらけになっていた。SDK の型付き part を使い、**assistant ロールの
非 synthetic な TextPart だけ**を取り出すことで解釈精度を上げる。

`run(instruction, on_progress=...)` の公開シグネチャと `session_id` の永続化は旧版と
ほぼ同一に保つ（orchestrator / sessions / app 側は最小改修）。`on_progress` を渡すと、
chat() がブロッキングしている裏で `/event`（SSE）を購読し、ツール利用などの進捗を
コールバックへ流す。
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import requests

import config as C

if TYPE_CHECKING:
    from opencode_ai import Opencode

# 進捗コールバック（ツール名・状態・タイトルの辞書を受け取る）。
ProgressCb = Callable[[dict], None]


class OpenCode:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self._client: Opencode | None = None

    def _get_client(self) -> Opencode:
        """SDK クライアントを遅延生成する（dev 環境に runtime 依存を入れないため、
        モジュール import 時ではなく実呼び出し時に opencode_ai を読み込む）。"""
        if self._client is None:
            from opencode_ai import Opencode

            self._client = Opencode(base_url=C.OPENCODE_BASE_URL)
        return self._client

    def _ensure_session(self) -> str:
        if not self.session_id:
            session = self._get_client().session.create()
            self.session_id = session.id
        return self.session_id

    def run(self, instruction: str, on_progress: ProgressCb | None = None) -> str:
        """opencode に作業を投げ、応答テキストを返す（同期）。

        on_progress を渡すと、chat() の実行中に裏で `/event`（SSE）を購読し、
        ツール利用などの進捗を on_progress(dict) で逐次通知する。
        """
        client = self._get_client()
        sid = self._ensure_session()

        # 進捗購読スレッド（chat がブロッキングしている裏で /event を読む）。
        stop = threading.Event()
        holder: dict[str, Any] = {}
        pump: threading.Thread | None = None
        if on_progress is not None:
            pump = threading.Thread(
                target=self._pump_events, args=(sid, on_progress, stop, holder), daemon=True
            )
            pump.start()

        try:
            assistant = client.session.chat(
                sid,
                provider_id=C.OPENCODE_PROVIDER_ID,
                model_id=C.OPENCODE_MODEL_ID,
                parts=[{"type": "text", "text": instruction}],
                timeout=600,
            )
        finally:
            stop.set()
            resp = holder.get("resp")
            if resp is not None:
                with contextlib.suppress(Exception):
                    resp.close()  # iter_lines のブロックを解除させる
            if pump is not None:
                pump.join(timeout=2)

        # chat() の戻り値はメッセージのメタ情報のみで parts を含まない版があるため、
        # 確定したメッセージ一覧から（可能なら id 一致で）本文 part を取り出す。
        return self._collect_assistant_text(sid, getattr(assistant, "id", None))

    def _pump_events(self, sid: str, on_progress: ProgressCb, stop: threading.Event, holder: dict[str, Any]) -> None:
        """/event（SSE）を購読し、このセッションのツール進捗を on_progress へ流す。

        SDK の型付き Stream は判別 Union で全イベントを厳格パースするため、サーバが流す
        多種のイベントに SDK の型が1つでも追従できないと反復ごと例外で止まり、以降の進捗が
        途切れる。そこで生の SSE を requests で読み、dict でフィールド参照する（未知イベントが
        来ても落ちず、版差にも強い）。例外は握りつぶす（進捗は補助情報で本処理は継続させる）。"""
        try:
            resp = requests.get(f"{C.OPENCODE_BASE_URL}/event", stream=True, timeout=(10, None))
            resp.raise_for_status()
        except Exception:
            return
        holder["resp"] = resp
        seen: set[tuple[str, str]] = set()
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if stop.is_set():
                    break
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                self._emit_tool_progress(event, sid, seen, on_progress)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                resp.close()

    @staticmethod
    def _emit_tool_progress(
        event: Any, sid: str, seen: set[tuple[str, str]], on_progress: ProgressCb
    ) -> None:
        """message.part.updated のうち、当該セッションのツール part の状態変化を通知する。
        生 JSON（dict）を受け取る。フィールド名はサーバの JSON 準拠（sessionID 等のキャメル）。"""
        if not isinstance(event, dict) or event.get("type") != "message.part.updated":
            return
        properties = event.get("properties")
        part = properties.get("part") if isinstance(properties, dict) else None
        if not isinstance(part, dict) or part.get("sessionID") != sid:
            return
        if part.get("type") != "tool":
            return
        state = part.get("state") or {}
        status = state.get("status")
        if status not in ("running", "completed", "error"):
            return
        # 同じ part の同じ状態は1回だけ（part.updated は何度も飛ぶため重複排除）。
        key = (str(part.get("id", "")), str(status))
        if key in seen:
            return
        seen.add(key)
        on_progress(
            {
                "tool": part.get("tool") or "",
                "state": status,
                "title": state.get("title") or "",
            }
        )

    def _collect_assistant_text(self, sid: str, message_id: str | None) -> str:
        """セッションのメッセージ一覧から assistant の本文テキストを取り出す。

        - assistant ロール以外（user 等）は無視する。
        - reasoning / tool / step などの非 text part は型で自然に除外される。
        - synthetic（自動挿入されるコンテキスト等）な text part は本文ではないので捨てる。
        - message_id が一致するメッセージがあればそれを、無ければ最後の assistant を採る
          （chat() の戻り値が id を持たない版への保険）。
        """
        client = self._get_client()
        items = client.session.messages(sid)

        target: Any = None
        last_assistant: Any = None
        for item in items:
            if getattr(item.info, "role", None) != "assistant":
                continue
            last_assistant = item
            if message_id and getattr(item.info, "id", None) == message_id:
                target = item
        chosen = target or last_assistant
        if chosen is None:
            return "(応答が空でした)"

        texts: list[str] = []
        for part in chosen.parts:
            if getattr(part, "type", None) != "text" or getattr(part, "synthetic", False):
                continue
            text = (getattr(part, "text", "") or "").strip()
            if text:
                texts.append(text)
        return "\n".join(texts).strip() or "(応答が空でした)"
