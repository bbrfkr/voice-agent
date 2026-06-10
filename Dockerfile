# syntax=docker/dockerfile:1
#
# voice-agent 実行用イメージ（Linux コンテナ）
#   - faster-whisper(CTranslate2) の CUDA 実行のため cuDNN 付き CUDA ランタイムを土台にする
#     （cuBLAS / cuDNN9 はこのイメージが提供するので requirements の nvidia-* wheel は入れない）
#   - マイク/スピーカーは PulseAudio(TCP) 経由でホスト(Windows)に繋ぐ前提（DOCKER.md 参照）
#     → ALSA のデフォルト出力を pulse プラグインに向け、PvRecorder/PortAudio とも pulse に流す
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ── システム依存 ──
#   python3            : 実行ランタイム（Ubuntu 22.04 = 3.10、コードは 3.10+ 対応）
#   libsndfile1        : soundfile
#   libportaudio2      : sounddevice(PortAudio)
#   libasound2*        : ALSA 本体 + pulse プラグイン（PortAudio→pulse 経路）
#   libpulse0          : PvRecorder(miniaudio) / pulse クライアント
#   alsa/pulse utils   : 疎通確認(paplay/arecord 等)用
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        libsndfile1 \
        libportaudio2 \
        libasound2 libasound2-plugins alsa-utils \
        libpulse0 pulseaudio-utils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── ALSA のデフォルトデバイスを PulseAudio に向ける ──
# これで PortAudio(sounddevice) も ALSA 経由で pulse に流れる。PvRecorder は直接 pulse を掴む。
RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf

WORKDIR /app

# ── Python 依存（nvidia-* wheel は土台イメージが提供するので除外） ──
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install \
        "faster-whisper>=1.0.0" \
        "openwakeword>=0.6.0" \
        "pvrecorder>=1.2.0" \
        "sounddevice>=0.4.6" \
        "soundfile>=0.12.1" \
        "numpy>=1.24" \
        "requests>=2.31"

# ── openWakeWord 共有特徴モデルをビルド時に取得（初回起動の DL 待ちを無くす） ──
RUN python3 -c "import openwakeword.utils as u; u.download_models()"

# ── 実行に必要なコードを COPY ──
# config.py は env 駆動のローダ（秘密は持たない）。実値は .env から runtime 注入する。
# モデルは compose で bind mount する。
COPY voice_agent.py /app/voice_agent.py
COPY config.py /app/config.py

# CUDA(faster-whisper) 既定。compose の environment で上書き可。
# HF_HOME: faster-whisper(Whisper モデル) の DL 先を固定。compose がここへ bind mount して
#          永続化する（未マウントだと毎回コンテナ破棄で消え、起動毎に再 DL になる）。
ENV WHISPER_DEVICE=cuda \
    OWW_MODEL_PATH=/app/zundamon.onnx \
    OWW_FRAMEWORK=onnx \
    VOICEVOX_URL=http://voicevox:50021 \
    HF_HOME=/cache/hf

CMD ["python3", "voice_agent.py"]
