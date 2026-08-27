"""アクティブウィンドウへ文字を打ち込むバックエンド。

OS のキーイベント注入は「アクティブウィンドウを持つデスクトップ上のプロセス」からしか
できない。したがってこのモジュールは WSL2/Docker のサーバ側ではなく、**入力先の OS で
動くクライアント**から使う。

方式は Unicode 直接打鍵（IME をバイパスして文字そのものを送る）。日本語でも変換候補
ウィンドウと干渉せず、1 文字ずつ流し込めるのが利点。
"""

import sys
from typing import Protocol


class TextInjector(Protocol):
    """文字列をアクティブウィンドウへ送るバックエンドの共通インタフェース。"""

    def type_text(self, text: str) -> None: ...


def create_injector(backend: str = "auto", char_delay_ms: int = 0) -> TextInjector:
    """プラットフォームに合ったインジェクタを返す。

    backend: "auto" / "windows" / "macos"
    char_delay_ms: 1 文字ごとに挟む待ち（0 なら一括送出）。取りこぼすアプリ向けの逃げ道。
    """
    if backend == "auto":
        backend = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "")
    if backend == "windows":
        from dictation.inject_windows import WindowsInjector

        return WindowsInjector(char_delay_ms=char_delay_ms)
    if backend == "macos":
        from dictation.inject_macos import MacInjector

        return MacInjector(char_delay_ms=char_delay_ms)
    raise RuntimeError(
        f"このプラットフォーム（{sys.platform}）向けの入力バックエンドはありません。"
        "対応は Windows と macOS です（--backend で明示指定もできます）。"
    )
