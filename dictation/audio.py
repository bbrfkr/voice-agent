"""マイク入力の取り込みと、無音による発話セグメント分割。

Whisper は本質的に非ストリーミング（発話全体を見て確定する）なので、文字単位の逐次確定は
できない。代わりに **無音で区切った発話セグメント単位**で文字起こしし、確定した端から打鍵する。
これにより「話している最中に、区切るそばから文字が流れ込む」体験になる。

しきい値の既定値は Web UI（`server/static/app.js`）の VAD と揃えてある
（RMS 0.015 / 無音 1200ms）。
"""

import io
import queue
import threading
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np

SAMPLE_RATE = 16000  # faster-whisper が内部で使うレート。ここで合わせておけば再サンプル不要。
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


@dataclass
class SegmentStats:
    """1 回の録音で実際に何が観測されたか（「無反応」の原因表示に使う）。"""

    max_rms: float = 0.0  # 観測された最大音量
    speech_ms: int = 0  # しきい値を超えていた合計時間
    total_ms: int = 0  # 溜め込んだ音声の長さ


@dataclass
class VadParams:
    """無音区切りのパラメータ（既定値は Web UI の VAD と同じ）。"""

    threshold: float = 0.015  # 発話とみなす RMS のしきい値
    silence_ms: int = 1200  # この長さの無音が続いたらセグメントを確定する
    min_speech_ms: int = 300  # これ未満しか発話が無いセグメントは捨てる（物音の誤検出よけ）
    max_segment_ms: int = 15000  # 区切りが来なくてもこの長さで強制的に確定する
    tail_ms: int = 300  # 確定時に残す末尾の無音（Whisper が語尾を切らないように少しだけ残す）


def _rms(frame: np.ndarray) -> float:
    """int16 フレームの RMS を 0.0〜1.0 で返す。"""
    if frame.size == 0:
        return 0.0
    x = frame.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def to_wav(frames: list[np.ndarray]) -> bytes:
    """int16 モノラルのフレーム列を WAV（16kHz/16bit）にまとめる。

    faster-whisper は PyAV でデコードするので、生 PCM ではなくコンテナに入れて渡す。
    WAV ヘッダを被せるだけなので再エンコードのコストはかからない。
    """
    pcm = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class Segmenter:
    """フレームを食わせると、無音で区切れた発話を WAV bytes として吐く状態機械。

    `feed()` はセグメントが確定したときだけ bytes を返す（それ以外は None）。
    PTT を離したときは `flush()` で残りを確定させる。
    """

    def __init__(self, params: VadParams) -> None:
        self.p = params
        self._buf: list[np.ndarray] = []
        self._speech_frames = 0
        self._silence_frames = 0
        self._max_rms = 0.0
        self.last_level = 0.0  # 直近フレームの音量（レベルメータ表示用）
        #: 直近の切り出しで観測された値（採用・不採用にかかわらず更新される）
        self.last = SegmentStats()

    def reset(self) -> None:
        self._buf = []
        self._speech_frames = 0
        self._silence_frames = 0
        self._max_rms = 0.0

    @property
    def _silence_limit(self) -> int:
        return max(1, self.p.silence_ms // FRAME_MS)

    def level(self, frame: np.ndarray) -> float:
        """フレームの音量を返しつつ、そのセグメントの最大値として記録する。"""
        rms = _rms(frame)
        self._max_rms = max(self._max_rms, rms)
        self.last_level = rms
        return rms

    def feed(self, frame: np.ndarray) -> bytes | None:
        loud = self.level(frame) >= self.p.threshold
        if not loud and self._speech_frames == 0:
            # まだ一度も声が出ていない先頭の無音は溜めない（無駄に長い音声を送らない）
            return None
        self._buf.append(frame)
        if loud:
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        if self._silence_frames >= self._silence_limit:
            return self._cut()
        if len(self._buf) * FRAME_MS >= self.p.max_segment_ms:
            return self._cut()
        return None

    def flush(self) -> bytes | None:
        """PTT を離したときなどに、溜まっている分を確定させる。"""
        return self._cut()

    def _cut(self) -> bytes | None:
        self.last = SegmentStats(
            max_rms=self._max_rms,
            speech_ms=self._speech_frames * FRAME_MS,
            total_ms=len(self._buf) * FRAME_MS,
        )
        if self._speech_frames * FRAME_MS < self.p.min_speech_ms:
            self.reset()
            return None
        # 末尾の無音は tail_ms 分だけ残して切り落とす（送信量と STT 時間を減らす）
        keep_tail = max(0, self.p.tail_ms // FRAME_MS)
        drop = max(0, self._silence_frames - keep_tail)
        frames = self._buf[: len(self._buf) - drop] if drop else self._buf
        wav = to_wav(frames)
        self.reset()
        return wav


class MicStream:
    """マイクを開きっぱなしにして、30ms フレームをキューへ流し込む。

    PTT のたびにデバイスを開閉すると先頭が数百 ms 欠けるため、ストリームは常時開いておき
    録音するかどうかは呼び出し側のフラグで決める（`Recorder` が担当）。
    """

    def __init__(self, device: int | str | None = None) -> None:
        self.device = device
        # フレームと制御マーカー（"start"/"stop"）を同じキューに流すことで、録音停止の
        # タイミングとフレームの前後関係が崩れない（コールバックは消費側より先行するため、
        # フラグで判定すると発話の末尾を取りこぼす）。
        self.frames: queue.Queue[np.ndarray | str] = queue.Queue()
        self._stream: Any = None

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            print(f"[audio] {status}")
        self.frames.put(indata[:, 0].copy())

    @contextmanager
    def open(self) -> Iterator["MicStream"]:
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        try:
            yield self
        finally:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def list_devices() -> str:
    """`--list-devices` 用に入力デバイス一覧を文字列で返す。"""
    import sounddevice as sd

    return str(sd.query_devices())


class RecorderEvents:
    """録音の進行状況を受け取るフック。既定はすべて何もしない。

    「キーを押しても無反応」がどの段階で起きているのか（キーが拾えていない／音が小さい／
    送信していない）を切り分けられるよう、各段階を外から観測できるようにしている。
    """

    def started(self) -> None:
        """PTT が押されて録音を開始した。"""

    def level(self, rms: float) -> None:
        """録音中の音量（間引いて呼ばれる）。"""

    def stopped(self, stats: SegmentStats, produced: int) -> None:
        """PTT が離された。produced はこの押下で送ったセグメント数。"""


class Recorder:
    """マイクフレームを読み、録音中だけセグメント化して `on_segment` へ渡す。

    `start()`/`stop()` は PTT のキー押下・解放から呼ばれる（pynput のリスナースレッド）。
    録音していない間のフレームは捨てるので、押していない時間の音は一切送られない。
    """

    #: レベル通知を間引く間隔（フレーム数）。30ms × 8 ≒ 0.24 秒ごと。
    LEVEL_EVERY = 8

    def __init__(
        self,
        mic: MicStream,
        params: VadParams,
        on_segment: Callable[[bytes, bool], None],
        events: RecorderEvents | None = None,
    ) -> None:
        self.mic = mic
        self.seg = Segmenter(params)
        self.on_segment = on_segment  # (WAV, その録音での 1 本目か) を受け取る
        self.events = events or RecorderEvents()
        self._nth = 0  # 1 回の押下の中で何本目のセグメントか
        self._frames_since_level = 0
        self._recording = False  # 消費スレッドだけが触る（マーカー受信で切り替わる）
        self._requested = False  # 呼び出し側から見た要求状態（キーリピートの抑止用）
        self._stop = threading.Event()

    def start(self) -> None:
        if self._requested:
            return  # キーリピートで start が連打されても無視する
        self._requested = True
        self.mic.frames.put("start")

    def stop(self) -> None:
        if not self._requested:
            return
        self._requested = False
        self.mic.frames.put("stop")

    def shutdown(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """フレームを消費し続けるループ（専用スレッドで回す）。"""
        while not self._stop.is_set():
            try:
                item = self.mic.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            if isinstance(item, str):
                if item == "start":
                    self.seg.reset()
                    self._nth = 0
                    self._frames_since_level = 0
                    self._recording = True
                    self.events.started()
                else:
                    self._recording = False
                    self._emit(self.seg.flush())
                    self.events.stopped(self.seg.last, self._nth)
                continue
            if not self._recording:
                continue  # 録音していない間のマイク入力は捨てる
            self._emit(self.seg.feed(item))
            self._frames_since_level += 1
            if self._frames_since_level >= self.LEVEL_EVERY:
                self._frames_since_level = 0
                self.events.level(self.seg.last_level)

    def _emit(self, wav: bytes | None) -> None:
        if wav:
            self.on_segment(wav, self._nth == 0)
            self._nth += 1
