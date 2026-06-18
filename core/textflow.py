"""応答テキストを TTS に流す単位へ切り出すユーティリティ（IO 非依存）。

旧 voice_agent.py の `_SENT_BOUNDARY` / `_SOFT_BOUNDARY` / `_flush_sentences` /
`_flush_first` を、`Speaker` 依存をやめて「文字列を受け取るコールバック `say`」を
取る形に一般化した。サーバ側ではこの `say` が VOICEVOX 合成→WS 送信に繋がる。
"""

import re
from collections.abc import Callable

# 文の区切り（ここで TTS に流す単位を切る）
SENT_BOUNDARY = re.compile(r"[。．！？!?\n]")
# 早出し用の緩い区切り（読点を含む）。応答の1文目だけここで先に喋り出す。
SOFT_BOUNDARY = re.compile(r"[、，,。．！？!?\n]")
TASK_SENTINEL = "[[TASK]]"

Say = Callable[[str], None]


def flush_sentences(buf: str, say: Say) -> str:
    """buf の中の完成した文を say に流し、未完の端数を返す。"""
    while True:
        m = SENT_BOUNDARY.search(buf)
        if not m:
            return buf
        end = m.end()
        sentence = buf[:end].strip()
        if sentence:
            say(sentence)
        buf = buf[end:]


def flush_first(buf: str, say: Say, min_chars: int, max_chars: int) -> tuple[str, bool]:
    """応答の1文目だけ、初回の音出しを早めるために緩い基準で say に流す。
      ・句点(。！？等)が来たら無条件で流す
      ・読点(、,)なら最小文字数を超えたとき流す（短すぎる細切れを避ける）
      ・どちらも来なくても上限文字数に達したら、そこで区切って流す
    早出しできたら (残り, True)、まだ流せないなら (buf, False) を返す。"""
    hard = SENT_BOUNDARY.search(buf)
    soft = SOFT_BOUNDARY.search(buf)
    if hard:
        end = hard.end()
    elif soft and soft.end() >= min_chars:
        end = soft.end()
    elif len(buf) >= max_chars:
        end = max_chars
    else:
        return buf, False
    chunk = buf[:end].strip()
    if chunk:
        say(chunk)
        return buf[end:], True
    # 区切りはあったが中身が空白だけ → 区切りを捨てて継続（まだ喋っていない）
    return buf[end:], False
