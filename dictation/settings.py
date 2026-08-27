"""設定ファイル（`dictation.ini`）の読み込み。

exe をダブルクリックで起動する運用では引数を渡せないため、exe と同じ場所に置いた
`dictation.ini` を既定値として読む。コマンドライン引数を渡した場合はそちらが優先される。

探索順は「--config で明示されたパス」→「exe と同じディレクトリ」→「OS のユーザ設定領域」→
「カレントディレクトリ」。最初に見つかった 1 つだけを使う。

macOS の `.app` の場合、実行ファイルはバンドル内部（`*.app/Contents/MacOS/`）にあるが、
そこへ設定ファイルを置かせるのは筋が悪いので **`.app` を置いたフォルダ**を見る。
"""

import configparser
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SECTION = "dictation"
FILENAME = "dictation.ini"


def _to_bool(v: str) -> bool:
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"真偽値として解釈できません: {v!r}（true/false で書いてください）")


def _to_str_or_none(v: str) -> str | None:
    """空欄は「未指定」として扱う（device のように既定が None のもの向け）。"""
    return v if v.strip() else None


#: ini のキー → (argparse の dest, 変換関数)。ここに無いキーは警告して無視する。
KEYS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "server": ("server", str),
    "key": ("key", str),
    "device": ("device", _to_str_or_none),
    "backend": ("backend", str),
    "join": ("join", str),
    "quiet": ("quiet", _to_bool),
    "char_delay_ms": ("char_delay_ms", int),
    "threshold": ("threshold", float),
    "silence_ms": ("silence_ms", int),
    "min_speech_ms": ("min_speech_ms", int),
    "max_segment_ms": ("max_segment_ms", int),
    "no_split": ("no_split", _to_bool),
    "quit_hotkey": ("quit_hotkey", str),
    "debug": ("debug", _to_bool),
}


def base_dir() -> Path:
    """設定ファイルを置く「アプリの隣」を返す。

    - macOS の .app : バンドルを置いたフォルダ（*.app の親）
    - それ以外の exe : exe と同じフォルダ
    - 未凍結        : リポジトリのルート
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    exe_dir = Path(sys.executable).parent
    # *.app/Contents/MacOS/<exe> → *.app の親フォルダ
    if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents" and exe_dir.parent.parent.suffix == ".app":
        return exe_dir.parent.parent.parent
    return exe_dir


def user_config_dir() -> Path:
    """OS の慣習的なユーザ設定の置き場所。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "voice-dictation"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else Path.home()) / "voice-dictation"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "voice-dictation"


def find_config(explicit: str | None = None) -> Path | None:
    """使う設定ファイルを 1 つ決める（見つからなければ None）。"""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {p}")
        return p
    for candidate in (base_dir() / FILENAME, user_config_dir() / FILENAME, Path.cwd() / FILENAME):
        if candidate.is_file():
            return candidate
    return None


def load(path: Path) -> dict[str, Any]:
    """ini を argparse の既定値（dest → 値）へ変換する。"""
    parser = configparser.ConfigParser()
    # ini は UTF-8 前提（Windows のメモ帳で保存しても BOM 付きで読めるようにする）
    parser.read(path, encoding="utf-8-sig")
    if not parser.has_section(SECTION):
        raise ValueError(f"{path}: [{SECTION}] セクションがありません")
    out: dict[str, Any] = {}
    for raw_key, raw_value in parser.items(SECTION):
        key = raw_key.strip().lower().replace("-", "_")
        entry = KEYS.get(key)
        if entry is None:
            print(f"[config] 未知の項目なので無視します: {raw_key}", file=sys.stderr)
            continue
        dest, convert = entry
        try:
            out[dest] = convert(raw_value)
        except ValueError as e:
            raise ValueError(f"{path}: {raw_key} の値が不正です（{e}）") from e
    return out
