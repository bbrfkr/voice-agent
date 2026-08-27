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
from collections.abc import Callable
from dataclasses import dataclass

from dictation.audio import MicStream, Recorder, VadParams
from dictation.inject import TextInjector
from dictation.stt_client import TranscribeClient


@dataclass
class RunnerOptions:
    join: str = ""  # セグメント同士の区切り文字（日本語は空、英語なら " " が自然）
    quiet: bool = False  # 認識結果をコンソールに出さない


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
        self.recorder = Recorder(mic, vad, self._on_segment)
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
            wav, first = item
            try:
                text = self.client.transcribe(wav)
            except Exception as e:
                print(f"[stt] 失敗（このセグメントは破棄）: {e}", file=sys.stderr)
                continue
            if not text:
                continue
            if not first and self.options.join:
                text = self.options.join + text
            if not self.options.quiet:
                print(f"▶ {text}")
            try:
                self.injector.type_text(text)
            except Exception as e:
                print(f"[inject] 打鍵に失敗: {e}", file=sys.stderr)

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
