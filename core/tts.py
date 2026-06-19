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

    def speakers(self) -> list[dict[str, object]]:
        """VOICEVOX の話者一覧を取得し、UI 用に「話者名（スタイル名）→ 話者ID」へ平坦化する。

        VOICEVOX の /speakers は話者ごとに styles（スタイル名と id）を持つネスト構造を返す。
        合成 API が受け取るのはこの style の id（整数）なので、人が選びやすいラベルと id の
        対応リスト [{"id": int, "label": str}, ...] を話者名→id 昇順で返す。
        """
        r = requests.get(f"{C.VOICEVOX_URL}/speakers", timeout=10)
        r.raise_for_status()
        items: list[tuple[str, int]] = []
        for spk in r.json():
            name = spk.get("name", "")
            for style in spk.get("styles", []):
                sid = style.get("id")
                if sid is None:
                    continue
                style_name = style.get("name", "")
                label = f"{name}（{style_name}）" if style_name else name
                items.append((label, int(sid)))
        items.sort()  # 話者名（スタイル名）のラベル順、同名は id 順
        return [{"id": sid, "label": label} for label, sid in items]

    def warmup(self) -> None:
        """初回合成の JIT を温める（失敗は無視。本番ループの妨げにしない）。"""
        with contextlib.suppress(Exception):
            self.synth("あ")
