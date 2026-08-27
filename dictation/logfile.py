"""コンソールが無いときに、標準出力をログファイルへ逃がす。

macOS の `.app` バンドルや Windows のウィンドウ無し exe には端末が繋がっていないため、
`print()` の出力先が無い（PyInstaller は sys.stdout を None ないしダミーに差し替える）。
認識結果やエラーを後から追えるよう、そういう場合だけログファイルへ切り替える。

端末がある場合（`python -m dictation` や console 付き exe）は何もしない。
"""

import contextlib
import os
import sys
from pathlib import Path

APP_NAME = "voice-dictation"
MAX_BYTES = 1_000_000  # これを超えていたら起動時に切り詰める（無限に太らせない）


def log_path() -> Path:
    """OS の慣習的な場所にログファイルのパスを決める。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home()) / APP_NAME / f"{APP_NAME}.log"
    return Path.home() / ".local" / "state" / APP_NAME / f"{APP_NAME}.log"


def has_console() -> bool:
    """標準出力・標準エラーが本物のストリームとして使えるか。

    パイプやリダイレクトは「使える」側（fileno が引ける）。`.app` から起動した場合だけ
    ここが False になる。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            return False
        try:
            stream.fileno()
        except Exception:
            return False
    return True


def setup(force: bool = False) -> Path | None:
    """端末が無ければ stdout/stderr をログファイルへ差し替え、そのパスを返す。"""
    if has_console() and not force:
        # ファイルやパイプへリダイレクトされていると既定でブロックバッファリングになり、
        # 強制終了したときに出力が丸ごと失われる。行単位で吐くようにしておく。
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                with contextlib.suppress(Exception):
                    reconfigure(line_buffering=True)
        return None
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_BYTES:
        path.unlink()
    # プロセスが終わるまで開きっぱなしにする（with で閉じてはいけない）。
    # 行バッファリングにして、強制終了されても直前までの出力が残るようにする。
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = stream
    sys.stderr = stream
    return path
