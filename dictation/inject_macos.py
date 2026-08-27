"""macOS 向けの Unicode 打鍵（Quartz CGEvent）。

`CGEventKeyboardSetUnicodeString` はキーコードではなく文字列そのものをイベントに載せる
ため、Windows の KEYEVENTF_UNICODE と同じく IME をバイパスして日本語を直接入力できる。

必要なもの:
  - `pyobjc-framework-Quartz`
  - システム設定 → プライバシーとセキュリティ → アクセシビリティ で、このクライアントを
    実行するアプリ（ターミナル等）に許可を与えること。許可が無いとイベントは黙って捨てられる。

このモジュールは macOS でのみ import される（`inject.create_injector` が遅延 import する）。
"""

import time
from typing import Any

#: 1 イベントに載せる UTF-16 コードユニット数の上限。長い文字列を 1 イベントで送ると
#: 取りこぼすことがあるため、短く刻んで複数イベントに分ける。
CHUNK_UNITS = 16


def _chunks(text: str) -> list[str]:
    """UTF-16 コードユニット数で刻む（サロゲートペアを途中で割らない）。"""
    out: list[str] = []
    cur = ""
    units = 0
    for ch in text:
        n = len(ch.encode("utf-16-le")) // 2
        if units + n > CHUNK_UNITS and cur:
            out.append(cur)
            cur, units = "", 0
        cur += ch
        units += n
    if cur:
        out.append(cur)
    return out


class MacInjector:
    """CGEvent でアクティブウィンドウへ文字を流し込む。"""

    def __init__(self, char_delay_ms: int = 0) -> None:
        self.char_delay = char_delay_ms / 1000.0
        import Quartz

        self._q: Any = Quartz

    def type_text(self, text: str) -> None:
        pieces = list(text) if self.char_delay > 0 else _chunks(text)
        for piece in pieces:
            self._post(piece)
            if self.char_delay > 0:
                time.sleep(self.char_delay)

    def _post(self, piece: str) -> None:
        q = self._q
        for down in (True, False):
            ev = q.CGEventCreateKeyboardEvent(None, 0, down)
            q.CGEventKeyboardSetUnicodeString(ev, len(piece), piece)
            q.CGEventPost(q.kCGHIDEventTap, ev)
