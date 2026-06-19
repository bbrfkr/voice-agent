"""会話ログ / ログモードの Discord Webhook 送信。

旧 voice_agent.py の `DiscordLogger` を移設（ロジックは不変）。キュー + 別スレッドの
投げ捨て式で、送信時間をターンのレイテンシに乗せない。送信失敗は stderr に出すだけ。
"""

import queue
import sys
import threading
import time

import requests

import config as C


class DiscordLogger:
    """会話ログを Discord Webhook へ POST する。
    「あなた」と AI で別の Webhook URL（DISCORD_WEBHOOK_URL_USER / _AI）を使うと、
    Discord 側で投稿者が分かれて読みやすい。片方しか設定されていなければ両方そちらへ
    送り、発話者名を本文に前置して区別する。両方空なら無効。
    ログモード（STT 直送）は別の Webhook（DISCORD_WEBHOOK_URL_LOGMODE）へ送る。"""

    _LIMIT = 1900  # Discord の content 上限 2000 字への安全マージン

    def __init__(self) -> None:
        user_url = C.DISCORD_WEBHOOK_URL_USER
        ai_url = C.DISCORD_WEBHOOK_URL_AI
        log_url = C.DISCORD_WEBHOOK_URL_LOGMODE
        self._urls = {"user": user_url or ai_url, "ai": ai_url or user_url, "log": log_url}
        self._shared = not (user_url and ai_url)  # URL 共用時は発話者名を前置
        self.enabled = bool(user_url or ai_url)
        self.log_enabled = bool(log_url)
        self.q: queue.Queue[tuple[str, str]] = queue.Queue()
        if self.enabled or self.log_enabled:
            threading.Thread(target=self._run, daemon=True).start()
        if self.enabled:
            print(
                "[discord] 会話ログ送信を有効化"
                + ("（単一 Webhook・発話者名を前置）" if self._shared else "（あなた/AI 別 Webhook）")
            )
        if self.log_enabled:
            print("[discord] ログモードの送信先を有効化")

    def user(self, text: str) -> None:
        self._post("user", text)

    def ai(self, text: str) -> None:
        self._post("ai", text)

    def log(self, text: str) -> None:
        """ログモード: STT 結果をそのまま専用 Webhook へ送る（発話者名は付けない）。"""
        self._post("log", text)

    def _post(self, role: str, text: str) -> None:
        text = (text or "").strip()
        url = self._urls.get(role)
        if not url or not text:
            return
        if role != "log" and self._shared:
            name = "あなた" if role == "user" else "VOICEVOXエージェント"
            text = f"**{name}**: {text}"
        for i in range(0, len(text), self._LIMIT):
            self.q.put((url, text[i : i + self._LIMIT]))

    def _run(self) -> None:
        while True:
            url, content = self.q.get()
            try:
                r = requests.post(url, json={"content": content}, timeout=10)
                if r.status_code == 429:  # レート制限: 指定秒だけ待って1回だけ再送
                    time.sleep(float(r.headers.get("Retry-After", "1")) + 0.5)
                    r = requests.post(url, json={"content": content}, timeout=10)
                r.raise_for_status()
            except Exception as e:
                print(f"[discord] 送信失敗（無視）: {e}", file=sys.stderr)
            finally:
                self.q.task_done()
