"""Windows 向けの Unicode 打鍵（SendInput + KEYEVENTF_UNICODE）。

仮想キーコードではなく Unicode コードポイントを直接送るため、IME の状態に左右されず
日本語をそのまま入力できる。文字は UTF-16 コードユニット単位で送る（サロゲートペアは
上位・下位をそれぞれ 1 イベントとして送れば Windows 側が合成する）。

このモジュールは Windows でのみ import される（`inject.create_injector` が遅延 import する）。
"""

import ctypes
import sys
import time
from ctypes import wintypes
from typing import Any

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D

# SendInput の構造体定義（64bit では dwExtraInfo がポインタ幅である必要がある）
ULONG_PTR = ctypes.c_size_t


class _KeyBdInput(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _MouseInput(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _HardwareInput(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD))


class _InputUnion(ctypes.Union):
    _fields_ = (("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput))


class _Input(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _InputUnion))


def _key_event(scan: int, vk: int, flags: int) -> _Input:
    ki = _KeyBdInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return _Input(type=INPUT_KEYBOARD, u=_InputUnion(ki=ki))


def _events_for_char(ch: str) -> list[_Input]:
    """1 文字を送るためのキーイベント列（押下＋解放）を作る。"""
    if ch == "\n":
        # Unicode の改行は多くのアプリで無視されるので、Enter キーとして送る
        return [_key_event(0, VK_RETURN, 0), _key_event(0, VK_RETURN, KEYEVENTF_KEYUP)]
    events: list[_Input] = []
    raw = ch.encode("utf-16-le")  # サロゲートペアは 2 コードユニットになる
    for i in range(0, len(raw), 2):
        code = raw[i] | (raw[i + 1] << 8)
        events.append(_key_event(code, 0, KEYEVENTF_UNICODE))
        events.append(_key_event(code, 0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


class WindowsInjector:
    """SendInput でアクティブウィンドウへ文字を流し込む。"""

    #: 1 回の SendInput で送るイベント数の上限（大きすぎると取りこぼすアプリがある）
    BATCH = 128

    def __init__(self, char_delay_ms: int = 0) -> None:
        self.char_delay = char_delay_ms / 1000.0
        self._warned = False
        # ctypes.windll は Windows にしか存在しない属性。Linux 上の mypy でも型チェックが
        # 通るよう属性アクセスを避けて引く（このクラス自体 Windows でしか生成されない）。
        user32: Any = ctypes.__dict__["windll"].user32
        self._send_input = user32.SendInput
        self._send_input.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
        self._send_input.restype = wintypes.UINT

    def type_text(self, text: str) -> None:
        if self.char_delay > 0:
            for ch in text:
                self._send(_events_for_char(ch))
                time.sleep(self.char_delay)
            return
        batch: list[_Input] = []
        for ch in text:
            batch.extend(_events_for_char(ch))
            if len(batch) >= self.BATCH:
                self._send(batch)
                batch = []
        if batch:
            self._send(batch)

    def _send(self, events: list[_Input]) -> None:
        n = len(events)
        arr = (_Input * n)(*events)
        sent = self._send_input(n, arr, ctypes.sizeof(_Input))
        if sent != n and not self._warned:
            self._warned = True
            print(
                "[inject] キー入力がブロックされました。入力先のアプリが管理者権限で動いている場合は、"
                "このクライアントも管理者として実行してください。",
                file=sys.stderr,
            )
