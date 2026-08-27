"""マイク → STT → 打鍵をつなぐ本体。

スレッド構成:
  - sounddevice のコールバック : マイクフレームをキューへ
  - Recorder スレッド          : 録音中のフレームを無音で区切り、セグメント（WAV）を吐く
  - 送信スレッド               : セグメントを 1 本ずつ STT へ投げ、返ってきた順に打鍵する
  - メインスレッド             : PTT キーの監視（pynput のリスナー）

送信を 1 スレッドに直列化しているので、打鍵の順序は必ず発話順になる（サーバ側の
faster-whisper もロックで直列化されているため、並列に投げても速くはならない）。
"""

import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from dictation.audio import SAMPLE_RATE, MicStream, Recorder, RecorderEvents, SegmentStats, VadParams
from dictation.inject import TextInjector
from dictation.stt_client import TranscribeClient


@dataclass
class RunnerOptions:
    join: str = ""  # セグメント同士の区切り文字（日本語は空、英語なら " " が自然）
    quiet: bool = False  # 認識結果をコンソールに出さない
    key_name: str = ""  # 表示用の PTT キー名
    debug: bool = False  # 録音中の音量を逐次表示する（しきい値調整用）


class Progress(RecorderEvents):
    """録音の各段階をコンソールへ出す。

    無反応のときにどこで止まっているのかを利用者が判断できるよう、押下・音量・
    送信・打鍵のそれぞれで足跡を残す。
    """

    def __init__(self, vad: VadParams, options: RunnerOptions) -> None:
        self.vad = vad
        self.options = options

    def started(self) -> None:
        print(f"* 録音中… ({self.options.key_name} を押している間)")

    def level(self, rms: float) -> None:
        if not self.options.debug:
            return
        # 0.05 を上限としたバーで、しきい値との位置関係を見えるようにする
        filled = min(30, int(rms / 0.05 * 30))
        mark = "超" if rms >= self.vad.threshold else "－"
        print(f"  音量 {rms:.4f} [{'#' * filled}{'.' * (30 - filled)}] しきい値{mark}")

    def stopped(self, stats: SegmentStats, produced: int) -> None:
        if produced:
            return
        if stats.max_rms < self.vad.threshold:
            suggested = max(0.001, round(stats.max_rms * 0.6, 4))
            print(f"! 音を検出できませんでした（最大音量 {stats.max_rms:.4f} < しきい値 {self.vad.threshold}）")
            if stats.max_rms < 0.0005:
                print("  マイクが拾えていないようです。--list-devices で確認し、device を指定してください。")
            else:
                print(f"  声は入っています。dictation.ini の threshold を {suggested} 程度に下げてください。")
        else:
            print(f"! 発話が短すぎました（{stats.speech_ms}ms < min_speech_ms {self.vad.min_speech_ms}）")


class Dictation:
    def __init__(
        self,
        client: TranscribeClient,
        injector: TextInjector,
        mic: MicStream,
        vad: VadParams,
        options: RunnerOptions,
    ) -> None:
        self.client = client
        self.injector = injector
        self.mic = mic
        self.options = options
        self.vad = vad
        self.recorder = Recorder(mic, vad, self._on_segment, events=Progress(vad, options))
        self._segments: queue.Queue[tuple[bytes, bool] | None] = queue.Queue()
        self._threads: list[threading.Thread] = []

    # ── PTT から呼ばれる ──────────────────────────────────────────────
    def press(self) -> None:
        self.recorder.start()

    def release(self) -> None:
        self.recorder.stop()

    # ── 内部 ────────────────────────────────────────────────────────
    def _on_segment(self, wav: bytes, first: bool) -> None:
        self._segments.put((wav, first))

    def _sender(self) -> None:
        while True:
            item = self._segments.get()
            if item is None:
                break
            # ここで例外を漏らすとスレッドごと死んで以後まったく反応しなくなるため、
            # 1 セグメントぶんの処理をまるごと囲う。
            try:
                self._handle(*item)
            except Exception as e:
                print(f"! 処理に失敗（このセグメントは破棄）: {type(e).__name__}: {e}", file=sys.stderr)

    def _handle(self, wav: bytes, first: bool) -> None:
        seconds = len(wav) / (SAMPLE_RATE * 2)
        if not self.options.quiet:
            print(f"  送信中… ({seconds:.1f} 秒)")
        started = time.monotonic()
        try:
            text = self.client.transcribe(wav)
        except Exception as e:
            print(f"! 文字起こしに失敗（このセグメントは破棄）: {type(e).__name__}: {e}", file=sys.stderr)
            return
        elapsed = time.monotonic() - started
        if not text:
            print(f"  聞き取れませんでした（{elapsed:.1f} 秒で応答）")
            return
        if not first and self.options.join:
            text = self.options.join + text
        if not self.options.quiet:
            print(f"> {text}   ({elapsed:.1f} 秒)")
        try:
            self.injector.type_text(text)
        except Exception as e:
            print(f"! 打鍵に失敗: {type(e).__name__}: {e}", file=sys.stderr)

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def start(self) -> None:
        self._spawn(self.recorder.run, "recorder")
        self._spawn(self._sender, "sender")

    def shutdown(self) -> None:
        self.recorder.stop()
        self.recorder.shutdown()
        self._segments.put(None)
