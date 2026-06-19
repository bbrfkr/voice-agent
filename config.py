"""
設定ローダ（このファイルは編集しない）。

設定値はすべて **環境変数 / `.env` から読み込む**。値を変えたいときは
このファイルではなく、リポジトリ直下の **`.env`** を編集すること。

    cp .env.example .env     # 初回だけ。中身を自分の環境に合わせて編集

優先順位:  実際の環境変数 > `.env` > このファイルの既定値
（docker compose では compose が環境変数を注入するので、コンテナ内に `.env` は不要。）

秘密情報（LLAMA_API_KEY 等）はこのファイルに書かない＝`.env` にだけ置く
（`.env` は Git にもイメージにも入れない）。
"""

import os
from typing import overload


# ───────────────────────── .env ローダ / 型付き取得（依存なし） ─────────────────────────
def _load_dotenv():
    """このファイルと同じディレクトリの `.env` を読み、未設定の環境変数だけ埋める
    （実際の環境変数が優先）。docker のように環境変数が既に入っていれば何もしない。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)
    except FileNotFoundError:
        pass


# default と同じ型で返すことを型レベルでも示す（呼び出し側の定数が正しい型で効く）。
# bool は int の派生なので bool→int→float→str の順に並べる。
@overload
def _env(name: str, default: bool) -> bool: ...
@overload
def _env(name: str, default: int) -> int: ...
@overload
def _env(name: str, default: float) -> float: ...
@overload
def _env(name: str, default: str) -> str: ...
def _env(name, default):
    """環境変数 name を default と同じ型で取り出す（未設定・空なら default）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


_load_dotenv()

# ───────────────────────── Web サーバ（FastAPI / uvicorn） ─────────────────────────
# ブラウザ（Web UI）を被せて、マイク録音と音声再生はブラウザ側で行う。サーバは STT/LLM/
# TTS のオーケストレーションと静的配信だけを担い、PulseAudio/PortAudio には一切触れない。
SERVER_HOST = _env("SERVER_HOST", "0.0.0.0")  # 0.0.0.0 で LAN からも到達可（公開時は要 TLS/認証）
SERVER_PORT = _env("SERVER_PORT", 8000)
# 配信する Web UI（静的ファイル）のディレクトリ。既定はリポジトリ内 server/static。
STATIC_DIR = _env("STATIC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "server", "static"))

# ───────────────────────── STT（faster-whisper） ─────────────────────────
WHISPER_MODEL = _env("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = _env("WHISPER_DEVICE", "cuda")  # cuda / cpu
WHISPER_COMPUTE = _env("WHISPER_COMPUTE", "float16")  # VRAM 節約は "int8_float16"
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "ja")
WHISPER_BEAM_SIZE = _env("WHISPER_BEAM_SIZE", 1)  # 1=greedy（最速）
WHISPER_VAD_FILTER = _env("WHISPER_VAD_FILTER", False)
STT_TIMING = _env("STT_TIMING", True)
TURN_TIMING = _env("TURN_TIMING", True)  # LLM の初トークン/初音出し/初回合成の所要を表示
RMS_DEBUG = _env("RMS_DEBUG", True)  # 発話/無音の RMS 分布を表示（SILENCE_RMS 調整用）
WARMUP = _env("WARMUP", True)  # 起動時に VOICEVOX/LLM を1回温め、初回の遅延を吸収

# ───────────────────────── 会話 LLM（OpenAI 互換 / llama.cpp 等） ─────────────────────────
LLAMA_BASE_URL = _env("LLAMA_BASE_URL", "http://127.0.0.1:8080/v1")
LLAMA_API_KEY = _env("LLAMA_API_KEY", "")  # ★秘密。.env にだけ書く
LLAMA_MODEL = _env("LLAMA_MODEL", "gemma")
LLAMA_TEMPERATURE = _env("LLAMA_TEMPERATURE", 0.7)
LLAMA_MAX_TOKENS = _env("LLAMA_MAX_TOKENS", 512)
# 会話履歴の上限（system を除くメッセージ件数。20 ≒ 直近10往復）。
# 履歴が伸び続けるとプロンプトが長くなり TTFT がじわじわ悪化するため刈り込む。0 で無制限。
LLAMA_MAX_HISTORY = _env("LLAMA_MAX_HISTORY", 20)
# reasoning(思考)モードを切る（Qwen3 等の thinking 対応モデル向け）。
# thinking 中は音声に流せるテキストが出ず TTFT がまるごと延びるため、音声対話では切るのが正解。
# chat_template_kwargs を解さないバックエンドでエラーになる場合は false にする。
LLAMA_DISABLE_THINKING = _env("LLAMA_DISABLE_THINKING", False)

# 応答の「早出し」（初回の音出しまでの無音を縮める）。
# 応答の1文目だけ、句点を待たず読点や文字数の区切りでも先に TTS へ流す。
# 2文目以降は従来どおり句点単位（1文目を喋る裏で生成されるので無音にならない）。
FIRST_FLUSH_MIN_CHARS = _env("FIRST_FLUSH_MIN_CHARS", 8)  # 読点で早出しする最小文字数（細切れ防止）
FIRST_FLUSH_MAX_CHARS = _env("FIRST_FLUSH_MAX_CHARS", 24)  # 区切りが来なくてもこの長さで区切って流す

# 固定コンテキスト（人格＋作業委譲ルール）。
# 変えたいときは .env に SYSTEM_PROMPT="..." を1行で書くか、
# SYSTEM_PROMPT_FILE=/path/to/prompt.txt でファイル指定する（複数行はこちらが楽）。
_DEFAULT_SYSTEM_PROMPT = """あなたは日本語で話す、親しみやすい音声アシスタントです。
返答は短く、話し言葉で、音声で聞いて自然な長さにしてください（基本1〜3文）。

【重要なルール】
ユーザーが「PC上で実際の作業をしてほしい」と頼んでいる場合
（例: コードを書く/直す、コマンドを実行する、ファイルを操作する、調べてまとめる 等）は、
返答の先頭に必ず半角で [[TASK]] と書き、続けて作業内容を1行の指示文で書いてください。
それ以外は何も付けず、普通に会話してください。

例:
ユーザー「今日はいい天気だね」→「ほんとですね、気持ちいい一日になりそう」
ユーザー「さっきのバグ直しといて」→「[[TASK]] さっき話していたバグを修正する」
"""
SYSTEM_PROMPT = _env("SYSTEM_PROMPT", _DEFAULT_SYSTEM_PROMPT)
if _env("SYSTEM_PROMPT_FILE", ""):
    with open(os.environ["SYSTEM_PROMPT_FILE"], encoding="utf-8") as _f:
        SYSTEM_PROMPT = _f.read()

SUMMARIZE_PROMPT = _env(
    "SUMMARIZE_PROMPT",
    "上の作業結果を、音声で聞いて分かるように1〜2文で短く要約してください。",
)

# ───────────────────────── Discord 会話ログ ─────────────────────────
# 会話ログ（あなたの発話 / AI の応答）を Discord Webhook へ送る。
# 「あなた」と AI で**別の Webhook URL**にすると、Discord 側で投稿者名・アイコンが
# 分かれて会話ログとして読みやすい（同じチャンネルに2本 Webhook を作るのが楽）。
# 片方だけ設定した場合は両方そちらへ送り、発話者名を本文に前置して区別する。
# 両方空ならこの機能は無効。URL は秘密情報なので .env にだけ書く。
DISCORD_WEBHOOK_URL_USER = _env("DISCORD_WEBHOOK_URL_USER", "")  # ★秘密。.env にだけ書く
DISCORD_WEBHOOK_URL_AI = _env("DISCORD_WEBHOOK_URL_AI", "")  # ★秘密。.env にだけ書く

# ───────────────────────── ログモード（STT → Discord 直送） ─────────────────────────
# Web UI のログモード切替（トグル）を ON にすると、その発話を LLM・TTS を一切挟まず
# STT 結果のまま専用 Webhook へ POST する（音声メモ・口述筆記用）。
# 送信先は会話ログとは**別の Webhook URL**（別チャンネルに作るのが楽）。
# 未設定だとログモードは使えない（UI でその旨を案内する）。
DISCORD_WEBHOOK_URL_LOGMODE = _env("DISCORD_WEBHOOK_URL_LOGMODE", "")  # ★秘密。.env にだけ書く

# ───────────────────────── opencode serve（作業エージェント） ─────────────────────────
OPENCODE_BASE_URL = _env("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
OPENCODE_PROVIDER_ID = _env("OPENCODE_PROVIDER_ID", "llamacpp")
OPENCODE_MODEL_ID = _env("OPENCODE_MODEL_ID", "gemma")

# ───────────────────────── TTS（VOICEVOX） ─────────────────────────
# docker では compose が VOICEVOX_URL=http://voicevox:50021 を注入する。
VOICEVOX_URL = _env("VOICEVOX_URL", "http://127.0.0.1:50021")
VOICEVOX_SPEAKER = _env("VOICEVOX_SPEAKER", 3)  # 話者ID（/speakers で一覧確認）
VOICEVOX_SPEED = _env("VOICEVOX_SPEED", 1.0)  # 話速
VOICEVOX_VOLUME = _env("VOICEVOX_VOLUME", 1.0)  # 音量倍率。2.0 を超える辺りから音割れに注意

# 注: プッシュトゥトーク + ブラウザ録音/再生に移行したため、ウェイクワード・サーバ側 VAD・
# 合図(ACK)・バージイン監視・フォローアップ窓に関する設定は廃止した（録音開始/終了は
# ブラウザのボタンが、再生はブラウザの AudioContext が担い、バージインは「再生中に押す＝
# cancel 送信」で実現する）。
