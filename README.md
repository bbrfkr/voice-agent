# voice-agent —「ずんだもん」で起きる低遅延・音声エージェント

ウェイクワード「ずんだもん」→ 音声認識 → 会話LLM（llama.cpp）→ 音声合成（VOICEVOX）を
**ストリーミングでパイプライン**し、会話は低遅延（最初の音まで ≈1.5〜2.5s）で返す。
「〜して」などの作業依頼を検出したときだけ **opencode** に委譲して実作業させる。

```
[Windows] マイク / スピーカー ──(PulseAudio TCP)──┐
                                                   ▼
[WSL2 / Docker] 「ずんだもん」(openWakeWord) → 録音(VAD) → faster-whisper(STT, GPU)
            │
            ▼
      llama.cpp (LAN, OpenAI互換, stream)
            ├ 通常会話     : 句点ごとに VOICEVOX(GPUコンテナ)で逐次再生   ← 低遅延
            └ [[TASK]] 検出: opencode serve に委譲 → 結果をLLMが音声で要約
```

## 構成と配置

| 役割 | 何を | どこで |
|---|---|---|
| ウェイクワード | openWakeWord（OSS, CPU） | WSL2 / Docker |
| STT | faster-whisper large-v3（GPU） | WSL2 / Docker |
| 会話LLM | llama.cpp server / gemma 26B-A4B | LAN（既存サーバ） |
| 作業エージェント | opencode serve | LAN（既存サーバ） |
| TTS | VOICEVOX engine（GPU） | WSL2 / Docker（compose） |
| マイク / スピーカー | 物理デバイス | **Windows**（PulseAudio で WSL へ共有） |

## 実行環境マップ — どこで動くか（重要）

**サンプル生成・学習・エージェント実行は、すべて WSL2（＋Docker）で完結する。**
**Windows 側で手作業が要るのは PulseAudio の導入だけ**（マイク/スピーカーを WSL へ渡すため）。
あとは前提インフラ（NVIDIA ドライバ・Docker Desktop）があればよい。

| 作業 | スクリプト | 実行場所 | マイク |
|---|---|---|---|
| 正例サンプル生成 | `gen_samples.py` | WSL（VOICEVOX に接続） | 不要 |
| 負例サンプル生成 | `gen_negatives.py` | WSL（VOICEVOX に接続） | 不要 |
| モデル学習 | `train.py` / `train_local/` | WSL2 + CUDA | 不要 |
| 自声の録音（任意・精度↑） | `record_wakeword.py` | WSL（PulseAudio 経由） | 要（PulseAudio で） |
| エージェント実行 | `voice_agent.py` | WSL / Docker | 要（PulseAudio で） |

> マイクを使う作業（自声録音・エージェント実行）も、**同じ PulseAudio 経由で WSL から**動く。
> つまり Windows 上で別途やることは PulseAudio の起動以外に無い。

## クイックスタート（Docker Compose・推奨）

`voice-agent` 本体と `VOICEVOX engine`（GPU版コンテナ）を **docker compose でまとめて**起動する。
GPU は Docker Desktop の WSL2 backend 経由で `gpus: all` がそのまま使える。マイク/スピーカーだけは
コンテナから直接触れないため、Windows 側に **PulseAudio を TCP で立てて共有**する。

```powershell
copy .env.example .env    # ← 編集面はこれだけ（LLM/opencode の接続先や秘密キーなど）
# Windows 側で PulseAudio(TCP 4713) を起動（手順は DOCKER.md）
docker compose up --build
```

- 前提・**Windows の PulseAudio 設定**・トラブルシュートは **[`DOCKER.md`](DOCKER.md)** に集約。
- 設定はすべて **`.env`** に集約（`config.py` は `.env` を読むだけのローダなので編集不要）。
- TTS(VOICEVOX) はコンテナで自動起動。`VOICEVOX_URL` / `OWW_MODEL_PATH` は compose が自動上書き。
- ウェイクワードモデル `my_custom_model/zundamon.onnx` が必要（無ければ下の「ウェイクワードの学習」で作る。
  compose がこのパスをコンテナにマウントする）。

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

## 調整ポイント

- **反応しすぎ/しなさすぎ** … `OWW_THRESHOLD`（高いほど誤検出減）
- **発話が途中で切れる/長く待つ** … `SILENCE_RMS`（環境ノイズに合わせる）と `SILENCE_HANG_SEC`
- **会話をもっと速く** … `WHISPER_MODEL="large-v3-turbo"`、`LLAMA_MAX_TOKENS` を下げる、応答を短く
- **作業判定がうまくいかない** … `SYSTEM_PROMPT` の `[[TASK]]` ルールの例を増やす
- **人格・口調** … `SYSTEM_PROMPT`。`.env` に1行で `SYSTEM_PROMPT=...`、
  複数行は `SYSTEM_PROMPT_FILE=/path/to/prompt.txt` で指定
- **バージイン（割り込み）** … `BARGE_IN_MODE` で切替（下記）

> 上記の各値はすべて **`.env`** で設定する（`config.py` は `.env` を読むローダなので編集不要）。

## バージイン（応答再生中の割り込み）

応答を喋っている最中にユーザーが割り込むと、再生を即停止して新しい発話を受け付ける。
`.env` の `BARGE_IN_MODE` で動作を選ぶ：

| モード | 割り込み方法 | 向き |
|---|---|---|
| `wakeword`（既定） | 再生中にもう一度**「ずんだもん」** | エコーに強く**スピーカー運用でも安全**。TTSの声では誤爆しない |
| `energy` | **そのまま喋り出す**（声量で検知） | 自然だが、スピーカー音をマイクが拾うと誤爆 → **ヘッドセット推奨**。`BARGE_IN_RMS` で閾値調整 |
| `off` | 無効 | 1ターンずつ順番 |

仕組み: ターン中だけ専用スレッド `BargeInMonitor` がマイクを監視し、検出すると再生キューを
即フラッシュ。`energy` モードでは割り込み時に拾った声の先頭をそのまま次の発話の頭に引き継ぐ
（言い直し不要）。`wakeword` モードは「ずんだもん」検出後に録り直し。

## ウェイクワードの学習（「ずんだもん」モデルを作る）— すべて WSL で完結

openWakeWord はアカウント不要だが、任意フレーズは **1回だけ学習**が要る。
ここでは手元の **VOICEVOX で正例（ポジティブ）サンプルを量産**して使う（追加ツール不要）。
**サンプル生成も学習も WSL 上で完結する**（マイクは使わない）。

### 1. 学習用サンプルの生成（VOICEVOX）
VOICEVOX を起動しておく（`docker compose up -d voicevox` でも、ホストの `127.0.0.1:50021` でも可。
`.env` の `VOICEVOX_URL` が指す先に繋ぐ）。

```bash
python gen_samples.py        # wake_samples/ に「ずんだもん」を全話者×韻律で大量合成（正例）
python gen_negatives.py      # 非ウェイクワード音声（負例）。train.py は負例が必須
```
- 表記ゆれを足したいときは `gen_samples.py` の `VARIANTS` を編集。

### 2. （任意・推奨）自分の声を足す
VOICEVOX 合成だけだと生声で反応しづらいことがある。**自分の声**の正例＋自分の声/環境音の負例を
足すと実環境で安定する。マイクを使うので **PulseAudio 経由で WSL から**録る（[`DOCKER.md`](DOCKER.md) 参照）：

```bash
python record_wakeword.py --label positive --count 60   # ビープ後に「ずんだもん」を1回ずつ
python record_wakeword.py --label negative --count 40   # 紛らわしい語/雑談
python record_wakeword.py --label negative --ambient --seconds 60  # 環境音
```
> 生声は合成データに**追加**する（置き換えない）。詳細は [`train_local/README.md`](train_local/README.md)。

### 3. 学習（Linux / WSL2 + CUDA）
詳細手順・依存の注意（Python 3.11〜3.12、`scipy<1.15` 等）は
[`train_local/README.md`](train_local/README.md) を参照：

```bash
python train_local/split_samples.py   # 正例を train/test に振り分け
# openWakeWord を学習（--generate_clips は付けない＝Piper不要）
python train.py --training_config train_local/config.yaml \
    --augment_clips --overwrite --train_model
```
- 大容量データセット（ネガティブ特徴・RIR・背景ノイズ, 数十GB）の DL が前提（`train_local/README.md`）。
- 完了で `my_custom_model/zundamon/zundamon.onnx` が出る → **`my_custom_model/zundamon.onnx` に置く**
  （compose がこのパスをコンテナにマウント。`.env` の `OWW_MODEL_PATH` は Docker では自動上書き）。

> **コツ**:「ずんだもん」は5モーラと長めで、ウェイクワードとしては誤検出しにくい良い語。
> それでも誤発火が多いときは `OWW_THRESHOLD` を上げる、反応が鈍いなら下げる。
> 合成声(VOICEVOX)だけだと自分の生声で反応しづらいことがあるので、手順2の自声追加が効く。

## （任意）Docker を使わず WSL で直接動かす

エージェントを Docker 無しで WSL の Python から直接動かすこともできる（学習と同じ venv で可）。
この場合もマイク/スピーカーは PulseAudio 経由（`PULSE_SERVER` を Windows ホストに向ける）。

```bash
cp .env.example .env          # 設定は .env に集約（config.py は触らない）
pip install -r requirements.txt
export PULSE_SERVER=tcp:<WindowsホストのIP>:4713
python voice_agent.py
```
- GPU で faster-whisper を使うには `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`（requirements.txt 同梱）が要る。
  入っていないと自動で CPU にフォールバック（large-v3 は遅くなる）。

> **（参考）Windows ネイティブ実行**：Python を Windows に直接入れて動かすことも一応可能だが、
> faster-whisper(CTranslate2) が `cublas64_12.dll` を見つけられず落ちる既知の罠がある
> （`voice_agent.py` の `_register_cuda_dll_dirs()` が pip の `nvidia-*` wheel の bin を登録して回避）。
> 音声デバイスも素直に使えるので一見楽だが、本リポジトリは **WSL/Docker を前提**に整備している。

## 既知の制約 / 今後

- opencode の応答取得は同期。作業が長いと待つ（フィラー発話でごまかしている）。
  作業の実行中に割り込んだ場合、再生は止まるが opencode 側の処理自体はキャンセルされない。
  ストリーミング要約にしたい場合は `/event` SSE を使う拡張余地あり。
- opencode の API は版差がありうる。動かない場合は `http://<opencode>:4096/doc`
  （OpenAPI）で `POST /session/:id/message` の body を確認して `OpenCode.run()` を合わせる。
- ウェイクワード「ずんだもん」は TTS の話者名と同じため、応答中に AI が自分で「ずんだもん」と
  発話するとバージイン（`wakeword` モード）が自己発火しうる。気になる場合は `SYSTEM_PROMPT` に
  「自分を“ずんだもん”と名乗らない」と書くか、ウェイクワードを別語に変える。
```
