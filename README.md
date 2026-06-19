# voice-agent — ブラウザから使う低遅延・音声エージェント

ブラウザでプッシュトゥトーク（ボタン押下中だけ話す）→ 音声認識（faster-whisper）→
会話LLM（llama.cpp）→ 音声合成（VOICEVOX）を **ストリーミングでパイプライン**し、
低遅延（最初の音まで ≈1.5〜2.5s）で返す。「〜して」などの作業依頼を検出したときだけ
**opencode** に委譲して実作業させる。

音声の入出力は**ブラウザ**（`getUserMedia` / `AudioContext`）が担うため、**PulseAudio に
依存しない**。サーバ（FastAPI）は STT→LLM→TTS のオーケストレーションと Web UI 配信だけを行う。
LAN 上のどの端末からでも（マイクのセキュアコンテキスト制約に注意。下記）操作できる。

```
[ブラウザ]  マイク録音(PTT) / 音声再生              [FastAPI サーバ]              [外部]
     │  ──── WebSocket(音声 binary) ────►  faster-whisper(STT, GPU)
     │                                          │
     │  ◄─── WebSocket(wav + JSON) ─────  オーケストレータ ──► llama.cpp(LLM, stream)
     │   会話/タスク/ログを画面表示            ├ 通常会話 : 文ごとに VOICEVOX 合成 → wav を WS で返す
     ▼                                          ├ [[TASK]] : opencode serve に委譲 → 結果をLLMが音声で要約
  AudioContext で順番に再生                     └ ログモード: STT 結果を Discord へ直送
```

## 構成と配置

| 役割 | 何を | どこで |
|---|---|---|
| マイク録音 / 音声再生 | ブラウザ（getUserMedia / AudioContext） | **任意の端末のブラウザ** |
| Web UI / API / WS | FastAPI（uvicorn） | サーバ（WSL2 / Docker） |
| STT | faster-whisper large-v3-turbo（GPU） | サーバ |
| 会話LLM | llama.cpp server（OpenAI互換） | LAN（既存サーバ） |
| 作業エージェント | opencode serve | LAN（既存サーバ） |
| TTS | VOICEVOX engine（GPU） | サーバ（compose） |

## クイックスタート（Docker Compose・推奨）

`voice-agent`（FastAPI）本体と `VOICEVOX engine`（GPU版コンテナ）を **docker compose で
まとめて**起動する。GPU は Docker Desktop の WSL2 backend 経由で `gpus: all` がそのまま使える。
**マイク/スピーカーはブラウザが扱うので、コンテナに音声デバイスを渡す必要はない**（PulseAudio 不要）。

```bash
cp .env.example .env       # ← 編集面はこれだけ（LLM/opencode の接続先や秘密キーなど）
docker compose up --build
# ブラウザで http://localhost:8000 を開く
```

- 前提・トラブルシュートは **[`DOCKER.md`](DOCKER.md)** を参照。
- 設定はすべて **`.env`** に集約（`config.py` は `.env` を読むだけのローダなので編集不要）。
- TTS(VOICEVOX) はコンテナで自動起動。`VOICEVOX_URL` は compose が自動上書きする。

### マイクのセキュアコンテキスト（重要）
ブラウザの `getUserMedia` は **secure context が必須**。

- **同一マシンから**は `http://localhost:8000` で OK（localhost は secure context 扱い）。
- **LAN の別端末から** `http://<サーバのIP>:8000` を平文で開くと、ブラウザがマイクを許可しない。
  この場合は TLS が必要：
  - 手軽な順に **Tailscale**（`https://<machine>.ts.net`）、**Cloudflare Tunnel**、
    **Caddy など TLS リバースプロキシ**（自己署名でも可）でサーバの 8000 番を https で前段する。
- インターネット公開＋認証は本リポジトリのスコープ外（当面 LAN/localhost 運用）。

### 外部サービス（会話LLM / opencode）の準備
会話 LLM(llama.cpp) と作業エージェント(opencode) は **LAN の既存サーバを外部参照**する（compose 外）。
`.env` に接続先を書く：

```dotenv
LLAMA_BASE_URL=http://<llamaのIP>:8080/v1     # OpenAI 互換
LLAMA_API_KEY=...                              # 未設定運用なら何でも可
OPENCODE_BASE_URL=http://<opencodeのIP>:4096
OPENCODE_PROVIDER_ID=...                       # opencode.json に合わせる
OPENCODE_MODEL_ID=...
```

> opencode serve は既定で `127.0.0.1` にしか bind しないので、別マシンから届かせるには
> `opencode serve --hostname 0.0.0.0 --port 4096` で起動し、`4096/tcp` を開放しておく。

## 使い方（Web UI）

- **押している間だけ話す**ボタン（または**スペースキー**長押し）で録音。離すと送信される。
- 応答は文字（会話ログ）と音声（自動再生）で返る。
- **再生中にもう一度ボタンを押すと割り込み（バージイン）**：再生を即停止して録り直す。
- **ログモード**トグル ON 中の発話は LLM/TTS を挟まず STT 結果を Discord へ直送する
  （音声メモ用。`DISCORD_WEBHOOK_URL_LOGMODE` が必要）。
- **話者ID / 話速**は画面から切り替えられる。
- **自動音声検出 (VAD)** トグル ON で、キーを押さず声を検知して自動録音（無音で停止）。
  背面タブでも動く完全ハンズフリー運用向け。**検出しきい値**と**無音停止(秒)**は画面から調整可。
- **タブが背面でもプッシュトゥトークしたい**場合は、OS のグローバルホットキーから
  `POST /api/remote-ptt?state=start|stop` を叩く。OS 別のサンプルは
  [`scripts/`](scripts/README.md)（Windows/AutoHotkey・Linux/sxhkd・macOS/Hammerspoon）参照。

## STT 単体 API（再利用可能）

Web UI を介さず、音声ファイルを文字起こしするだけの素のエンドポイントもある：

```bash
curl -F file=@sample.webm http://localhost:8000/api/transcribe
# => {"text": "..."}
```
webm/opus・wav・mp3 など（faster-whisper の PyAV デコードが扱える形式）を受け付ける。

## 調整ポイント

- **会話をもっと速く** … `WHISPER_MODEL=large-v3-turbo`、`LLAMA_MAX_TOKENS` を下げる、応答を短く
- **作業判定がうまくいかない** … `SYSTEM_PROMPT` の `[[TASK]]` ルールの例を増やす
- **人格・口調** … `SYSTEM_PROMPT`。`.env` に1行で `SYSTEM_PROMPT=...`、
  複数行は `SYSTEM_PROMPT_FILE=/path/to/prompt.txt` で指定
- **初回の音出しを縮める** … `FIRST_FLUSH_MIN_CHARS` / `FIRST_FLUSH_MAX_CHARS`

> 上記の各値はすべて **`.env`** で設定する（`config.py` は `.env` を読むローダなので編集不要）。

## Discord 会話ログ（任意）

会話の内容（あなたの発話・AI の応答・作業委譲の指示と要約）を Discord チャンネルへ流せる。
送信は**キュー + 別スレッドの投げ捨て式**なので、会話のレイテンシには乗らず、失敗しても会話は止まらない。

ログを流したいチャンネルに **Webhook を2本**作り（1本目の名前を「あなた」、2本目を「VOICEVOXエージェント」に
すると会話として読みやすい）、URL を `.env` に書く：

```dotenv
DISCORD_WEBHOOK_URL_USER=https://discord.com/api/webhooks/...   # あなたの発話用
DISCORD_WEBHOOK_URL_AI=https://discord.com/api/webhooks/...     # AI の応答用
```

- 片方だけ設定した場合は両方そちらへ送り、`**あなた**:` / `**VOICEVOXエージェント**:` を本文に前置して区別する。
- 両方とも未設定なら機能は無効（既定）。2000 字超は自動分割。

## ログモード（STT → Discord 直送・任意）

Web UI の「ログモード」トグルを ON にすると、その発話を **LLM・TTS を一切挟まず** STT 結果の
テキストをそのまま Discord Webhook へ POST する（音声メモ・口述筆記、または STT 非対応の
エージェントへの音声入力口）。会話ログとは**別の Webhook** を 1 本作り、URL を `.env` に書く：

```dotenv
DISCORD_WEBHOOK_URL_LOGMODE=https://discord.com/api/webhooks/...   # ログモード専用
```

未設定のまま ON にすると、画面にその旨を表示して送信しない。

## （任意）Docker を使わず直接動かす

```bash
cp .env.example .env                 # 設定は .env に集約（config.py は触らない）
pip install --group runtime          # 依存は pyproject.toml に集約（pip>=25.1）
# GPU で faster-whisper を使うなら（Windows 実機直起動時）:
# pip install --group cuda           # nvidia-cublas-cu12 / nvidia-cudnn-cu12
python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
# ブラウザで http://localhost:8000
```
- Linux で CUDA を使う場合は cuBLAS/cuDNN がシステムにあること（Docker 版は土台イメージが提供）。
  無ければ自動で CPU にフォールバック（large-v3 は遅くなる）。

## 開発（型 + lint の担保）

「型とlintは無料の担保」として ruff（lint/format）と mypy（型チェック）を入れている。
設定・依存は `pyproject.toml` に集約。**コード変更後は必ず `make check` を回す**こと。

```bash
make dev-install   # 開発ツール導入 + pre-commit 有効化（pip>=25.1。重い実行依存は入らない）
make check         # ruff lint + mypy（commit 前の本命）
make format        # ruff フォーマット
```

- 型チェック対象は本体（`core/` / `server/` / `config.py`）。`train_local/` は対象外。
- commit 時は `.pre-commit-config.yaml` のフックでも同じチェックが自動で回る。

## ウェイクワードの学習（`train_local/`・現在は未使用）

旧構成では openWakeWord の「ずんだもん」ウェイクワードで起動していたが、Web 化＋
プッシュトゥトークへ移行したため、**現在の実行経路ではウェイクワードを使わない**。
学習スクリプト一式は [`train_local/`](train_local/) に残してある（将来サーバ側ウェイクワードを
再導入する場合の参考）。

## 既知の制約 / 今後

- マイクは secure context 必須（上記）。LAN の別端末から使うには TLS 前段が要る。
- opencode の応答取得は同期。作業が長いと待つ（フィラー発話でごまかしている）。
  作業中に割り込んでも opencode 側の処理自体はキャンセルされない（再生だけ止まる）。
- opencode の API は版差がありうる。動かない場合は `http://<opencode>:4096/doc`
  （OpenAPI）で `POST /session/:id/message` の body を確認して `core/opencode.py` を合わせる。
- 認証なし前提。インターネット公開時は別途リバースプロキシ等で認証・TLS を必ず入れること。
