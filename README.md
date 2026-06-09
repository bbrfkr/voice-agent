# voice-agent —「ずんだもん」で起きる低遅延・音声エージェント

ウェイクワード「ずんだもん」→ 音声認識 → 会話LLM（llama.cpp）→ 音声合成（VOICEVOX）を
**ストリーミングでパイプライン**し、会話は低遅延（最初の音まで ≈1.5〜2.5s）で返す。
「〜して」などの作業依頼を検出したときだけ **opencode** に委譲して実作業させる。

```
[Windows] 「ずんだもん」(openWakeWord) → 録音(VAD) → faster-whisper(STT, 4070Ti)
            │
            ▼
      llama.cpp (LAN, 5080×2, OpenAI互換, stream)
            ├ 通常会話     : 句点ごとに VOICEVOX(4070Ti)で逐次再生   ← 低遅延
            └ [[TASK]] 検出: opencode serve に委譲 → 結果をLLMが音声要約
```

## 構成と配置

| 役割 | 何を | どこで |
|---|---|---|
| ウェイクワード | openWakeWord（OSS, CPU） | Windows |
| STT | faster-whisper large-v3（GPU） | Windows(4070Ti) |
| 会話LLM | llama.cpp server / gemma 26B-A4B | LAN(5080×2) |
| 作業エージェント | opencode serve | LAN（llama と同居が手軽） |
| TTS | VOICEVOX engine（GPU） | Windows(4070Ti) |

## Docker Compose で動かす（おすすめ・構築が楽）

`voice-agent` 本体と `VOICEVOX engine`（GPU版コンテナ）を **docker compose でまとめて**起動できる。
ホストは **Windows + Docker Desktop**（コンテナは Linux ベース）を想定。GPU は WSL2 backend 経由で
`gpus: all` がそのまま使える。マイク/スピーカーだけはコンテナから直接触れないため、Windows 側に
**PulseAudio を TCP で立てて共有**する（手順は [`DOCKER.md`](DOCKER.md)）。

```powershell
copy .env.example .env    # ← 編集面はこれだけ（LLM/opencode の接続先や秘密キーなど）
# Windows 側で PulseAudio(TCP 4713) を起動（DOCKER.md 参照）
docker compose up --build
```

- 設定はすべて **`.env`** に集約（`config.py` は `.env` を読むだけのローダなので編集不要）。
- TTS(VOICEVOX) はコンテナで自動起動。`VOICEVOX_URL` / `OWW_MODEL_PATH` は compose が自動上書きするので
  `.env` のコンテナ向け調整は不要。
- 会話 LLM(llama.cpp) と作業エージェント(opencode) は **LAN の既存サーバを外部参照**する
  （compose には含めない）。
- 詳しい前提・Windows の PulseAudio 設定・トラブルシュートは **[`DOCKER.md`](DOCKER.md)** を参照。

下記の「セットアップ（Windows）」は、Docker を使わずホストに直接入れる従来手順。

## セットアップ（Windows）

### 0. 前提
- Python 3.10+ / 最新の NVIDIA ドライバ（**CUDA 13.1 でも可。下記参照**）

### 1. 依存インストール
```powershell
copy .env.example .env    # ← 設定はここに集約して編集（config.py は .env を読むだけ）
pip install -r requirements.txt
```

> **CUDA 13.1 が入っている場合**：問題ありません。faster-whisper の中核 CTranslate2 は
> `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`（CUDA 12 のユーザ空間ライブラリ）を使いますが、
> これらは**新しいドライバ上でも後方互換で動作**します。システムの CUDA 13.1 Toolkit とは
> 別物なので共存して問題なし。pip 同梱版（requirements.txt の2行）を入れておけば OK です。
>
> **`RuntimeError: Library cublas64_12.dll is not found` が出たら**：
> pip の `nvidia-*` wheel は DLL を `site-packages\nvidia\<pkg>\bin` に置きますが、
> **そこは Windows の DLL 検索パスに載らない**ため、CTranslate2 が encode 時に見つけられず落ちます
> （システムの CUDA は 13 系なので `cublas64_13.dll` しか無く、`_12` は別途必要）。
> `voice_agent.py` は起動時に `_register_cuda_dll_dirs()` でこの bin を
> `os.add_dll_directory()` 登録するので、**`pip install -r requirements.txt` 済みなら自動で解決**します。
> それでも出る場合は wheel 未導入なので `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` を実行。
> （登録に失敗しても CPU へ自動フォールバックして動作は継続します＝large-v3 は遅くなります。）

### 2. ウェイクワード「ずんだもん」（openWakeWord, アカウント不要）
「ずんだもん」用の検出モデルを **1回だけ自前で学習**して `.onnx` を用意する（後述「ウェイクワードの学習」）。
- 学習済み `zundamon.onnx` を `C:\voice-agent\` に置く
- `.env` の `OWW_MODEL_PATH` / `OWW_FRAMEWORK` / `OWW_THRESHOLD` を設定
- 初回起動時に openWakeWord が共有の特徴抽出モデルを自動DL（手動なら
  `python -c "import openwakeword; openwakeword.utils.download_models()"`）

### 3. VOICEVOX（TTS）
- VOICEVOX（GPU版）を起動するとローカルに `http://127.0.0.1:50021` でエンジンが立つ
- 話者IDは `http://127.0.0.1:50021/speakers` で確認 → `.env` の `VOICEVOX_SPEAKER`

### 4. llama.cpp（会話LLM）
- 既存の 5080×2 機で稼働中の llama.cpp server を OpenAI 互換で叩く
- `.env` の `LLAMA_BASE_URL` を `http://<その機のIP>:8080/v1` に設定
- `--api-key` を設定していなければ `LLAMA_API_KEY` は何でも可

### 5. opencode（作業エージェント, **LAN の Linux 機**で起動）
llama.cpp と同じ 5080×2 機に同居させるのが手軽（opencode→llama が localhost になる）。

```bash
# provider を llama.cpp に向ける設定を置く（opencode/opencode.json をこの機にコピーして編集）
#   - llama と同居なら baseURL は http://127.0.0.1:8080/v1 でOK
#   - 別マシンなら llama のLAN IPを指定
# 作業ディレクトリ直下 or ~/.config/opencode/ に opencode.json を置く

# 重要: Windows から届かせるため 0.0.0.0 で bind する
opencode serve --hostname 0.0.0.0 --port 4096
```
- この機の **ファイアウォールで 4096/tcp を開放**しておく
- Windows 側 `.env` の `OPENCODE_BASE_URL` をこの機の **LAN IP**（例 `http://192.168.1.50:4096`）に、
  `OPENCODE_PROVIDER_ID` / `OPENCODE_MODEL_ID` を opencode.json に合わせる

### 6. 起動
```powershell
python voice_agent.py
```
「ずんだもん」→ ビープ → 話す → 応答が返れば成功。

## 調整ポイント

- **反応しすぎ/しなさすぎ** … `OWW_THRESHOLD`（高いほど誤検出減）
- **発話が途中で切れる/長く待つ** … `SILENCE_RMS`（環境ノイズに合わせる）と `SILENCE_HANG_SEC`
- **会話をもっと速く** … `WHISPER_MODEL="large-v3-turbo"`、`LLAMA_MAX_TOKENS` を下げる、応答を短く（SYSTEM_PROMPT）
- **作業判定がうまくいかない** … `SYSTEM_PROMPT` の `[[TASK]]` ルールの例を増やす
- **人格・口調** … `SYSTEM_PROMPT`（＝固定コンテキスト）。`.env` に1行で `SYSTEM_PROMPT=...`、
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

## ウェイクワードの学習（「ずんだもん」モデルを作る）

openWakeWord はアカウント不要だが、任意フレーズは **1回だけ学習**が要る。
ここでは手元の **VOICEVOX で正例（ポジティブ）サンプルを量産**して使う（追加ツール不要）。

### 学習用サンプルの生成（VOICEVOX）
1. VOICEVOX エンジンを起動（`http://127.0.0.1:50021`）
2. `python gen_samples.py` を実行
   - VOICEVOX の**全話者スタイル × 速度 × ピッチ × 抑揚**で「ずんだもん」を合成し、
     16kHz mono WAV を `wake_samples/` に出力（数百〜千件）
   - 表記ゆれを足したい場合は `gen_samples.py` の `VARIANTS` を編集
3. （任意・推奨）**自分の声**で「ずんだもん」を数十回録音して `wake_samples/` に足すと、
   あなたの声へのフィットが上がり検出が安定する

### モデル学習
**Colab を使わずローカル GPU で学習できる**（推奨）。詳細手順は
[`train_local/README.md`](train_local/README.md) を参照：

```bash
# 1) 正例を振り分け
python train_local/split_samples.py
# 2) Linux/WSL2 + CUDA で openWakeWord を学習（--generate_clips は付けない＝Piper不要）
python train.py --training_config train_local/config.yaml \
    --augment_clips --train_model --convert_to_tflite
```
- 大容量データセット（ネガティブ特徴・RIR・背景ノイズ）の DL が前提（`train_local/README.md` 参照）
- 完了で `my_custom_model/zundamon/zundamon.onnx` が出る → `C:\voice-agent\` にコピーし
  `.env` の `OWW_MODEL_PATH` に設定

> Colab で済ませたい場合は同じ `wake_samples/` を
> `notebooks/automatic_model_training.ipynb` の正例として読ませてもよい。

> **コツ**:「ずんだもん」は5モーラと長めで、ウェイクワードとしては誤検出しにくい良い語。
> それでも誤発火が多いときは `OWW_THRESHOLD` を上げる、反応が鈍いなら下げる。
> 合成声(VOICEVOX)だけだと自分の生声で反応しづらいことがあるので、手順3の自声追加が効く。

## 既知の制約 / 今後

- opencode の応答取得は同期。作業が長いと待つ（フィラー発話でごまかしている）。
  作業の実行中に割り込んだ場合、再生は止まるが opencode 側の処理自体はキャンセルされない。
  ストリーミング要約にしたい場合は `/event` SSE を使う拡張余地あり。
- opencode の API は版差がありうる。動かない場合は `http://127.0.0.1:4096/doc`
  （OpenAPI）で `POST /session/:id/message` の body を確認して `OpenCode.run()` を合わせる。
- ウェイクワード「ずんだもん」は TTS の話者名と同じため、応答中に AI が自分で「ずんだもん」と
  発話するとバージイン（`wakeword` モード）が自己発火しうる。気になる場合は `SYSTEM_PROMPT` に
  「自分を“ずんだもん”と名乗らない」と書くか、ウェイクワードを別語に変える。
```
