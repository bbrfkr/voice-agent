"""STT（faster-whisper）をサービス化する。

旧 voice_agent.py の `_transcribe` と `_register_cuda_dll_dirs`・モデル初期化を
`WhisperService` にまとめた。録音した音声ファイル（webm/opus 等）の bytes を
そのまま受け取り、faster-whisper（CTranslate2）内部の PyAV デコードに通すので、
サーバ側で numpy への変換や 16kHz リサンプルは不要。
"""

import contextlib
import glob
import io
import os
import sys
import threading
import time
from typing import Any, BinaryIO

import numpy as np

import config as C


def _register_cuda_dll_dirs() -> None:
    """Windows で faster-whisper(CTranslate2) が要求する CUDA12 cuBLAS/cuDNN の
    DLL を見つけられるようにする（Windows でサーバを直起動する場合のみ意味を持つ）。
    Docker/Linux 運用では no-op。faster_whisper を import する前に呼ぶこと。"""
    if sys.platform != "win32":
        return
    import site

    bases = list(site.getsitepackages())
    user = getattr(site, "getusersitepackages", lambda: None)()
    if user:
        bases.append(user)
    bases += [p for p in sys.path if p.endswith("site-packages")]
    found: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for binp in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            binp = os.path.normpath(binp)
            if binp in seen or not os.path.isdir(binp):
                continue
            seen.add(binp)
            with contextlib.suppress(OSError):
                os.add_dll_directory(binp)
            os.environ["PATH"] = binp + os.pathsep + os.environ.get("PATH", "")
            found.append(binp)
    has_cublas = any(glob.glob(os.path.join(d, "cublas64_*.dll")) for d in found)
    if found and has_cublas:
        print(f"CUDA DLL ディレクトリを登録: {len(found)} 件")
    elif found:
        print("⚠ nvidia の bin を登録しましたが cublas64_*.dll が見当たりません。", file=sys.stderr)
    else:
        print("⚠ nvidia-* の DLL が見つかりません（CUDA 利用時は nvidia-cublas-cu12 等が必要）。", file=sys.stderr)


class WhisperService:
    """faster-whisper モデルを 1 度ロードし、音声→テキストの文字起こしを提供する。
    モデルはスレッドセーフではないので、呼び出しはサーバ側で 1 本のワーカースレッドに
    直列化する前提（CTranslate2 自体は内部で並列化する）。"""

    def __init__(self) -> None:
        # CTranslate2 のモデルは並行 transcribe に対して安全でないため、複数接続からの
        # 呼び出しはこのロックで直列化する（CTranslate2 自体は内部で並列化する）。
        self._lock = threading.Lock()
        # faster_whisper(CTranslate2) を import する前に DLL 検索パスを通す
        _register_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        try:
            self.model = WhisperModel(C.WHISPER_MODEL, device=C.WHISPER_DEVICE, compute_type=C.WHISPER_COMPUTE)
        except Exception as e:
            if C.WHISPER_DEVICE != "cpu":
                print(
                    f"[警告] CUDA で Whisper を初期化できませんでした（{e}）。CPU にフォールバックします。",
                    file=sys.stderr,
                )
                self.model = WhisperModel(C.WHISPER_MODEL, device="cpu", compute_type="int8")
            else:
                raise

    def transcribe(self, audio: bytes | BinaryIO | np.ndarray) -> str:
        """音声を文字起こしして結合テキストを返す（STT_TIMING で所要を表示）。
        audio: 録音ファイルの bytes / file-like（faster-whisper が PyAV でデコード）、
               または 16kHz float32 の ndarray（ウォームアップ等）。"""
        t0 = time.monotonic()
        src: Any = io.BytesIO(audio) if isinstance(audio, bytes) else audio
        with self._lock:
            segments, info = self.model.transcribe(
                src,
                language=C.WHISPER_LANGUAGE,
                beam_size=C.WHISPER_BEAM_SIZE,
                vad_filter=C.WHISPER_VAD_FILTER,
            )
            text = "".join(s.text for s in segments).strip()
        if C.STT_TIMING:
            dur = getattr(info, "duration", 0.0) or 0.0
            print(f"[stt] {time.monotonic() - t0:.2f}s（音声 {dur:.1f}s）")
        return text

    def warmup(self) -> None:
        """無音 1 秒を transcribe して CUDA カーネルを初期化する（失敗は無視）。"""
        with contextlib.suppress(Exception):
            segments, _ = self.model.transcribe(np.zeros(16000, dtype=np.float32), language=C.WHISPER_LANGUAGE)
            list(segments)
