"""TTS（VOICEVOX）クライアント。合成だけ行い、再生はしない。

旧 voice_agent.py の `Speaker._synth` 相当。サーバはここで得た wav の bytes を
WebSocket でブラウザへ送り、再生はブラウザの AudioContext が担う。
"""

import contextlib
import json
import time

import requests

import config as C


class VoicevoxClient:
    """テキスト → wav(bytes) を返す。audio_query → synthesis の2段（VOICEVOX 標準）。"""

    def synth(self, text: str, speaker: int | None = None, speed: float | None = None) -> bytes:
        text = text.strip()
        if not text:
            return b""
        spk = C.VOICEVOX_SPEAKER if speaker is None else speaker
        _t0 = time.monotonic()
        query_params: dict[str, str | int] = {"text": text, "speaker": spk}
        q = requests.post(
            f"{C.VOICEVOX_URL}/audio_query",
            params=query_params,
            timeout=30,
        )
        q.raise_for_status()
        query = q.json()
        query["speedScale"] = C.VOICEVOX_SPEED if speed is None else speed
        query["volumeScale"] = C.VOICEVOX_VOLUME
        r = requests.post(
            f"{C.VOICEVOX_URL}/synthesis",
            params={"speaker": spk},
            data=json.dumps(query),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        if C.TURN_TIMING:
            print(f"[tts] 合成 {time.monotonic() - _t0:.2f}s（{len(text)}字）")
        return r.content

    def warmup(self) -> None:
        """初回合成の JIT を温める（失敗は無視。本番ループの妨げにしない）。"""
        with contextlib.suppress(Exception):
            self.synth("あ")
