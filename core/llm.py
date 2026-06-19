"""会話 LLM（llama.cpp / OpenAI 互換）クライアント。

旧 voice_agent.py の `llm_stream` と `_trim_history` を移設（ロジックは不変）。
"""

import json
from collections.abc import Iterator

import requests

import config as C

Message = dict[str, str]


def llm_stream(messages: list[Message]) -> Iterator[str]:
    """OpenAI 互換 /chat/completions を stream で叩き、トークン(delta)を yield する。"""
    payload: dict = {
        "model": C.LLAMA_MODEL,
        "messages": messages,
        "temperature": C.LLAMA_TEMPERATURE,
        "max_tokens": C.LLAMA_MAX_TOKENS,
        "stream": True,
    }
    if C.LLAMA_DISABLE_THINKING:
        # thinking を切らないと、思考トークンを吐き終わるまで content が来ず
        # 初回の音出しがまるごと遅れる（実測で TTFT 2.4s → 0.7s）。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Authorization": f"Bearer {C.LLAMA_API_KEY}"}
    with requests.post(
        f"{C.LLAMA_BASE_URL}/chat/completions",
        json=payload,
        headers=headers,
        stream=True,
        timeout=120,
    ) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


def trim_history(messages: list[Message]) -> None:
    """system(先頭) + 直近 LLAMA_MAX_HISTORY 件だけ残す。長時間の会話で
    プロンプトが伸び続けて TTFT が悪化するのを防ぐ。タスクの「作業結果」全文も
    ここで自然に押し出される。先頭が assistant 始まりにならないよう調整する。"""
    keep = C.LLAMA_MAX_HISTORY
    if keep <= 0 or len(messages) - 1 <= keep:
        return
    del messages[1 : len(messages) - keep]
    while len(messages) > 1 and messages[1]["role"] != "user":
        del messages[1]
