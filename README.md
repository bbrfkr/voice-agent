# voice-agent — Discord ボイスチャンネルで話す低遅延・音声エージェント

bot が Discord のボイスチャンネルに常駐し、**ウェイクワード無しで**チャンネル内の発話を聞いて応答する。
音声認識(STT) → 会話LLM → 音声合成(TTS) を**ストリーミングでパイプライン**し、低遅延で返す。
「〜して」などの作業依頼を検出したときだけ **opencode** に委譲して実作業させる。

STT(faster-whisper) と TTS(VOICEVOX) は **GPU サーバ側の HTTP サービス**に分離してあり、
bot 本体は GPU 不要の軽量プロセス。マイク/スピーカーは持たず、音声入出力はすべて Discord で行う。

```
[Discord ボイスチャンネル] ← ユーザーが入室して会話
        │ Opus/RTP（喋っている間だけパケットが届く＝VAD相当が無料で付く）
        ▼
[voice-agent bot（軽量・GPU不要）]
   受信(voice-recv): 話者ごとに PCM をバッファ → 発話停止で 1 発話を確定
        ├ STT : POST /v1/audio/transcriptions（リモート GPU）
        ├ LLM : llama.cpp 等（OpenAI互換, stream）
        │        ├ 通常会話     : 句点ごとに VOICEVOX で逐次再生 → チャンネルへ送出
        │        └ [[TASK]] 検出: opencode serve に委譲 → 結果をLLMが音声で要約
        └ 送信 : 合成 PCM(48kHz/stereo) をボイスチャンネルへ再生
        ▼
[STT サーバ(GPU)]   [VOICEVOX(GPU)]
```

## 構成と配置

| 役割 | 何を | どこで |
|---|---|---|
| 音声 I/O | Discord ボイスチャンネル（discord.py + discord-ext-voice-recv） | bot |
| STT | faster-whisper（OpenAI 互換サーバ, GPU） | GPU サーバ |
| 会話LLM | llama.cpp server 等（OpenAI互換） | LAN（既存サーバ） |
| 作業エージェント | opencode serve | LAN（既存サーバ） |
| TTS | VOICEVOX engine（GPU） | GPU サーバ |
| bot 本体 | `discord_agent.py`（軽量・GPU不要） | どこでも |

## クイックスタート（Docker Compose・推奨）

`voice-agent` bot、STT（OpenAI 互換）、VOICEVOX を **docker compose でまとめて**起動する。
STT/TTS は GPU が要る（`gpus: all`）。bot 自体は GPU 不要なので、必要なら `stt` / `voicevox` だけを
GPU サーバに置き、bot を別ホストで動かして `.env` の `STT_BASE_URL` / `VOICEVOX_URL` を向けてもよい。

```bash
cp .env.example .env    # ← 編集面はこれだけ
# .env に DISCORD_BOT_TOKEN / DISCORD_VOICE_CHANNEL_ID と LLM/opencode の接続先を設定
docker compose up --build
```

- 設定はすべて **`.env`** に集約（`config.py` は `.env` を読むだけのローダなので編集不要）。
- compose 内では `STT_BASE_URL=http://stt:8000/v1` / `VOICEVOX_URL=http://voicevox:50021` を自動上書き。

### Discord bot の準備
1. [Discord Developer Portal](https://discord.com/developers/applications) で **アプリ + bot** を作成し、
   **bot トークン**を取得（`.env` の `DISCORD_BOT_TOKEN` に。★秘密）。
2. bot 設定の **Bot → Privileged Gateway Intents** で **SERVER MEMBERS INTENT** を ON にする。
   （発話者を解決するために必須。OFF だと voice チャンネルで一切応答しない。）
3. bot を対象サーバに招待する（OAuth2 → URL Generator → scope `bot`、権限は **Connect** と **Speak**）。
4. 入室させたいボイスチャンネルを右クリック →「**IDをコピー**」して `DISCORD_VOICE_CHANNEL_ID` に。
   （開発者モードが必要: ユーザー設定 → 詳細設定 → 開発者モード）

```dotenv
DISCORD_BOT_TOKEN=...                 # ★秘密
DISCORD_VOICE_CHANNEL_ID=1234567890   # 入室するボイスチャンネル ID
```

起動すると bot が指定チャンネルに自動入室する。あなたも同じチャンネルに入って話せば応答する。

### 外部サービス（会話LLM / opencode）の準備
会話 LLM(llama.cpp 等) と作業エージェント(opencode) は **LAN の既存サーバを外部参照**する（compose 外）。
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

### STT サーバについて
`docker-compose.yml` の `stt` サービスは OpenAI 互換の faster-whisper サーバ（例: `speaches`）を使う。
`POST {STT_BASE_URL}/audio/transcriptions` に音声を投げてテキストを得る。読み込むモデルは
`.env` の `STT_MODEL`（既定 `mobiuslabsgmbh/faster-whisper-large-v3-turbo` ＝従来ローカル版の
`large-v3-turbo` と同一モデル）。別の OpenAI 互換 STT を使ってもよい。

## 調整ポイント

- **発話が短い物音に反応する／拾い損ねる** … `MIN_UTTERANCE_SEC`（これより短い発話は無視）
- **会話をもっと速く** … `STT_MODEL` を軽量に、`LLAMA_MAX_TOKENS` を下げる、応答を短く
- **作業判定がうまくいかない** … `SYSTEM_PROMPT` の `[[TASK]]` ルールの例を増やす
- **人格・口調** … `SYSTEM_PROMPT`。`.env` に1行で `SYSTEM_PROMPT=...`、
  複数行は `SYSTEM_PROMPT_FILE=/path/to/prompt.txt` で指定
- **バージイン（割り込み）** … `BARGE_IN_ENABLED` / `BARGE_IN_MIN_SEC`（下記）

> 上記の各値はすべて **`.env`** で設定する（`config.py` は `.env` を読むローダなので編集不要）。

## バージイン（応答再生中の割り込み）

bot が応答を喋っている最中にユーザーが話し始めると、再生を即停止して未再生の合成キューを捨て、
新しい発話を受け付ける。Discord はパケット到来＝発話開始なので RMS 閾値は不要。短い物音での
誤割り込みを防ぐため、`BARGE_IN_MIN_SEC` 秒以上話し続けたときだけ割り込みを確定する。

| 設定 | 既定 | 意味 |
|---|---|---|
| `BARGE_IN_ENABLED` | `true` | `false` で割り込み無効（1ターンずつ順番） |
| `BARGE_IN_MIN_SEC` | `0.2` | この秒数以上喋り続けたら割り込み確定 |

## Discord 会話ログ（任意）

会話の内容（あなたの発話・AI の応答・作業委譲の指示と要約）を Discord の**テキストチャンネル**へ流せる。
送信は**キュー + 別スレッドの投げ捨て式**なので会話のレイテンシには乗らず、失敗しても会話は止まらない。

ログを流したいテキストチャンネルに **Webhook を2本**作り（名前を「あなた」「ずんだもん」にすると
チャット画面で会話として読みやすい）、URL を `.env` に書く：

```dotenv
DISCORD_WEBHOOK_URL_USER=https://discord.com/api/webhooks/...   # あなたの発話用
DISCORD_WEBHOOK_URL_AI=https://discord.com/api/webhooks/...     # AI の応答用
```

- 片方だけ設定した場合は両方そちらへ送り、`**あなた**:` / `**ずんだもん**:` を本文に前置して区別する。
- 両方とも未設定なら機能は無効（既定）。2000 字超は自動分割。
- これは**会話ログ用のテキストチャンネル Webhook** であり、bot が入室する**ボイスチャンネルとは別**。

## ログモード（STT → Discord 直送・任意）

音声入力した内容を **LLM・TTS を一切挟まず**、STT 結果のテキストをそのまま Discord Webhook へ
POST するだけのモード。音声メモ・口述筆記、あるいは Discord テキスト連携の入力口に使う。

会話ログとは**別の Webhook** を 1 本作り（別チャンネル推奨）、URL を `.env` に書く：

```dotenv
DISCORD_WEBHOOK_URL_LOGMODE=https://discord.com/api/webhooks/...   # ログモード専用
```

| 発話 | 動作 |
|---|---|
| 「ログモード」 | ON。「ログモードがオンになりました」と読み上げ |
| （ON 中の任意の発話） | STT 結果をそのまま専用 Webhook へ直送（応答なし） |
| 「ログモード終了」 | OFF。「ログモードがオフになりました」と読み上げ |

- 切替は **発話全体が（ほぼ）一致したときだけ**反応する（会話文中に「ログ」が出ただけでは切り替えない）。
  空白・句読点・かなの表記ゆれ（「ログ モード」「ろぐもーど」等）は正規化で自動吸収。
  同義語は `.env` の `LOG_MODE_ON_COMMANDS` / `LOG_MODE_OFF_COMMANDS` で変更できる。
- ON 中の発話は通常の会話ログ（`DISCORD_WEBHOOK_URL_USER`）には送られない。
- `DISCORD_WEBHOOK_URL_LOGMODE` 未設定のまま ON にしようとすると音声で案内して断る。

## （任意）Docker を使わず直接動かす

bot は GPU 不要なので、Python から直接動かすこともできる（STT/VOICEVOX のリモート URL は要設定）。

```bash
cp .env.example .env                 # 設定は .env に集約（config.py は触らない）
pip install --group runtime          # 依存は pyproject.toml に集約（pip>=25.1）
python discord_agent.py
```
- voice 送受信に **libopus** が要る（Debian/Ubuntu なら `apt install libopus0`）。
- VOICEVOX が返す WAV のデコードに **libsndfile**（`apt install libsndfile1`）。

## 開発（型 + lint の担保）

「型とlintは無料の担保」として ruff（lint/format）と mypy（型チェック）を入れている。
設定・依存は `pyproject.toml` に集約。**コード変更後は必ず `make check` を回す**こと。

```bash
make dev-install   # 開発ツール導入 + pre-commit 有効化（pip>=25.1。重い実行依存は入らない）
make check         # ruff lint + mypy（commit 前の本命）
make format        # ruff フォーマット
```

- 型チェック対象は本体（`voice_agent.py` / `config.py` / `discord_agent.py`）。`train_local/` は対象外。
- commit 時は `.pre-commit-config.yaml` のフックでも同じチェックが自動で回る。

## 既知の制約 / 今後

- opencode の応答取得は同期。作業が長いと待つ（フィラー発話でごまかしている）。
  作業の実行中に割り込んだ場合、再生は止まるが opencode 側の処理自体はキャンセルされない。
- opencode の API は版差がありうる。動かない場合は `http://<opencode>:4096/doc`（OpenAPI）で
  `POST /session/:id/message` の body を確認して `OpenCode.run()` を合わせる。
- 発話の区切りは voice-recv の発話停止イベント（パケット途切れ）に依存する。ネットワークの
  ジッタや小さな間で 1 発話が分割されることがある。`MIN_UTTERANCE_SEC` で極端に短い断片は捨てる。
- `train_local/`（ウェイクワード学習一式）は **旧ローカル版の遺産**で、Discord 版では使わない。
  当面は参照用に残してあるが、本 bot の動作には不要。
```
