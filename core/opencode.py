"""opencode serve への作業委譲（[[TASK]] 用）。

旧 voice_agent.py の `OpenCode` / `_extract_text` を移設（ロジックは不変）。
"""

from typing import Any

import requests

import config as C


class OpenCode:
    def __init__(self) -> None:
        self.session_id: str | None = None

    def _ensure_session(self) -> None:
        if self.session_id:
            return
        r = requests.post(f"{C.OPENCODE_BASE_URL}/session", json={}, timeout=30)
        r.raise_for_status()
        self.session_id = r.json()["id"]

    def run(self, instruction: str) -> str:
        """opencode に作業を投げ、応答テキストを返す（同期）。"""
        self._ensure_session()
        body: dict[str, Any] = {
            "providerID": C.OPENCODE_PROVIDER_ID,
            "modelID": C.OPENCODE_MODEL_ID,
            "parts": [{"type": "text", "text": instruction}],
        }
        r = requests.post(
            f"{C.OPENCODE_BASE_URL}/session/{self.session_id}/message",
            json=body,
            timeout=600,
        )
        r.raise_for_status()
        return _extract_text(r.json())


def _extract_text(obj: Any) -> str:
    """opencode の応答 JSON から text パートをかき集める（版差に対する保険つき）。"""
    texts: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("type") == "text" and isinstance(x.get("text"), str):
                texts.append(x["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return "\n".join(t for t in texts if t.strip()).strip() or "(応答が空でした)"
