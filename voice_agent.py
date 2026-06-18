"""
音声エージェント本体（Discord ボイスチャンネル版・共通ロジック）。

このモジュールはオーディオ I/O に依存しない再利用部品を提供する:
  STT(リモート)  : transcribe()             … OpenAI 互換 /audio/transcriptions を叩く
  会話 LLM       : llm_stream() / handle_turn()  … ストリーミング生成・文単位フラッシュ
  作業委譲       : OpenCode                  … [[TASK]] を opencode serve に委譲
  TTS            : Speaker                   … VOICEVOX 合成 → 再生プレイヤへ PCM を渡す
  会話ログ       : DiscordLogger             … Discord Webhook へ投げ捨て式で送信
  ログモード照合 : _match_log_command()      … STT テキストでログモードを切替

マイク/スピーカーの入出力と bot の常駐は discord_agent.py（asyncio）が担う。
設定は config.py（環境変数 / `.env` から読み込むローダ）に分離。値の編集は `.env` で行う。
"""

import io
import json
import queue
import re
import sys
import threading
import time
import unicodedata

import numpy as np
import requests
import soundfile as sf

import config as C

SAMPLE_RATE = 16000  # STT 用（16kHz mono）
DISCORD_RATE = 48000  # Discord の音声は 48kHz
DISCORD_CHANNELS = 2  # Discord の音声は stereo

# 文の区切り（ここで TTS に流す単位を切る）
_SENT_BOUNDARY = re.compile(r"[。．！？!?\n]")
# 早出し用の緩い区切り（読点を含む）。応答の1文目だけここで先に喋り出す。
_SOFT_BOUNDARY = re.compile(r"[、，,。．！？!?\n]")
_TASK_SENTINEL = "[[TASK]]"


# ───────────────────────────────── 音声フォーマット変換 ─────────────────────────────────
def to_discord_pcm(data: np.ndarray, sr: int) -> bytes:
    """合成音声（float32, mono または stereo, 任意サンプルレート）を
    Discord 再生用の 48kHz / stereo / 16bit little-endian PCM に変換する。"""
    a = np.asarray(data, dtype=np.float32)
    if a.ndim == 2:
        a = a.mean(axis=1)  # stereo → mono
    if sr != DISCORD_RATE and a.size > 1:
        n = int(round(a.size * DISCORD_RATE / sr))
        if n > 0:
            xp = np.linspace(0.0, 1.0, a.size, endpoint=False)
            xi = np.linspace(0.0, 1.0, n, endpoint=False)
            a = np.interp(xi, xp, a).astype(np.float32)
    a = np.clip(a, -1.0, 1.0)
    i16 = (a * 32767.0).astype("<i2")
    stereo = np.repeat(i16[:, None], DISCORD_CHANNELS, axis=1)
    return bytes(stereo.tobytes())


def discord_pcm_to_16k_mono(pcm: bytes) -> np.ndarray:
    """Discord 受信 PCM（48kHz / stereo / 16bit LE）を STT 用の 16kHz / mono float32 に変換する。
    48k→16k は 3:1 デシメーション（平均で簡易アンチエイリアス）。"""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if a.size >= 2:
        a = a.reshape(-1, 2).mean(axis=1)  # stereo → mono
    n = (a.size // 3) * 3
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return a[:n].reshape(-1, 3).mean(axis=1).astype(np.float32)


# ───────────────────────────────── TTS（VOICEVOX 合成 → 再生プレイヤ） ─────────────────────────────────
class Speaker:
    """文字列をキューで受け取り、別スレッドで VOICEVOX 合成して、合成済み PCM を
    再生プレイヤ（discord_agent.DiscordPlayer 等）へ渡す。
    生成(LLM)と発話(TTS)を重ねることで体感遅延を下げる。
    interrupt() でバージイン（再生の即時停止＋未再生キュー破棄）に対応する。

    player は次のインターフェイスを満たすこと:
      feed(data: np.ndarray, sr: int)  … 合成済み音声を再生キューへ
      clear()                          … 再生を即停止し、再生キューを破棄
      wait_idle()                      … 再生キューを流し切るまでブロック
    """

    def __init__(self, player):
        self.player = player
        self.q: queue.Queue[str | None] = queue.Queue()
        self._interrupted = False
        self._anchor: float | None = None  # ユーザー発話完了時刻。次の音出し直前に総遅延を表示
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def set_anchor(self, t: float | None) -> None:
        """ユーザーの発話完了時刻を覚える。次の再生開始時に
        『発話完了→応答音声』のトータル遅延を一度だけログする。"""
        self._anchor = t

    def say(self, text: str) -> None:
        text = text.strip()
        if text and not self._interrupted:
            self.q.put(text)

    def wait_done(self) -> None:
        """積んだ発話を全て合成・再生し終えるまで待つ（割り込み時は即座に返る）。"""
        self.q.join()  # 全テキストの合成＋プレイヤへの投入完了を待つ
        self.player.wait_idle()  # 投入済み音声の再生完了を待つ

    def clear_interrupt(self) -> None:
        """次ターン開始時に割り込み状態を解除する。"""
        self._interrupted = False

    def interrupt(self) -> None:
        """再生を即停止し、未再生のキューを捨てる（バージイン用）。"""
        self._interrupted = True
        while True:
            try:
                self.q.get_nowait()
                self.q.task_done()
            except queue.Empty:
                break
        self.player.clear()

    def _run(self) -> None:
        while True:
            text = self.q.get()
            if text is None:
                self.q.task_done()
                break
            try:
                if self._interrupted:
                    continue
                wav = self._synth(text)
                if wav is not None and not self._interrupted:
                    data, sr = sf.read(io.BytesIO(wav), dtype="float32")
                    if self._anchor is not None:
                        print(f"[total] 発話完了→応答音声 {time.monotonic() - self._anchor:.2f}s")
                        self._anchor = None
                    self.player.feed(data, sr)
            except Exception as e:
                print(f"[TTS error] {e}", file=sys.stderr)
            finally:
                self.q.task_done()

    def _synth(self, text: str) -> bytes:
        # 1) audio_query 2) synthesis の2段（VOICEVOX 標準）
        _t0 = time.monotonic()
        q = requests.post(
            f"{C.VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": C.VOICEVOX_SPEAKER},
            timeout=30,
        )
        q.raise_for_status()
        query = q.json()
        query["speedScale"] = C.VOICEVOX_SPEED
        query["volumeScale"] = getattr(C, "VOICEVOX_VOLUME", 1.0)
        r = requests.post(
            f"{C.VOICEVOX_URL}/synthesis",
            params={"speaker": C.VOICEVOX_SPEAKER},
            data=json.dumps(query),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        if getattr(C, "TURN_TIMING", True):
            print(f"[tts] 合成 {time.monotonic() - _t0:.2f}s（{len(text)}字）")
        return bytes(r.content)


# ───────────────────────────────── 会話ログ（Discord Webhook） ─────────────────────────────────
class DiscordLogger:
    """会話ログを Discord Webhook へ POST する。Speaker と同様の
    キュー + 別スレッドの投げ捨て式で、送信時間をターンのレイテンシに乗せない。
    送信失敗は stderr に出すだけで会話は止めない。
    「あなた」と AI で別の Webhook URL（DISCORD_WEBHOOK_URL_USER / _AI）を使うと、
    Discord 側で投稿者（名前・アイコン）が分かれて読みやすい。片方しか設定されて
    いなければ両方そちらへ送り、発話者名を本文に前置して区別する。両方空なら無効。
    ログモード（STT 直送）は会話ログとは別の Webhook（DISCORD_WEBHOOK_URL_LOGMODE）
    へ送る。送信ワーカーは全 Webhook で共用する。"""

    _LIMIT = 1900  # Discord の content 上限 2000 字への安全マージン

    def __init__(self):
        user_url = getattr(C, "DISCORD_WEBHOOK_URL_USER", "")
        ai_url = getattr(C, "DISCORD_WEBHOOK_URL_AI", "")
        log_url = getattr(C, "DISCORD_WEBHOOK_URL_LOGMODE", "")
        self._urls = {"user": user_url or ai_url, "ai": ai_url or user_url, "log": log_url}
        self._shared = not (user_url and ai_url)  # URL 共用時は発話者名を前置
        self.enabled = bool(user_url or ai_url)
        self.log_enabled = bool(log_url)
        self.q: queue.Queue[tuple[str, str]] = queue.Queue()
        if self.enabled or self.log_enabled:
            threading.Thread(target=self._run, daemon=True).start()
        if self.enabled:
            print(
                "[discord] 会話ログ送信を有効化"
                + ("（単一 Webhook・発話者名を前置）" if self._shared else "（あなた/AI 別 Webhook）")
            )
        if self.log_enabled:
            print("[discord] ログモードの送信先を有効化")

    def user(self, text: str) -> None:
        self._post("user", text)

    def ai(self, text: str) -> None:
        self._post("ai", text)

    def log(self, text: str) -> None:
        """ログモード: STT 結果をそのまま専用 Webhook へ送る（発話者名は付けない）。"""
        self._post("log", text)

    def _post(self, role: str, text: str) -> None:
        text = (text or "").strip()
        url = self._urls.get(role)
        if not url or not text:
            return
        if role != "log" and self._shared:
            name = "あなた" if role == "user" else "ずんだもん"
            text = f"**{name}**: {text}"
        for i in range(0, len(text), self._LIMIT):
            self.q.put((url, text[i : i + self._LIMIT]))

    def _run(self) -> None:
        while True:
            url, content = self.q.get()
            try:
                r = requests.post(url, json={"content": content}, timeout=10)
                if r.status_code == 429:  # レート制限: 指定秒だけ待って1回だけ再送
                    time.sleep(float(r.headers.get("Retry-After", "1")) + 0.5)
                    r = requests.post(url, json={"content": content}, timeout=10)
                r.raise_for_status()
            except Exception as e:
                print(f"[discord] 送信失敗（無視）: {e}", file=sys.stderr)
            finally:
                self.q.task_done()


# ───────────────────────────────── STT（リモート・OpenAI 互換） ─────────────────────────────────
def transcribe(audio: np.ndarray) -> str:
    """16kHz/mono float32 の音声を WAV にして OpenAI 互換 STT サーバへ POST し、
    文字起こしテキストを返す（STT_TIMING で所要を表示）。"""
    t0 = time.monotonic()
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    buf.seek(0)
    files = {"file": ("utterance.wav", buf, "audio/wav")}
    data = {"model": C.STT_MODEL, "language": C.WHISPER_LANGUAGE, "response_format": "json"}
    headers = {}
    if C.STT_API_KEY:
        headers["Authorization"] = f"Bearer {C.STT_API_KEY}"
    r = requests.post(f"{C.STT_BASE_URL}/audio/transcriptions", files=files, data=data, headers=headers, timeout=60)
    r.raise_for_status()
    try:
        text = (r.json().get("text") or "").strip()
    except ValueError:
        text = r.text.strip()
    if getattr(C, "STT_TIMING", True):
        dur = len(audio) / SAMPLE_RATE
        print(f"[stt] {time.monotonic() - t0:.2f}s（音声 {dur:.1f}s）")
    return text


# ───────────────────────────────── ログモード（STT → Discord 直送） ─────────────────────────────────
def _normalize_command(text: str) -> str:
    """発話コマンド照合用の正規化。STT の表記ゆれ（「ログ モード」「ろぐもーど。」等）を
    吸収するため、NFKC → 空白・記号の除去 → ひらがな→カタカナ統一 → 英字小文字化を行う。"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^ぁ-ゖァ-ヶー一-龠a-zA-Z0-9]", "", text)
    text = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)
    return text.lower()


_LOG_ON_CMDS = {_normalize_command(s) for s in C.LOG_MODE_ON_COMMANDS.split(",") if s.strip()}
_LOG_OFF_CMDS = {_normalize_command(s) for s in C.LOG_MODE_OFF_COMMANDS.split(",") if s.strip()}


def _match_log_command(text: str) -> str | None:
    """発話がログモードの切替コマンドなら "on"/"off"、違えば None を返す。
    誤発火対策として、正規化後の発話**全体**が同義語に完全一致したときだけ反応する
    （会話文中に「ログ」が出ただけでは切り替えない）。
    ログモード中は全発話が Discord 直送になるため、解除側を先に判定する。"""
    t = _normalize_command(text)
    if t in _LOG_OFF_CMDS:
        return "off"
    if t in _LOG_ON_CMDS:
        return "on"
    return None


# ───────────────────────────────── 会話 LLM（llama.cpp / OpenAI 互換） ─────────────────────────────────
def llm_stream(messages):
    """OpenAI 互換 /chat/completions を stream で叩き、トークン(delta)を yield する。"""
    payload = {
        "model": C.LLAMA_MODEL,
        "messages": messages,
        "temperature": C.LLAMA_TEMPERATURE,
        "max_tokens": C.LLAMA_MAX_TOKENS,
        "stream": True,
    }
    if getattr(C, "LLAMA_DISABLE_THINKING", False):
        # thinking を切らないと、思考トークンを吐き終わるまで content が来ず
        # 初回の音出しがまるごと遅れる（実測で TTFT 2.4s → 0.7s）。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Authorization": f"Bearer {C.LLAMA_API_KEY}"}
    with requests.post(
        f"{C.LLAMA_BASE_URL}/chat/completions",
        json=payload,
        headers=headers,
        stream=True,
        timeout=120,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


# ───────────────────────────────── opencode serve（作業委譲） ─────────────────────────────────
class OpenCode:
    def __init__(self):
        self.session_id = None

    def _ensure_session(self) -> None:
        if self.session_id:
            return
        r = requests.post(f"{C.OPENCODE_BASE_URL}/session", json={}, timeout=30)
        r.raise_for_status()
        self.session_id = r.json()["id"]

    def run(self, instruction: str) -> str:
        """opencode に作業を投げ、応答テキストを返す（同期）。"""
        self._ensure_session()
        body = {
            "providerID": C.OPENCODE_PROVIDER_ID,
            "modelID": C.OPENCODE_MODEL_ID,
            "parts": [{"type": "text", "text": instruction}],
        }
        r = requests.post(
            f"{C.OPENCODE_BASE_URL}/session/{self.session_id}/message",
            json=body,
            timeout=600,
        )
        r.raise_for_status()
        return _extract_text(r.json())


def _extract_text(obj) -> str:
    """opencode の応答 JSON から text パートをかき集める（版差に対する保険つき）。"""
    texts = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == "text" and isinstance(x.get("text"), str):
                texts.append(x["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return "\n".join(t for t in texts if t.strip()).strip() or "(応答が空でした)"


# ───────────────────────────────── ターン処理 ─────────────────────────────────
class TurnInterrupt:
    """handle_turn にバージイン（割り込み）を伝える最小フラグ。
    discord_agent 側が、再生中にユーザーが話し始めたら triggered をセットする。"""

    def __init__(self):
        self.triggered = threading.Event()


def _trim_history(messages) -> None:
    """system(先頭) + 直近 LLAMA_MAX_HISTORY 件だけ残す。長時間の会話で
    プロンプトが伸び続けて TTFT が悪化するのを防ぐ。タスクの「作業結果」全文も
    ここで自然に押し出される。先頭が assistant 始まりにならないよう調整する。"""
    keep = getattr(C, "LLAMA_MAX_HISTORY", 20)
    if keep <= 0 or len(messages) - 1 <= keep:
        return
    del messages[1 : len(messages) - keep]
    while len(messages) > 1 and messages[1]["role"] != "user":
        del messages[1]


def handle_turn(user_text, messages, speaker: Speaker, opencode: OpenCode, monitor: TurnInterrupt, dlog: DiscordLogger):
    messages.append({"role": "user", "content": user_text})
    _trim_history(messages)

    buffer = ""  # 生成テキスト全体
    sent_buf = ""  # TTS にまだ流していない端数
    decided = False  # 雑談/タスクの判定が済んだか
    is_task = False
    first_done = False  # 1文目を喋り出したか（早出し制御）

    t0 = time.monotonic()  # LLM 呼び出し開始（STT 完了直後）
    t_first_token = None  # 最初のトークンが来た時刻
    t_first_say = None  # 最初の音をキューに積んだ時刻

    for delta in llm_stream(messages):
        if monitor.triggered.is_set():  # バージインで中断
            break
        if t_first_token is None:
            t_first_token = time.monotonic()
        buffer += delta

        # 先頭を覗いて「雑談」か「[[TASK]]」かを一度だけ判定
        if not decided:
            head = buffer.lstrip()
            if len(head) < len(_TASK_SENTINEL):
                continue  # まだ判定に足る文字が来ていない
            decided = True
            is_task = head.startswith(_TASK_SENTINEL)
            if is_task:
                continue
            # 雑談確定。ここまでの buffer は現 delta を既に含むので、そのまま発話対象へ。
            sent_buf = buffer
        elif is_task:
            continue  # タスク時は喋らず全文を貯める
        else:
            sent_buf += delta

        # 雑談: 1文目は読点/文字数でも早出しし、初回の音出しを縮める。
        # 2文目以降は文単位（1文目を喋る裏で生成されるので無音にならない）。
        if not first_done:
            sent_buf, flushed = _flush_first(sent_buf, speaker)
            if not flushed:
                continue
            first_done = True
            if t_first_say is None:
                t_first_say = time.monotonic()
        sent_buf = _flush_sentences(sent_buf, speaker)

    if getattr(C, "TURN_TIMING", True) and t_first_token is not None:
        ttft = t_first_token - t0
        if t_first_say is not None:
            print(f"[turn] TTFT {ttft:.2f}s / 初音 {t_first_say - t0:.2f}s")
        else:
            print(f"[turn] TTFT {ttft:.2f}s（音声出力なし）")

    if monitor.triggered.is_set():
        if buffer.strip():
            print(f"ずんだもん: {buffer.strip()}（割り込みで中断）")
            dlog.ai(f"{buffer.strip()}（割り込みで中断）")
        messages.append({"role": "assistant", "content": buffer})
        return

    if is_task:
        instruction = buffer.lstrip()[len(_TASK_SENTINEL) :].strip()
        messages.append({"role": "assistant", "content": buffer})
        print(f"  → opencode へ委譲: {instruction}")
        dlog.ai(f"🛠️ 作業委譲: {instruction}")
        speaker.say("わかりました、やってみますね")  # 待ち時間を隠すフィラー
        try:
            result = opencode.run(instruction)
        except Exception as e:
            speaker.say("作業中にエラーが出ちゃいました")
            print(f"[opencode error] {e}", file=sys.stderr)
            dlog.ai(f"⚠️ 作業中にエラー: {e}")
            speaker.wait_done()
            return
        if monitor.triggered.is_set():  # 作業中に割り込まれたら要約しない
            return
        # 結果を LLM に渡して音声向けに要約させる
        messages.append({"role": "user", "content": f"作業結果:\n{result}\n\n{C.SUMMARIZE_PROMPT}"})
        summary = ""
        sb = ""
        for delta in llm_stream(messages):
            if monitor.triggered.is_set():
                break
            summary += delta
            sb += delta
            sb = _flush_sentences(sb, speaker)
        if sb.strip() and not monitor.triggered.is_set():
            speaker.say(sb)
        if summary.strip():
            print(f"ずんだもん: {summary.strip()}")
            dlog.ai(summary)
        messages.append({"role": "assistant", "content": summary})
    else:
        if sent_buf.strip():
            speaker.say(sent_buf)  # 端数を流し切る
        if buffer.strip():
            print(f"ずんだもん: {buffer.strip()}")
            dlog.ai(buffer)
        messages.append({"role": "assistant", "content": buffer})

    speaker.wait_done()


def _flush_sentences(buf: str, speaker: Speaker) -> str:
    """buf の中の完成した文を TTS に流し、未完の端数を返す。"""
    while True:
        m = _SENT_BOUNDARY.search(buf)
        if not m:
            return buf
        end = m.end()
        sentence = buf[:end].strip()
        if sentence:
            speaker.say(sentence)
        buf = buf[end:]


def _flush_first(buf: str, speaker: Speaker) -> tuple[str, bool]:
    """応答の1文目だけ、初回の音出しを早めるために緩い基準で TTS に流す。
      ・句点(。！？等)が来たら無条件で流す
      ・読点(、,)なら最小文字数を超えたとき流す（短すぎる細切れを避ける）
      ・どちらも来なくても上限文字数に達したら、そこで区切って流す
    早出しできたら (残り, True)、まだ流せないなら (buf, False) を返す。"""
    hard = _SENT_BOUNDARY.search(buf)
    soft = _SOFT_BOUNDARY.search(buf)
    if hard:
        end = hard.end()
    elif soft and soft.end() >= C.FIRST_FLUSH_MIN_CHARS:
        end = soft.end()
    elif len(buf) >= C.FIRST_FLUSH_MAX_CHARS:
        end = C.FIRST_FLUSH_MAX_CHARS
    else:
        return buf, False
    chunk = buf[:end].strip()
    if chunk:
        speaker.say(chunk)
        return buf[end:], True
    # 区切りはあったが中身が空白だけ → 区切りを捨てて継続（まだ喋っていない）
    return buf[end:], False


# ───────────────────────────────── 起動時ウォームアップ ─────────────────────────────────
def warmup(speaker: Speaker, messages) -> None:
    """初回の体感遅延を吸収する。VOICEVOX(初回合成 JIT)・LLM(接続/プロンプト前処理) を
    起動時に1回だけ温める。失敗は無視（本番ループの妨げにしない）。"""
    print("ウォームアップ中…")
    t0 = time.monotonic()
    try:
        speaker._synth("あ")  # 捨て合成（再生はしない）
    except Exception as e:
        print(f"[warmup] VOICEVOX 失敗（無視）: {e}", file=sys.stderr)
    try:
        # system prompt を投げてプロンプト前処理を温める。最初のトークンで即打ち切り。
        for _ in llm_stream(messages + [{"role": "user", "content": "ping"}]):
            break
    except Exception as e:
        print(f"[warmup] LLM 失敗（無視）: {e}", file=sys.stderr)
    print(f"ウォームアップ完了（{time.monotonic() - t0:.1f}s）")
