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

    def selftest(self) -> str | None:
        """短い無音を実際に投げて、サーバまでの経路が生きているか確かめる。

        接続だけでなく faster-whisper が応答するところまで見る（モデル読み込み中や
        WSL2 のポート転送が効いていない場合に、起動時点で気づけるようにする）。
        問題なければ None、駄目なら人が読めるエラー文を返す。
        """
        import numpy as np

        from dictation.audio import SAMPLE_RATE, to_wav

        silence = np.zeros(SAMPLE_RATE // 4, dtype=np.int16)
        try:
            self.transcribe(to_wav([silence]))
        except requests.exceptions.ConnectionError:
            return f"{self.url} へ接続できません（サーバは起動していますか／URL は合っていますか）"
        except requests.exceptions.Timeout:
            return f"{self.url} が応答しません（モデル読み込み中かもしれません）"
        except requests.exceptions.HTTPError as e:
            return f"サーバがエラーを返しました: {e}"
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        return None

    def transcribe(self, wav: bytes) -> str:
        """WAV bytes を投げて文字起こし結果を返す。"""
        files = {"file": ("segment.wav", wav, "audio/wav")}
        r = self.session.post(self.url, files=files, timeout=self.timeout)
        r.raise_for_status()
        text: str = r.json().get("text", "")
        return text.strip()
