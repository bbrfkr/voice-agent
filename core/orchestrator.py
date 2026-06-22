"""1 ターンのオーケストレーション（旧 voice_agent.py の handle_turn のイベント発行版）。

制御フローは現行どおり：
  先頭で「雑談 / [[TASK]]」を一度だけ判定 → 雑談は1文目を早出し → 文単位で TTS へ流す。
  [[TASK]] は全文を貯めて opencode に委譲し、結果は要約せずそのまま画面へ表示する。
  読み上げは「短ければ本文を読み、長ければ読まずに定型句だけ喋る」（長い調査結果を
  延々と読み上げないため。しきい値は config の TASK_SPEAK_MAX_CHARS）。

旧版との違いは「喋る」先が音声再生ではなく **イベント発行**になったこと：
  - 文が確定するたび TtsSink.say() → 別スレッドで VOICEVOX 合成 → emit_audio(wav) で
    WebSocket からブラウザへ送る（合成と LLM 生成のパイプラインは現行と同様に維持）。
  - 文字情報（assistant 応答・タスク状況・LLM の逐次 delta）は emit_event(dict) で送る。
バージインは旧 monitor.triggered と同型の threading.Event（cancel）で中断する。
"""

import queue
import threading
import time
from collections.abc import Callable

import config as C
from core.discord_log import DiscordLogger
from core.llm import Message, llm_stream, trim_history
from core.opencode import OpenCode
from core.textflow import TASK_SENTINEL, flush_first, flush_sentences
from core.tts import VoicevoxClient

EmitEvent = Callable[[dict], None]
EmitAudio = Callable[[int, bytes], None]


class TtsSink:
    """文字列をキューで受け取り、別スレッドで VOICEVOX 合成 → emit_audio で送出する。
    旧 Speaker の「生成(LLM)と発話(TTS)を重ねる」キュー構造をそのまま踏襲し、再生だけ
    ブラウザへ移したもの。cancel が立つと未処理分を捨てて静かになる（バージイン）。"""

    def __init__(
        self,
        voicevox: VoicevoxClient,
        emit_audio: EmitAudio,
        cancel: threading.Event,
        speaker: int | None = None,
        speed: float | None = None,
    ) -> None:
        self._vv = voicevox
        self._emit_audio = emit_audio
        self._cancel = cancel
        self._speaker = speaker
        self._speed = speed
        self._q: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._seq = 0
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def say(self, text: str) -> None:
        text = text.strip()
        if text and not self._cancel.is_set():
            self._q.put((self._seq, text))
            self._seq += 1

    def wait_done(self) -> None:
        """積んだ発話を全て合成・送出し終えるまで待つ。"""
        self._q.join()

    def close(self) -> None:
        """ワーカーを終了させる（接続のターン終了時に呼ぶ）。"""
        self._q.put(None)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                break
            seq, text = item
            try:
                if self._cancel.is_set():
                    continue
                wav = self._vv.synth(text, speaker=self._speaker, speed=self._speed)
                if wav and not self._cancel.is_set():
                    self._emit_audio(seq, wav)
            except Exception as e:
                print(f"[TTS error] {e}")
            finally:
                self._q.task_done()


def run_turn(
    user_text: str,
    messages: list[Message],
    *,
    opencode: OpenCode,
    tts: TtsSink,
    dlog: DiscordLogger,
    emit: EmitEvent,
    cancel: threading.Event,
) -> None:
    """1 ターン分の応答を生成し、テキストは emit、音声は tts.say で送り出す。"""
    messages.append({"role": "user", "content": user_text})
    trim_history(messages)

    buffer = ""  # 生成テキスト全体
    sent_buf = ""  # TTS にまだ流していない端数
    decided = False  # 雑談/タスクの判定が済んだか
    is_task = False
    first_done = False  # 1文目を喋り出したか（早出し制御）

    t0 = time.monotonic()  # LLM 呼び出し開始（STT 完了直後）
    t_first_token: float | None = None
    t_first_say: float | None = None

    for delta in llm_stream(messages):
        if cancel.is_set():  # バージインで中断
            break
        if t_first_token is None:
            t_first_token = time.monotonic()
        buffer += delta

        # 先頭を覗いて「雑談」か「[[TASK]]」かを一度だけ判定
        if not decided:
            head = buffer.lstrip()
            if len(head) < len(TASK_SENTINEL):
                continue
            decided = True
            is_task = head.startswith(TASK_SENTINEL)
            if is_task:
                continue
            sent_buf = buffer  # 雑談確定。ここまでの buffer をそのまま発話対象へ
        elif is_task:
            continue  # タスク時は喋らず全文を貯める
        else:
            sent_buf += delta
            emit({"type": "llm_delta", "text": delta})

        # 雑談: 1文目は読点/文字数でも早出しし、初回の音出しを縮める。
        if not first_done:
            sent_buf, flushed = flush_first(sent_buf, tts.say, C.FIRST_FLUSH_MIN_CHARS, C.FIRST_FLUSH_MAX_CHARS)
            if not flushed:
                continue
            first_done = True
            if t_first_say is None:
                t_first_say = time.monotonic()
        sent_buf = flush_sentences(sent_buf, tts.say)

    if C.TURN_TIMING and t_first_token is not None:
        ttft = t_first_token - t0
        if t_first_say is not None:
            print(f"[turn] TTFT {ttft:.2f}s / 初音 {t_first_say - t0:.2f}s")
        else:
            print(f"[turn] TTFT {ttft:.2f}s（音声出力なし）")

    if cancel.is_set():
        if buffer.strip():
            print(f"VOICEVOXエージェント: {buffer.strip()}（割り込みで中断）")
            emit({"type": "assistant", "text": buffer.strip(), "interrupted": True})
            dlog.ai(f"{buffer.strip()}（割り込みで中断）")
        messages.append({"role": "assistant", "content": buffer})
        return

    if is_task:
        # [[TASK]] 以降はそのまま opencode へ渡る指示文。LLM が後ろに相槌や説明
        # （「承知しました！…」等）を続けても拾わないよう、最初の1行だけを採用する。
        # 読み上げ用の相槌はこの下の固定フィラーが担うので、指示文に混ぜない。
        instruction = buffer.lstrip()[len(TASK_SENTINEL) :].split("\n", 1)[0].strip()
        messages.append({"role": "assistant", "content": buffer})
        print(f"  → opencode へ委譲: {instruction}")
        emit({"type": "task", "status": "delegating", "instruction": instruction})
        dlog.ai(f"🛠️ 作業委譲: {instruction}")
        tts.say("わかりました、やってみますね")  # 待ち時間を隠すフィラー

        def on_progress(info: dict) -> None:
            # opencode 側のツール利用などの進捗を画面へ中継する（読み上げはしない）。
            if cancel.is_set():
                return
            emit({"type": "task", "status": "progress", **info})

        try:
            result = opencode.run(instruction, on_progress=on_progress)
        except Exception as e:
            tts.say("作業中にエラーが出ちゃいました")
            print(f"[opencode error] {e}")
            emit({"type": "task", "status": "error", "message": str(e)})
            dlog.ai(f"⚠️ 作業中にエラー: {e}")
            tts.wait_done()
            return
        if cancel.is_set():  # 作業中に割り込まれたら何もしない
            return
        # opencode の結果は要約せず、そのまま画面・会話履歴・Discord へ流す。
        # 表示は通常の assistant バブル（pre-wrap で複数行・コードもそのまま出る）に一本化。
        result = result.strip()
        print(f"VOICEVOXエージェント(作業結果): {result}")
        emit({"type": "assistant", "text": result})
        dlog.ai(result)
        messages.append({"role": "assistant", "content": result})
        # 読み上げ: 1〜3文程度に収まる短い結果は本文をそのまま読み上げる。
        # 長い結果（調査結果など）は読み上げず、定型句だけ喋って画面を見てもらう。
        if not cancel.is_set():
            if result and len(result) <= C.TASK_SPEAK_MAX_CHARS:
                rem = flush_sentences(result, tts.say)
                if rem.strip():
                    tts.say(rem)
            else:
                tts.say(C.TASK_LONG_REPLY_PHRASE)
    else:
        if sent_buf.strip():
            tts.say(sent_buf)  # 端数を流し切る
        if buffer.strip():
            print(f"VOICEVOXエージェント: {buffer.strip()}")
            emit({"type": "assistant", "text": buffer.strip()})
            dlog.ai(buffer)
        messages.append({"role": "assistant", "content": buffer})

    tts.wait_done()
