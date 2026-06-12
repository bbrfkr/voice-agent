# Discord ボイスチャンネル常駐版で動かす（ROCm / amdgpu）

ローカルのマイク/スピーカー（`voice_agent.py` + `docker-compose.yml`）の代わりに、
**ずんだもん AI を Discord のボイスチャンネル（VC）に bot として常駐**させる構成。
スマホ等、Discord app が動く端末ならどこからでも音声で呼び出せる。

既存のローカル版 Docker 環境とは**完全に独立**しており（compose プロジェクト名も別）、
従来どおり `docker compose up` でローカル版も使い続けられる。

```
[スマホ / PC の Discord app]
        │ (Opus 48kHz, ユーザーごとに別ストリーム)
        ▼
[ROCm ホスト（例: Ryzen AI Max+ 395 / Radeon 8060S, ネイティブ Linux）]
  └─ docker compose -f docker-compose.discord.yml
       ├─ discord-voice-agent : 受信 → openWakeWord → faster-whisper(Radeon/ROCm)
       │                        → llama.cpp(LAN) → VOICEVOX → VC へ再生
       └─ voicevox (CPU 版)   ◀── http://voicevox:50021
              └─ LLM(llama.cpp) / opencode は LAN の既存サーバを外部参照
```

役割分担: **STT = Radeon（in-process, ROCm）／ TTS = VOICEVOX CPU ／ LLM = 既存 LAN サーバ**。

ローカル版との違い:

- **エコー問題が消える**: bot の再生音声は受信ストリームに混ざらないため、TTS の声での
  誤検出がない。`BARGE_IN_MODE=energy`（自由発話での割り込み）が安全に使える
- **話者を ID で制御できる**: ユーザーごとにストリームが分かれており、
  `DISCORD_ALLOWED_USER_IDS` で「誰の声に反応するか」を確実に絞れる
- **ウェイクワードの位置づけを選べる**: `WAKE_MODE=wakeword`（既定、「ずんだもん」呼びかけ式）
  / `WAKE_MODE=always`（常時リッスン。1人で使うチャンネル向け）
- PulseAudio / WSLg の音声配管が丸ごと不要

機能はローカル版をフル移植: 会話（ストリーミング TTS・1文目早出し）・`[[TASK]]` の
opencode 委譲・Discord 会話ログ・ログモード・バージイン。

---

## 0. 前提

- **ネイティブ Linux + amdgpu ドライバ + ROCm 7.2.x 系**（WSL2 の ROCm は非推奨）。
  Strix Halo（gfx1151）でのドライバ/ROCm 導入は AMD 公式手順どおり:
  HWE カーネル → `amdgpu-install` リポジトリ登録 → `amdgpu-dkms` → `rocm`。
  `rocminfo | grep gfx` で GPU が見えること
- docker / docker compose v2.20 以降（compose ファイル先頭の `name:` を解するもの）
- 会話 LLM（llama.cpp）と作業エージェント（opencode）は **LAN の既存サーバを外部参照**
  （`.env` の `LLAMA_BASE_URL` / `OPENCODE_BASE_URL`）
- 学習済みウェイクワードモデル `my_custom_model/zundamon.onnx`（リポジトリ同梱）

> STT は CTranslate2 公式の **ROCm wheel**（gfx1151 カーネル焼き込み済み）を使うため、
> ソースビルドや `HSA_OVERRIDE_GFX_VERSION` は不要。コードも `WHISPER_DEVICE=cuda` の
> ままで動く（HIP が CUDA API を擬態する）。wheel はイメージビルド時に自動取得される。

## 1. Discord bot を作る

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** タブ → **Reset Token** でトークンを発行（→ `.env` の `DISCORD_BOT_TOKEN`）。
   Privileged Gateway Intents（PRESENCE / SERVER MEMBERS / MESSAGE CONTENT）は**すべて不要**
3. **OAuth2 → URL Generator**: scope `bot`、Bot Permissions は **Connect** と **Speak** だけ
   チェックし、生成された URL で自分のサーバーへ招待
4. Discord クライアントの設定 → 詳細設定 → **開発者モード** を ON にし、常駐させたい
   ボイスチャンネルを右クリック → **ID をコピー**（→ `.env` の `DISCORD_VOICE_CHANNEL_ID`）

## 2. .env を用意（唯一の編集面）

```bash
cp .env.example .env   # 既にローカル版で使っている .env があればそのまま追記
```

Discord セクションを埋める:

```bash
DISCORD_BOT_TOKEN=（手順1のトークン。★秘密）
DISCORD_VOICE_CHANNEL_ID=（手順1のチャンネル ID）
# DISCORD_ALLOWED_USER_IDS=（反応するユーザーを絞るなら。空で bot 以外の全員）
# WAKE_MODE=wakeword            # always にすると呼びかけ不要の常時リッスン
RENDER_GID=（getent group render | cut -d: -f3 の値）
BARGE_IN_MODE=energy            # エコーが無いので自由発話の割り込みを推奨
```

`LLAMA_BASE_URL` / `OPENCODE_BASE_URL` / Webhook 類はローカル版と共通。
`VOICEVOX_URL` と `OWW_MODEL_PATH` は compose が自動で上書きするので触らなくてよい。

> **RENDER_GID が要る理由**: コンテナから `/dev/dri`（GPU）を読むには render グループに
> 入る必要があるが、render の GID はホストごとに違う（992 や 110 など）。compose の
> `group_add` に `.env` 経由で渡す。

## 3. ビルドして起動

```bash
docker compose -f docker-compose.discord.yml up --build -d
docker compose -f docker-compose.discord.yml logs -f discord-voice-agent
```

- 初回は ROCm ライブラリ（数 GB）と Whisper モデルの DL で時間がかかる
- ログに `[discord] ボイスチャンネル「…」に接続しました` →（モデル読み込み）→
  `準備完了。` が出たら、**スマホ等で同じ VC に入り「ずんだもん」と話しかける**
- 停止: `docker compose -f docker-compose.discord.yml down`。
  コード変更後は `--build` を付けて上げ直す。`.env` 変更はコンテナ再作成（`up -d`）で反映

## 4. 疎通確認・トラブルシュート

| 症状 | 確認 / 対処 |
|---|---|
| GPU が使われない（STT が CPU フォールバック） | コンテナ内で GPU が見えるか: `docker compose -f docker-compose.discord.yml exec discord-voice-agent python3 -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"` が 1 以上か。0 なら `/dev/kfd`・`/dev/dri` のマウントと `RENDER_GID` を確認。ホスト側は `rocminfo \| grep gfx` |
| bot がオンラインにならない | `DISCORD_BOT_TOKEN` が正しいか（Reset Token し直すと古いトークンは失効） |
| VC に入ってこない | `DISCORD_VOICE_CHANNEL_ID` が**ボイス**チャンネルの ID か。bot がそのチャンネルの閲覧/接続権限を持つか。ログの `[discord] VC 接続に失敗` を確認（10 秒ごとに自動再試行） |
| 声に反応しない | `DISCORD_ALLOWED_USER_IDS` に自分が入っているか。サーバーミュートされていないか。`OWW_THRESHOLD` / `[wake] score=` ログで感度確認。まれにユーザー特定前の音声が捨てられて頭が欠けるが、発話頭の前置きバッファで通常は吸収される |
| 応答の声が出ない | VOICEVOX の疎通: `curl http://localhost:50021/version`。bot に Speak 権限があるか |
| `OpusError: corrupted stream` が連発して声に反応しない | コンテナに `davey`（DAVE/E2EE）が入っていないか確認: `docker compose -f docker-compose.discord.yml exec discord-voice-agent pip list \| grep davey`。入っていると Discord がボイスを E2EE のままにし、voice-recv が復号できず全パケットがこのエラーになる。イメージは `pip uninstall davey` 済みの構成でビルドし直す（`Dockerfile.discord` のコメント参照） |
| ウェイクワード不要にしたい | `.env` で `WAKE_MODE=always`（発話即ターン。ログモードの切替は発話コマンドのまま使える） |
| STT が遅い | `[stt]` ログで所要を確認（典型発話で 0.3〜1 秒が目安）。精度を上げたいなら `WHISPER_MODEL=large-v3`（フル版でも実時間の約 11 倍速） |

## 補足 / 既知の制約

- **複数人の同時発話は混ざる**（受信は 1 本のキューに合流させている）。複数人で同時に
  話す用途ではなく、個人利用前提。`DISCORD_ALLOWED_USER_IDS` で 1 人に絞ると確実
- 遅延は Discord 経由で片道 100〜200ms 上乗せされるが、応答全体（1.5〜2.5s）からは誤差の範囲
- ROCm wheel は Python 3.12（cp312）向けのため、ベースイメージの Python を変えないこと
- ROCm のバージョンを変える場合は `Dockerfile.discord` の `ROCM_VERSION` / `CT2_VERSION`
  （build arg）を調整する。CTranslate2 v4.7.2 以降は ROCm 7.2.1 ビルド
- bot を GPU なしホストに置きたい場合は、STT を speaches（OpenAI 互換 API）等で
  外出しする構成も取れる（本構成では不要なので未実装）
