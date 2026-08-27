"""`python -m dictation` および exe の入口。

exe をダブルクリックで起動した場合、エラーで即座にウィンドウが閉じると原因が読めないため、
異常終了のときだけ Enter 待ちで画面を残す。
"""

import contextlib
import sys

from dictation.cli import main

if __name__ == "__main__":
    code = main()
    if code != 0 and getattr(sys, "frozen", False):
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("\nEnter キーで終了します…")
    sys.exit(code)
