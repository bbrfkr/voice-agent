# syntax=docker/dockerfile:1
#
# voice-agent 実行用イメージ（Linux コンテナ・Web サーバ）
#   - faster-whisper(CTranslate2) の CUDA 実行のため cuDNN 付き CUDA ランタイムを土台にする
#     （cuBLAS / cuDNN9 はこのイメージが提供するので runtime の nvidia-* wheel は入れない）
#   - 音声の入出力はブラウザ（getUserMedia / AudioContext）が担うため、PortAudio/ALSA/
#     PulseAudio 系のシステム依存は一切入れない。音声デコードは faster-whisper が連れてくる
#     PyAV(av) が担う。
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ── システム依存（最小） ──
#   python3      : 実行ランタイム（Ubuntu 22.04 = 3.10、コードは 3.10+ 対応）
#   ca-certificates : 外部 API(HTTPS) 用
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python 依存（pyproject.toml の runtime グループ。cuBLAS/cuDNN は土台イメージが提供） ──
# pip>=25.1 が PEP 735 の --group に対応。
COPY pyproject.toml /app/pyproject.toml
RUN python3 -m pip install --upgrade "pip>=25.1" \
    && python3 -m pip install --group runtime

# ── 実行に必要なコードを COPY ──
# config.py は env 駆動のローダ（秘密は持たない）。実値は .env から runtime 注入する。
COPY config.py /app/config.py
COPY core /app/core
COPY server /app/server

# CUDA(faster-whisper) 既定。compose の environment で上書き可。
# HF_HOME: faster-whisper(Whisper モデル) の DL 先を固定。compose がここへ bind mount して
#          永続化する（未マウントだと毎回コンテナ破棄で消え、起動毎に再 DL になる）。
ENV WHISPER_DEVICE=cuda \
    VOICEVOX_URL=http://voicevox:50021 \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    HF_HOME=/cache/hf

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
