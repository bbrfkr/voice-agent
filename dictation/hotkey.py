"""グローバルなプッシュトゥトーク（PTT）キーの監視。

pynput のリスナーは Windows / macOS の両方で動く（macOS はアクセシビリティ権限が必要）。
既定キーは `scripts/` のホットキー用サンプルと揃えて F13（F11 全画面・F12 開発者ツール等と
衝突しにくい）。
"""

from collections.abc import Callable
from typing import Any


def parse_key(name: str) -> Any:
    """`f13` や `ctrl_r` のような名前を pynput のキーへ変換する（1 文字なら文字キー）。"""
    from pynput import keyboard

    key = name.strip().lower()
    if len(key) == 1:
        return keyboard.KeyCode.from_char(key)
    special = getattr(keyboard.Key, key, None)
    if special is None:
        raise ValueError(f"不明なキー名です: {name}（例: f13, f9, scroll_lock, ctrl_r）")
    return special


class PttListener:
    """指定キーの押下中だけ `on_press` → `on_release` の区間を作る。

    OS のキーリピートで押下イベントが連続して届くため、押しっぱなしの間に何度 `on_press`
    が呼ばれても Recorder 側で無視される（`Recorder.start` が冪等）。
    """

    def __init__(self, key_name: str, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        self.key = parse_key(key_name)
        self.key_name = key_name
        self._on_press = on_press
        self._on_release = on_release
        self._listener: Any = None

    def _matches(self, key: Any) -> bool:
        return bool(key == self.key)

    def run(self) -> None:
        """キーイベントを待ち受ける（Ctrl+C で抜けるまでブロックする）。"""
        from pynput import keyboard

        def pressed(key: Any) -> None:
            if self._matches(key):
                self._on_press()

        def released(key: Any) -> None:
            if self._matches(key):
                self._on_release()

        with keyboard.Listener(on_press=pressed, on_release=released) as listener:
            self._listener = listener
            listener.join()

    def stop(self) -> None:
        """`run()` のブロックを解除する（終了ホットキーから呼ばれる）。"""
        if self._listener is not None:
            self._listener.stop()


class QuitHotkey:
    """アプリを終了させるためのホットキー（例: `<ctrl>+<alt>+q`）。

    macOS の `.app` や Windows のウィンドウ無し起動では Ctrl+C が使えないため、
    キーボードから確実に終了できる経路を用意する。`start()` は即座に戻る。
    """

    def __init__(self, combo: str, on_quit: Callable[[], None]) -> None:
        from pynput import keyboard

        # 不正な組み合わせはここで弾く（起動後に無言で効かないのを避ける）
        try:
            keyboard.HotKey.parse(combo)
        except ValueError as e:
            raise ValueError(f"終了ホットキーの書式が不正です: {combo}（例: <ctrl>+<alt>+q）") from e
        self.combo = combo
        self._on_quit = on_quit
        self._listener: Any = None

    def start(self) -> None:
        from pynput import keyboard

        self._listener = keyboard.GlobalHotKeys({self.combo: self._on_quit})
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
