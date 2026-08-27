"""voice-agent サーバの STT 単体 API（`POST /api/transcribe`）を叩くクライアント。

サーバ側は faster-whisper を 1 本のロックで直列化しているので、こちらも 1 接続を使い回して
順番に投げる。`requests.Session` により TCP/TLS の張り直しが起きず、LAN なら往復のオーバー
ヘッドはほぼ無視できる。
"""

import requests


class TranscribeClient:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.url = base_url.rstrip("/") + "/api/transcribe"
        self.timeout = timeout
        self.session = requests.Session()

    def transcribe(self, wav: bytes) -> str:
        """WAV bytes を投げて文字起こし結果を返す。"""
        files = {"file": ("segment.wav", wav, "audio/wav")}
        r = self.session.post(self.url, files=files, timeout=self.timeout)
        r.raise_for_status()
        text: str = r.json().get("text", "")
        return text.strip()
