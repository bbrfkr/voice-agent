"""FastAPI アプリ本体。

エンドポイント:
  GET  /                : Web UI（server/static/index.html）
  POST /api/transcribe  : 音声ファイル → 文字起こし（再利用可能な STT 単体 API）
  WS   /ws              : マイク発話 → STT → LLM → TTS のオーケストレーション

音声の入出力はすべてブラウザが担う（録音=getUserMedia / 再生=AudioContext）ため、
サーバは PortAudio/ALSA/PulseAudio に一切触れない。ブロッキング処理（faster-whisper・
requests ストリーミング・VOICEVOX 合成）はワーカースレッドで実行し、結果は asyncio.Queue
経由で WebSocket 送信する。バージインは接続から送られる cancel で threading.Event を立てる。
"""

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config as C
from core.discord_log import DiscordLogger
from core.llm import llm_stream
from core.opencode import OpenCode
from core.orchestrator import TtsSink, run_turn
from core.sessions import SessionStore
from core.stt import WhisperService
from core.tts import VoicevoxClient

# 起動時に 1 度だけ作る共有サービス（モデル・クライアント）
SVC: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    print("モデル読み込み中…")
    stt = WhisperService()
    voicevox = VoicevoxClient()
    dlog = DiscordLogger()
    # 会話セッションのディスク永続化（SESSION_STORE_DIR が空なら無効＝メモリのみ）。
    store: SessionStore | None = None
    if C.SESSION_STORE_DIR:
        store = SessionStore(C.SESSION_STORE_DIR)
        SESSIONS.update(store.load_all())
        print(f"セッション {len(SESSIONS)} 件を復元しました（{C.SESSION_STORE_DIR}）")
    SVC.update(stt=stt, voicevox=voicevox, dlog=dlog, store=store)
    if C.WARMUP:
        print("ウォームアップ中…")
        await asyncio.to_thread(stt.warmup)
        await asyncio.to_thread(voicevox.warmup)
        await asyncio.to_thread(_warmup_llm)
        print("ウォームアップ完了")
    print(f"準備完了。http://localhost:{C.SERVER_PORT} を開いてください。")
    yield


def _warmup_llm() -> None:
    """system prompt を投げてプロンプト前処理を温める（最初のトークンで打ち切り）。"""
    try:
        messages = [
            {"role": "system", "content": C.SYSTEM_PROMPT},
            {"role": "user", "content": "ping"},
        ]
        for _ in llm_stream(messages):
            break
    except Exception as e:
        print(f"[warmup] LLM 失敗（無視）: {e}")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(C.STATIC_DIR, "index.html"))


@app.get("/api/speakers")
async def speakers() -> dict[str, Any]:
    """VOICEVOX の話者一覧（人が選べるラベル付き）と既定の話者IDを返す。

    Web UI はこれでドロップダウンを組み立てる。VOICEVOX へ到達できない場合は
    空リストを返し、UI 側はラベルなしでも既定IDで動けるようにしておく。
    """
    voicevox: VoicevoxClient = SVC["voicevox"]
    try:
        items = await asyncio.to_thread(voicevox.speakers)
    except Exception as e:
        print(f"[speakers] 取得失敗（無視）: {e}")
        items = []
    return {"speakers": items, "default": C.VOICEVOX_SPEAKER}


@app.post("/api/transcribe")
async def transcribe(file: UploadFile) -> dict[str, str]:
    """音声ファイル（webm/opus/wav/mp3 等）を文字起こしして返す素の STT API。"""
    data = await file.read()
    stt: WhisperService = SVC["stt"]
    text = await asyncio.to_thread(stt.transcribe, data)
    return {"text": text}


# アクティブな WebSocket 接続のリスト（リモート PTT トリガー用）
ACTIVE_WS_CONNECTIONS: set[WebSocket] = set()

# sid（セッションID）単位で会話状態を保持するメモリストア。
#   sid -> {"messages": [...], "opencode": OpenCode, "display": [表示用イベント...]}
# 同じ sid で再接続すれば、ブラウザのリロードをまたいで LLM の文脈と見た目を引き継ぐ。
# メモリ保持のためサーバを再起動すると消える（単一ユーザのローカル用途を想定）。
SESSIONS: dict[str, dict[str, Any]] = {}

# 履歴（リロード後の見た目復元）に残すイベント種別。
_DISPLAY_EVENT_TYPES = {"stt", "assistant", "task", "log_saved", "error"}
_DISPLAY_MAX = 400


def _record_display(display: list[dict[str, Any]], event: dict[str, Any]) -> None:
    """表示に出るイベントだけをセッションの履歴へ追記する（リロード時に再生する）。"""
    t = event.get("type")
    if t not in _DISPLAY_EVENT_TYPES:
        return
    if t == "stt" and not str(event.get("text", "")).strip():
        return  # 空の聞き取り結果は履歴に残さない
    if t == "task" and event.get("status") not in ("delegating", "error"):
        return
    display.append(event)
    if len(display) > _DISPLAY_MAX:
        del display[: len(display) - _DISPLAY_MAX]


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    ACTIVE_WS_CONNECTIONS.add(ws)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    # セッション（sid）単位で会話履歴・opencode セッション・表示ログを保持する。
    # 同じ sid なら接続をまたいで再利用し、リロード後も会話の文脈と見た目を引き継ぐ。
    # sid 未指定の旧クライアントは保存しない一時セッションで動く（後方互換）。
    sid = ws.query_params.get("sid") or ""
    session = SESSIONS.get(sid)
    if session is None:
        session = {
            "messages": [{"role": "system", "content": C.SYSTEM_PROMPT}],
            "opencode": OpenCode(),
            "display": [],
        }
        if sid:
            SESSIONS[sid] = session
    messages: list[dict[str, str]] = session["messages"]
    opencode: OpenCode = session["opencode"]
    display: list[dict[str, Any]] = session["display"]

    # 読み上げ設定は接続単位（フロントが接続時に config を送り直す）
    settings: dict[str, Any] = {"speaker": C.VOICEVOX_SPEAKER, "speed": C.VOICEVOX_SPEED}
    state: dict[str, Any] = {"cancel": None, "worker": None}

    store: SessionStore | None = SVC.get("store")

    def persist() -> None:
        """会話状態をディスクへ保存（無効時・一時セッション時は何もしない）。"""
        if store is not None and sid:
            try:
                store.save(sid, session)
            except OSError as e:
                print(f"[session save error] {e}")

    def emit(event: dict) -> None:
        _record_display(display, event)
        loop.call_soon_threadsafe(out_q.put_nowait, ("json", event))

    def emit_audio(seq: int, wav: bytes) -> None:
        loop.call_soon_threadsafe(out_q.put_nowait, ("json", {"type": "tts", "seq": seq, "format": "wav"}))
        loop.call_soon_threadsafe(out_q.put_nowait, ("bytes", wav))

    async def sender() -> None:
        while True:
            item = await out_q.get()
            if item is None:
                break
            kind, payload = item
            if kind == "json":
                await ws.send_json(payload)
            else:
                await ws.send_bytes(payload)

    def worker(audio: bytes, mode: str, cancel: threading.Event) -> None:
        stt: WhisperService = SVC["stt"]
        voicevox: VoicevoxClient = SVC["voicevox"]
        dlog: DiscordLogger = SVC["dlog"]
        try:
            text = stt.transcribe(audio)
            emit({"type": "stt", "text": text})
            if not text.strip():
                emit({"type": "turn_end"})
                return
            print(f"あなた: {text}")
            if mode == "log":
                # ログモード: LLM/TTS を挟まず STT 結果をそのまま Discord へ直送
                if dlog.log_enabled:
                    dlog.log(text)
                    emit({"type": "log_saved", "text": text})
                else:
                    emit({"type": "error", "message": "ログモードの送信先（Webhook）が未設定です"})
                emit({"type": "turn_end"})
                return
            dlog.user(text)
            tts = TtsSink(voicevox, emit_audio, cancel, speaker=settings["speaker"], speed=settings["speed"])
            try:
                run_turn(text, messages, opencode=opencode, tts=tts, dlog=dlog, emit=emit, cancel=cancel)
            finally:
                tts.close()
            emit({"type": "turn_end"})
        except Exception as e:
            print(f"[turn error] {e}")
            emit({"type": "error", "message": str(e)})
            emit({"type": "turn_end"})
        finally:
            persist()  # ターン後に会話状態をディスクへ保存（再起動後の復元用）

    def start_turn(audio: bytes, mode: str) -> None:
        # 進行中のターンがあればキャンセル（バージイン）してから新ターンを開始
        prev = state["cancel"]
        if isinstance(prev, threading.Event):
            prev.set()
        cancel = threading.Event()
        state["cancel"] = cancel
        t = threading.Thread(target=worker, args=(audio, mode, cancel), daemon=True)
        state["worker"] = t
        t.start()

    send_task = asyncio.create_task(sender())

    # 既存セッションがあれば、過去の会話を履歴として送り見た目を復元させる。
    # 新規イベントより前に届くよう out_q（FIFO）へ先に積む。
    if display:
        await out_q.put(("json", {"type": "history", "events": list(display)}))

    pending_mode = "chat"
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "utterance":
                    pending_mode = data.get("mode", "chat")
                elif mtype == "cancel":
                    cur = state["cancel"]
                    if isinstance(cur, threading.Event):
                        cur.set()
                elif mtype == "clear":
                    # 会話履歴・表示ログ・opencode セッションを破棄してまっさらに戻す。
                    cur = state["cancel"]
                    if isinstance(cur, threading.Event):
                        cur.set()
                    messages[:] = [{"role": "system", "content": C.SYSTEM_PROMPT}]
                    display.clear()
                    opencode.session_id = None
                    if store is not None and sid:
                        store.delete(sid)
                    # 空の履歴を送ってブラウザ側の表示もクリアさせる。
                    await out_q.put(("json", {"type": "history", "events": []}))
                elif mtype == "config":
                    if "speaker" in data:
                        settings["speaker"] = int(data["speaker"])
                    if "speed" in data:
                        settings["speed"] = float(data["speed"])
            elif msg.get("bytes") is not None:
                start_turn(msg["bytes"], pending_mode)
                pending_mode = "chat"
    except WebSocketDisconnect:
        pass
    finally:
        ACTIVE_WS_CONNECTIONS.discard(ws)
        cur = state["cancel"]
        if isinstance(cur, threading.Event):
            cur.set()
        await out_q.put(None)
        await send_task


async def _broadcast_ws(message: dict[str, Any]) -> None:
    """接続中のすべてのブラウザクライアントへ WebSocket メッセージを送る。"""
    disconnected = []
    for ws in ACTIVE_WS_CONNECTIONS:
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.append(ws)

    for ws in disconnected:
        ACTIVE_WS_CONNECTIONS.discard(ws)


@app.post("/api/remote-ptt")
async def remote_ptt(state: str) -> dict[str, str]:
    """外部スクリプトなどから PTT をトリガーする API。

    `state` には "start" または "stop" を指定します。
    接続中のすべてのブラウザクライアントに対して、WebSocket 経由で録音の開始/停止を指示します。
    """
    if state not in ("start", "stop"):
        return {"status": "error", "message": "state must be 'start' or 'stop'"}

    await _broadcast_ws({"type": "remote_ptt", "action": state})
    return {"status": "ok"}


@app.post("/api/remote-logmode")
async def remote_logmode(state: str = "toggle") -> dict[str, str]:
    """外部スクリプトなどからログモードの ON/OFF を切り替える API。

    `state` には "toggle"（既定）/"on"/"off" を指定します。
    接続中のすべてのブラウザクライアントに対して、WebSocket 経由でログモードの切り替えを指示します。
    実際のログモード状態はブラウザ側（チェックボックス）が保持します。
    """
    if state not in ("toggle", "on", "off"):
        return {"status": "error", "message": "state must be 'toggle', 'on' or 'off'"}

    await _broadcast_ws({"type": "remote_logmode", "action": state})
    return {"status": "ok"}


@app.post("/api/remote-vad")
async def remote_vad(state: str = "toggle") -> dict[str, str]:
    """外部スクリプトなどから自動音声検出（VAD）の ON/OFF を切り替える API。

    `state` には "toggle"（既定）/"on"/"off" を指定します。
    接続中のすべてのブラウザクライアントに対して、WebSocket 経由で VAD の切り替えを指示します。
    実際の VAD 状態はブラウザ側（チェックボックス）が保持します。
    """
    if state not in ("toggle", "on", "off"):
        return {"status": "error", "message": "state must be 'toggle', 'on' or 'off'"}

    await _broadcast_ws({"type": "remote_vad", "action": state})
    return {"status": "ok"}


# 静的ファイル（Web UI）。API/WS ルートの後にマウントして "/" 配下を配信する。
app.mount("/", StaticFiles(directory=C.STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=C.SERVER_HOST, port=C.SERVER_PORT)


if __name__ == "__main__":
    main()
