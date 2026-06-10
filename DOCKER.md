# Docker Compose で動かす（WSL2 / Linux コンテナ）

`voice-agent` 本体と `VOICEVOX engine`（GPU版）を **docker compose でまとめて**起動する手順。
ホストは **WSL2**（docker engine を WSL 内で動かす構成が既定）。Docker Desktop でも動くが、
その場合は音声経路を §2-B（TCP）に切り替える。コンテナは Linux ベース。

```
[WSL2 ホスト]
  ├─ マイク / スピーカー ──(WSLg PulseAudio: unix /mnt/wslg/PulseServer)──┐
  ├─ NVIDIA GPU ──(WSL2 backend, gpus: all)──┐                            │
  └─ docker compose                           ▼                           ▼
       ├─ voicevox  (GPU)   ◀── http://voicevox:50021 ──┐
       └─ voice-agent (GPU, faster-whisper) ────────────┘
              └─ LLM(llama.cpp) / opencode は LAN の既存サーバを外部参照
```

> **なぜ PulseAudio が要るのか**
> Linux コンテナはホストの音声デバイス（マイク/スピーカー）に**直接アクセスできません**
> （`/dev/snd` も無い）。そこで PulseAudio 経由で繋ぎます。経路は2通り（§2 参照）：
> WSL2+WSLg なら **WSLg の unix ソケットをコンテナに渡す**（既定・推奨）、
> Docker Desktop なら **Windows 側 PulseAudio を TCP で立てる**。
> GPU は WSL2 backend 経由で `gpus: all` がそのまま使えます。

---

## 0. 前提

- **WSL2**（docker engine を WSL 内で。`docker context` が `default` を指す）。または Docker Desktop
- **NVIDIA ドライバ**（Windows 用、最新。CUDA 12.8 ランタイムに対応する版）。GPU サポートが有効なこと
  （`docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi` が通るか確認）
- 学習済みウェイクワードモデル `my_custom_model/zundamon.onnx`（リポジトリに同梱済みならそのまま）
- 会話 LLM（llama.cpp）と作業エージェント（opencode）は **LAN の既存サーバを外部参照**する
  （compose には含めない。`.env` の `LLAMA_BASE_URL` / `OPENCODE_BASE_URL` を指定）

---

## 1. .env を用意（唯一の編集面）

```powershell
copy .env.example .env
```

`.env` を編集して **LLM / opencode の接続先・秘密キー**や人格(`SYSTEM_PROMPT` / `SYSTEM_PROMPT_FILE`)等を埋める。
`config.py` は `.env` を読むだけのローダなので **編集不要**。

> **コンテナ向けの値は触らなくてよい**：`VOICEVOX_URL` と `OWW_MODEL_PATH` は
> compose 側の環境変数が自動で上書きします（それぞれ `http://voicevox:50021`、`/app/zundamon.onnx`）。
> `.env` 内のそれらの値はホスト直実行時のフォールバックです。
> なお `.env` は秘密情報を含むため Git にもイメージにも入りません（compose が runtime に注入）。

---

## 2. マイク/スピーカー共有（PulseAudio）

音声経路はホスト構成で 2 通り。**どちらか一方**でよい。

### 2-A. WSL2 + WSLg（docker engine を WSL 内で動かす）— 既定・推奨
最近の WSL2 は **WSLg が PulseAudio を unix ソケット `/mnt/wslg/PulseServer` で提供**している。
docker-compose.yml がこのソケットをコンテナへ bind mount し、`PULSE_SERVER` も同パスを指すよう
設定済みなので、**この構成では §2-B（Windows 側 PulseAudio）は不要**。追加作業なしで動く。

確認：
```bash
ls -l /mnt/wslg/PulseServer                 # ソケットが存在するか
docker compose exec voice-agent pactl info  # 起動後、コンテナから疎通するか
```
> `docker context ls` が `default (unix:///var/run/docker.sock)` を指す＝WSL ネイティブ engine。
> この場合に 2-A が使える。Docker Desktop（`desktop-linux`）を使うなら 2-B にする。

### 2-B. Docker Desktop + Windows 版 PulseAudio（TCP:4713）
Docker Desktop を使う／WSLg ソケットを渡せない場合はこちら。`.env` の `PULSE_SERVER` を
`tcp:host.docker.internal:4713` に戻し、docker-compose.yml の `/mnt/wslg/PulseServer` マウント行を外す。

1. **PulseAudio for Windows** を入手して展開（例: `pulseaudio-win32` 系のビルド）。
   `C:\pulseaudio\` に置いたとして説明します。

2. `C:\pulseaudio\etc\pulse\default.pa` の末尾に **TCP 受け付け**を追加：

   ```
   ### コンテナ(WSL2 仮想ネットワーク)から繋げるように TCP を開く
   load-module module-native-protocol-tcp auth-ip-acl=127.0.0.1;172.16.0.0/12;192.168.0.0/16 auth-anonymous=1
   ```

3. （任意）`C:\pulseaudio\etc\pulse\daemon.conf` で常駐させる：

   ```
   exit-idle-time = -1
   ```

4. 起動：

   ```powershell
   C:\pulseaudio\bin\pulseaudio.exe --use-pid-file=false -D
   # 動作確認: 入出力デバイスが見えるか
   C:\pulseaudio\bin\pactl.exe list short sources   # マイク
   C:\pulseaudio\bin\pactl.exe list short sinks      # スピーカー
   ```

5. **Windows Defender ファイアウォール**で `pulseaudio.exe`（または TCP 4713）を許可。

> マイク/スピーカーの既定デバイスは **Windows の音量ミキサー/サウンド設定**で選んだものが使われます。
> ヘッドセット運用なら `.env` の `BARGE_IN_MODE="energy"` でも誤爆しにくくなります。

---

## 3. （任意）.env のチューニング

手順1で作った `.env` の中で、`PULSE_SERVER`（既定 `unix:/mnt/wslg/PulseServer`。2-B の TCP
構成にしたなら `tcp:host.docker.internal:4713`）や
`WHISPER_DEVICE`、`VOICEVOX_SPEAKER`、`BARGE_IN_MODE` などを必要に応じて変更する。
未設定の項目は `config.py`（ローダ）の既定値が使われる。

---

## 4. ビルドして起動

```powershell
docker compose up --build
```

- 初回は VOICEVOX イメージ DL とエージェントのビルド（CUDA ベースのため数 GB）で時間がかかります。
- 起動後、ログに「準備完了。…」が出たら **マイクに向かって「ずんだもん」** と話しかける。
- 終了は `Ctrl+C`。バックグラウンド起動は `docker compose up -d --build`、停止は `docker compose down`。

コード（`voice_agent.py` / `config.py`）を変えたら `--build` で作り直し。
`.env` を変えたら **コンテナを作り直すと反映**されます（`env_file` はコンテナ生成時に読まれるため、
`docker compose up -d` で再作成。`restart` だけでは反映されない点に注意）。

---

## 5. 疎通確認・トラブルシュート

| 症状 | 確認 / 対処 |
|---|---|
| GPU が使われない（Whisper が CPU で遅い） | `docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi` が通るか。`.env` の `WHISPER_DEVICE=cuda` |
| 音が出ない / マイクを拾わない | **2-A(WSLg)**: `ls -l /mnt/wslg/PulseServer` が在るか＆compose の socket マウントが効いているか。**2-B(TCP)**: Windows で `pulseaudio.exe` 起動中か、`pactl list short sinks/sources` に出るか、ファイアウォールで 4713 許可 |
| `RuntimeError: Failed to initialize PvRecorder` / `PulseAudio: Unable to connect: Connection refused` | コンテナが PulseAudio に繋げていない。`docker compose exec voice-agent pactl info` で疎通確認。**2-A** ならソケット未マウント/パス不一致、**2-B** なら 4713 が listen されていない（`Connection refused`＝待ち受け自体が無い。`tcp:...` 構成で WSLg しか無いと必ずこれになる→2-A に切替）|
| VOICEVOX に繋がらない | `curl http://localhost:50021/version`（ホストから）。話者一覧は `/speakers` |
| ウェイクワードが反応しない | `my_custom_model/zundamon.onnx` がマウントされているか（`OWW_MODEL_PATH=/app/zundamon.onnx`）。`.env` の `OWW_THRESHOLD` を調整 |
| 話者を変えたい | `.env` の `VOICEVOX_SPEAKER`（`/speakers` で ID 確認） |

コンテナ内から音声疎通を直接試す：

```powershell
# スピーカー（ビープ）
docker compose exec voice-agent bash -c "paplay /usr/share/sounds/alsa/Front_Center.wav"
# マイク 3 秒録音 → 再生
docker compose exec voice-agent bash -c "parecord -d @DEFAULT_SOURCE@ /tmp/t.wav & sleep 3; kill %1; paplay /tmp/t.wav"
```

---

## 補足 / 既知の制約

- **LLM・opencode はコンテナ外**：既存の LAN サーバ（llama.cpp / opencode serve）を `.env` の
  `LLAMA_BASE_URL` / `OPENCODE_BASE_URL` で指す前提。compose に含めたい場合は services を追加で。
- **音声遅延**：PulseAudio を挟むため、ホスト直実行より僅かにレイテンシが乗ります（特に 2-B の TCP）。
  シビアな低遅延が必要なら、エージェント本体だけホスト(Windows)の Python で動かし、
  VOICEVOX だけ compose で立てる構成も選べます（その場合 `VOICEVOX_URL=http://localhost:50021`）。
- VOICEVOX の CPU 版に切り替えるなら、`docker-compose.yml` の image を
  `voicevox/voicevox_engine:cpu-latest` にし、その service の `gpus: all` を外します。
