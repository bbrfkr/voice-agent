# Docker Compose で動かす（WSL2 / Linux コンテナ）

`voice-agent`（FastAPI）本体と `VOICEVOX engine`（GPU版）を **docker compose でまとめて**
起動する手順。ホストは **WSL2** または Docker Desktop。コンテナは Linux ベース。

```
[WSL2 ホスト]
  ├─ NVIDIA GPU ──(WSL2 backend, gpus: all)──┐
  └─ docker compose                           ▼
       ├─ voicevox  (GPU)   ◀── http://voicevox:50021 ──┐
       └─ voice-agent (GPU, faster-whisper, :8000) ─────┘
              └─ LLM(llama.cpp) / opencode は LAN の既存サーバを外部参照

[任意の端末のブラウザ] ──(HTTP/WS :8000)──► voice-agent
       マイク録音 / 音声再生はブラウザが担う（PulseAudio 不要）
```

> **音声デバイスをコンテナに渡す必要はありません。** マイク録音は
> ブラウザの `getUserMedia`、再生は `AudioContext` が担い、音声は WebSocket で
> サーバとやり取りします。旧構成にあった PulseAudio の共有は不要になりました。
> GPU は WSL2 backend 経由で `gpus: all` がそのまま使えます。

---

## 0. 前提

- **WSL2**（docker engine を WSL 内で）または **Docker Desktop**
- **NVIDIA ドライバ**（Windows 用、最新。CUDA 12.8 ランタイム対応版）。GPU サポートが有効なこと
  （`docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi` が通るか確認）
- 会話 LLM（llama.cpp）と作業エージェント（opencode）は **LAN の既存サーバを外部参照**する
  （compose には含めない。`.env` の `LLAMA_BASE_URL` / `OPENCODE_BASE_URL` を指定）

---

## 1. .env を用意（唯一の編集面）

```bash
cp .env.example .env
```

`.env` を編集して **LLM / opencode の接続先・秘密キー**や人格(`SYSTEM_PROMPT` / `SYSTEM_PROMPT_FILE`)等を埋める。
`config.py` は `.env` を読むだけのローダなので **編集不要**。

> `VOICEVOX_URL` は compose 側の環境変数が `http://voicevox:50021` に自動上書きします
> （`.env` の値はホスト直実行時のフォールバック）。`.env` は秘密情報を含むため Git にもイメージにも
> 入りません（compose が runtime に注入）。

---

## 2. ビルドして起動

```bash
docker compose up --build
# ブラウザで http://localhost:8000 を開く
```

- 初回は VOICEVOX イメージ DL とビルド（CUDA ベースのため数 GB）で時間がかかります。
- 起動後、ログに「準備完了。…」が出たらブラウザでアクセスし、**押している間だけ話す**
  ボタン（またはスペースキー）で話しかける。
- バックグラウンド起動は `docker compose up -d --build`、停止は `docker compose down`。

コード（`core/` / `server/` / `config.py`）を変えたら `--build` で作り直し。
`.env` を変えたら **コンテナを作り直すと反映**されます（`env_file` はコンテナ生成時に読まれるため、
`docker compose up -d` で再作成。`restart` だけでは反映されない点に注意）。

### マイクのセキュアコンテキスト
ブラウザの `getUserMedia` は secure context が必須です。**同一マシン**からは
`http://localhost:8000` で動きますが、**LAN の別端末**から `http://<サーバIP>:8000` を平文で
開くとマイクが使えません。Tailscale / Cloudflare Tunnel / Caddy 等で https を前段してください
（README「マイクのセキュアコンテキスト」参照）。

---

## 3. 疎通確認・トラブルシュート

| 症状 | 確認 / 対処 |
|---|---|
| ページが開かない | `docker compose ps` で voice-agent が up か。`docker compose logs voice-agent` を確認。`8000:8000` が空いているか |
| マイクが使えない（許可ダイアログが出ない/エラー） | secure context 問題。`http://localhost:8000`（同一マシン）か、LAN なら https 前段が必要 |
| GPU が使われない（Whisper が CPU で遅い） | `docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi` が通るか。`.env` の `WHISPER_DEVICE=cuda` |
| 音声が返らない | `docker compose logs` に STT/LLM/TTS のエラーが出ていないか。`/api/transcribe` に手元の音声を投げて STT 単体を切り分け（README 参照）|
| VOICEVOX に繋がらない | `curl http://localhost:50021/version`（ホストから）。話者一覧は `/speakers` |
| LLM に繋がらない | `.env` の `LLAMA_BASE_URL` がコンテナから到達可能か（LAN の別ホストなら IP/ポート開放を確認）|
| 話者を変えたい | Web UI の話者ID 入力、または `.env` の `VOICEVOX_SPEAKER`（`/speakers` で ID 確認）|

STT 単体 API で切り分け：

```bash
curl -F file=@sample.webm http://localhost:8000/api/transcribe   # => {"text": "..."}
```

---

## 補足 / 既知の制約

- **LLM・opencode はコンテナ外**：既存の LAN サーバ（llama.cpp / opencode serve）を `.env` の
  `LLAMA_BASE_URL` / `OPENCODE_BASE_URL` で指す前提。compose に含めたい場合は services を追加で。
- **認証なし前提**：インターネット公開時はリバースプロキシ等で認証・TLS を必ず入れること。
- VOICEVOX の CPU 版に切り替えるなら、`docker-compose.yml` の image を
  `voicevox/voicevox_engine:cpu-latest` にし、その service の `gpus: all` を外します。
