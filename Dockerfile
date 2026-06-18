# syntax=docker/dockerfile:1
#
# voice-agent bot 実行用イメージ（軽量・GPU 不要）。
#   - 音声 I/O は Discord ボイスチャンネルで行う（マイク/スピーカーや PulseAudio は不要）。
#   - STT(faster-whisper) と TTS(VOICEVOX) は GPU サーバ側の HTTP サービスに分離してあり、
#     この bot は HTTP で叩くだけなので CUDA も重いモデルも持たない。
#   - libopus / libsndfile が voice 送受信と WAV デコードに必要。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ── システム依存 ──
#   libopus0    : discord の voice 送受信（opus エンコード/デコード）
#   libsndfile1 : soundfile（VOICEVOX が返す WAV のデコード）
#   ffmpeg      : discord.py が voice で利用しうる（保険。無くても本実装の生 PCM 再生は動く）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopus0 \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python 依存（pyproject.toml の runtime グループ）。pip>=25.1 が PEP 735 の --group に対応。 ──
COPY pyproject.toml /app/pyproject.toml
RUN python3 -m pip install --upgrade "pip>=25.1" \
    && python3 -m pip install --group runtime

# ── 実行に必要なコードを COPY（config.py は env 駆動のローダ。実値は .env から runtime 注入） ──
COPY voice_agent.py /app/voice_agent.py
COPY discord_agent.py /app/discord_agent.py
COPY config.py /app/config.py

CMD ["python3", "discord_agent.py"]
