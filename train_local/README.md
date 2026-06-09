# ローカル学習（Colab 不要）—「ずんだもん」モデルを自分の GPU で作る

Colab は使わず、**Linux / WSL2 + CUDA** 上で openWakeWord をローカル学習する手順。
正例は **VOICEVOX で生成**（Piper 不要）。学習自体は小さなモデルなので速い。

> **どこで動かすか**: torchaudio / 各種音声ライブラリの相性で **Linux / WSL2 が圧倒的に楽**。
> 5080×2 機（Linux）か、Windows なら WSL2 + CUDA を推奨。学習は軽いので GPU はどれでも可。
>
> **⚠️ Python は 3.11〜3.12 を使うこと**。3.13/3.14 など最新版は torch・numba 等の
> 科学計算系 wheel がまだ揃っておらず、`torch` の import error・`torchcodec`・ソースビルド失敗
> （`CCompiler` / build-essential 要求）等の**連鎖エラー**になる。3.12 の venv で一掃できる。
>
> **⚠️ さらに `scipy<1.15` をピンすること**（Python 版とは別問題）。openWakeWord 学習の依存
> `acoustics` が SciPy 1.15 で削除された `sph_harm` を使うため、新しい scipy だと
> `ImportError: cannot import name 'sph_harm'` になる。→ `pip install "scipy<1.15"`。

## 手順

### 1. 正例＋負例の音声を用意（VOICEVOX）
Windows / VOICEVOX 側で：
```bash
# 正例（ウェイクワード「ずんだもん」）
python gen_samples.py                       # wake_samples/ に大量の「ずんだもん」WAV
python train_local/split_samples.py         # → my_custom_model/zundamon/positive_train|test/

# 負例（非ウェイクワード音声）。train.py は負例クリップが必須（空ディレクトリ不可）
python gen_negatives.py                      # → my_custom_model/zundamon/negative_train|test/
```
- `gen_negatives.py` は「こんにちは」等の一般語に加え、「ずんだ」「ずんだもち」等の**ハード負例**を
  VOICEVOX 多話者で合成。誤発火を減らす。件数が多く時間がかかる場合は `--max-styles 15` 等で抑制。

#### 1.5. 自分の声を足す（誤発火/未発火が多いとき・強く推奨）
VOICEVOX 合成だけだと生声で精度が出にくい。**自分の声の正例＋自分の声/部屋の負例**を
`record_wakeword.py`（同梱）で録って足すと、実環境で激変する。マイクのある Windows 側で実行：

> ⚠️ **生声は「合成データに“追加”」する。置き換えてはいけない**。
> openWakeWord は大量データ前提で、生声 60 件“だけ”で学習すると**全く反応しなくなる**。
> 必ず手順1（`gen_samples.py`＋`split_samples.py`＋`gen_negatives.py`＝合成 数千件）を済ませた
> **上に**生声を足すこと。`record_wakeword.py` / `split_samples.py` はどちらも既存ファイルを消さず
> **追記**する（ファイル名が別系統なので衝突しない）。学習前に `python inspect_clips.py` で
> `positive_train` が**数千件**あることを確認すると安全。
```bash
# 正例：ビープ後に「ずんだもん」を1回ずつ。60回くらい（声色・距離・速さ・向きを変える）
python record_wakeword.py --label positive --count 60

# 負例：自分の声で“紛らわしい語/雑談”（ずんだ・ずんだもち・こんにちは 等、毎回違う言葉）
python record_wakeword.py --label negative --count 40

# 負例：部屋の環境音（無言で生活音・TV など）。誤発火の抑制に効く
python record_wakeword.py --label negative --ambient --seconds 60
```
- `record_wakeword.py` はエージェントと同じ **PvRecorder(16kHz mono)** で録音し、推論時と音響特性を
  揃える。既存の VOICEVOX クリップに**追記**（上書きしない）、train/test も自動振り分け。
- 自分のペースで録りたいときは `--manual`（毎回 Enter で録音開始）。
- **未発火が多い**→正例を増やす（距離/声色のバリエーションを増やす）。
  **誤発火が多い**→負例（特に `--ambient` の環境音と、紛らわしい語）を増やす。
- 録り足したら **手順4を `--overwrite` 付きで再実行**（新クリップから特徴を作り直す）。

### 2. 学習環境を作る（Linux / WSL2）
**必ず Python 3.11〜3.12 の venv** を使う（3.14 等は依存 wheel 未整備で連鎖エラーになる）：

> ⚠️ **openwakeword は必ず git ソースから `-e` で入れる**。PyPI 版（`pip install openwakeword`）は
> **推論専用で学習コードを含まない**ため、`train.py` が
> `ImportError: cannot import name 'generate_adversarial_texts'` で落ちる。

```bash
python3.12 -m venv .venv && source .venv/bin/activate

git clone https://github.com/dscripka/openWakeWord
cd openWakeWord
pip install -e .                            # ← ソース版（学習コード入り）。PyPI版を上書き
pip install -r requirements_training.txt    # torch / torchaudio など（CUDA 版 torch を入れる）
pip install "scipy<1.15"                     # ← 最後に固定（sph_harm 対策。巻き戻り防止）
# ↑ 依存で詰まる場合は後述「community trainer」を使う手もある

# 特徴抽出モデル(melspectrogram.onnx / embedding_model.onnx)を取得（未取得だと学習時に NO_SUCHFILE）
python -c "from openwakeword import utils; utils.download_models()"
```

### 3. 大容量データセットを DL（数GB）— スクリプト同梱
公式 notebook のDLセルと同じ物を、付属スクリプトで取得する（保存先 `/data/oww` は変更可）。

> **前提: FFmpeg が必要**。`datasets` の音声デコード（torchcodec）が FFmpeg の共有
> ライブラリ（libavcodec 等）に依存する。未導入だと `prepare_aux_data.py` 実行時に
> `OSError: Could not load this library: ... libtorchcodec_core4.so` で落ちる。先に入れる：
> ```bash
> sudo apt-get update && sudo apt-get install -y ffmpeg   # torchcodec は FFmpeg 4〜7 対応
> ```

```bash
# (a) 直接DL分: ネガティブ特徴(ACAV100M) / FP検証特徴（.npy 2種）
bash train_local/download_datasets.sh /data/oww

# (b) RIR と AudioSet を取得して 16kHz mono に整形（HuggingFace datasets 経由）
pip install -r train_local/requirements_dataprep.txt
python train_local/prepare_aux_data.py --data /data/oww
```

> **背景ノイズについて**: 音楽データセット FMA(`rudraml/fma`) は読み込みスクリプト方式で、
> 新しい `datasets`(>=3.0) では `RuntimeError: Dataset scripts are no longer supported` となり
> 読めない。そのため**既定でスキップ**し、背景ノイズは **AudioSet のみ**で賄う（学習には十分）。
> どうしても FMA も使いたい場合は `--fma-count 1000` を付け、かつ `pip install "datasets<3.0"` が要る。

取得・生成されるもの → `config.yaml` の対応キー：

| ファイル/ディレクトリ | config.yaml のキー |
|---|---|
| `/data/oww/openwakeword_features_ACAV100M_2000_hrs_16bit.npy` | `feature_data_files.ACAV100M` |
| `/data/oww/validation_set_features.npy` | `false_positive_validation_data_path` |
| `/data/oww/mit_rirs/` | `rir_paths` |
| `/data/oww/noise_16k/`（AudioSet を16k化。FMAは既定スキップ） | `background_paths` |

同梱 `config.yaml` は既定でこのパスに合わせてある（`/data/oww` 以外にしたら書き換える）。

参照元（URL/スキーマがバージョンで変わったらここを確認）:
- 特徴 .npy: HuggingFace `davidscripka/openwakeword_features`
- RIR: HuggingFace `davidscripka/MIT_environmental_impulse_responses`
- ノイズ: `agkphysics/AudioSet`（config="balanced", split="train"。Parquet化済みのため
  `datasets` ストリーミングで取得）/ `rudraml/fma`(small)

### 4. 学習を実行
**クローンした openWakeWord リポジトリ内の `train.py`** を使う（ソース版 openwakeword と対）。
正例を自前供給するので **`--generate_clips` は付けない**。正例を先置きする都合上
**`--overwrite` を付けて**特徴生成を確実に走らせる（無いと「features already exist」で
スキップ→ `positive_features_test.npy` 不在で落ちる）：
```bash
python openWakeWord/train.py --training_config /path/to/voice-agent/train_local/config.yaml \
    --augment_clips --overwrite --train_model --convert_to_tflite
```
- `--augment_clips`: 正例に RIR/ノイズを重畳し特徴量化
- `--train_model`: 学習
- `--convert_to_tflite`: tflite も出力
- 完了で `my_custom_model/zundamon/zundamon.onnx`（+ `.tflite`）が出る

### 5. 配置
`zundamon.onnx` を Windows の `C:\voice-agent\` にコピー →
`config.py` の `OWW_MODEL_PATH` に設定。

## うまくいかない時

- **`OSError: ... libtorchcodec_core4.so`** → FFmpeg 未導入。`sudo apt-get install -y ffmpeg`。
- **`RuntimeError: Dataset scripts are no longer supported, but found fma.py`** → FMA は新しい
  `datasets` で読めない。既定でスキップ済み（AudioSet で代替）。使うなら `pip install "datasets<3.0"`。
- **`ImportError: cannot import name 'sph_harm' from scipy.special`** → openWakeWord 学習の依存
  `acoustics`（`acoustics/directivity.py`）が旧 API `sph_harm` を import するが、**SciPy 1.15 で
  `sph_harm` は削除**され `sph_harm_y` に改名された衝突。**scipy を下げれば直る**（Python 版とは無関係）：
  ```bash
  pip install "scipy<1.15"      # uv なら: uv pip install "scipy<1.15"
  ```
  ※ これは openWakeWord 学習環境の既知の相性問題。3.12 でも発生する。
- **`torch` の import error / torch・torchcodec が入らない** → 多くは Python 3.13/3.14 で
  wheel が無いのが原因。3.11〜3.12 の venv にすると解決することが多い。
- **`FileNotFoundError: .../negative_train`** → 負例クリップが無い。train.py は負例特徴を必須に
  使うため空ディレクトリ不可。`python gen_negatives.py` で VOICEVOX 負例を用意してから再実行
  （`--generate_clips`=piper を使わない本構成での代替）。
- **`Openwakeword features already exist, skipping...` → 直後に `FileNotFoundError: positive_features_test.npy`**
  → 前回の失敗 run の中間物や、先置きした `positive_train/test` を「生成済み」と誤判定し特徴生成を
  スキップしているのに、特徴 .npy は未完成、という状態。**`--overwrite` を付けて再実行**（特徴を作り直す。
  入力 WAV は消えない）。それでも残るなら `my_custom_model/<name>/` 内の `*features*.npy` を手で消す。
- **`ModuleNotFoundError: No module named 'onnx_tf'`（`--convert_to_tflite` の段で落ちる）**
  → これは **tflite 変換だけの失敗**で、その手前で `my_custom_model/zundamon.onnx` は**既に出力済み**。
  本エージェントは `OWW_FRAMEWORK="onnx"` で `.onnx` を直接使うため **tflite は不要**。
  `--convert_to_tflite` を**外して**実行すればよい（`onnx_tf` は TensorFlow 依存で壊れやすく、入れない）：
  ```bash
  uv run python train.py --training_config train_local/config.yaml --train_model
  ```
  ※ どうしても tflite が要る場合のみ `onnx_tf` + 対応 TF を別途用意（非推奨）。
- **`OnnxExporterError: Module onnx is not installed!` / `ModuleNotFoundError: No module named 'onnx'`（学習完了後の書き出しで落ちる）**
  → 学習自体は**完走している**が、`torch.onnx.export` に `onnx` 本体が要る。未導入だと最後の
  書き出しだけ失敗する（しかもチェックポイントは保存されないため、**入れてから再学習が必要**）。
  ```bash
  uv pip install onnx        # ml-dtypes も一緒に入る
  ```
  特徴量 `.npy` はキャッシュ済みなので、再学習は `--overwrite`/`--augment_clips` を**外して**
  学習＋書き出しだけ回せばよい（特徴生成・augmentation はスキップされ速い）：
  ```bash
  uv run python train.py --training_config train_local/config.yaml --train_model --convert_to_tflite
  ```
- **`AttributeError: 'int' object has no attribute 'items'`（学習ループ開始直後・`data.py` 内）**
  → `config.yaml` の `batch_n_per_class` を**スカラー（`1024` 等）にしている**のが原因。train.py は
  これを「各クラスごとの1バッチ件数」を表す **dict** として `mmap_batch_generator` に渡すため、
  int だと `n_per_class.items()` で落ちる。**dict にする**（キーは `feature_data_files` のキー
  ＋自動追加の `positive` / `adversarial_negative` に一致させる。合計が総バッチサイズ）：
  ```yaml
  batch_n_per_class:
    ACAV100M: 1024          # ← feature_data_files のキーと同名
    adversarial_negative: 50
    positive: 50
  ```
- **`AttributeError: module 'torchaudio' has no attribute 'info'`** → torchaudio が新しすぎる。
  `.info` は 2.8 で非推奨→**2.9 で削除**されたが、augmentation の `torch_audiomentations` が旧 API を
  呼ぶため衝突。**torch/torchaudio を 2.8 に固定**で解決（ペアで揃える）：
  ```bash
  pip install "torch==2.8.0" "torchaudio==2.8.0"   # uv: uv pip install ...
  ```
  `torchcodec` が torch 2.9 を要求して競合したら、学習には不要なので `pip uninstall torchcodec`。
- **`NO_SUCHFILE: ... resources/models/melspectrogram.onnx`** → 特徴抽出モデルが未取得。
  学習 venv で `python -c "from openwakeword import utils; utils.download_models()"` を実行。
- **`UserWarning: CUDAExecutionProvider is not available`** → onnxruntime が CPU 版。特徴抽出は
  軽いので無害（CPU で続行）。GPU で回したいなら `pip install onnxruntime-gpu`（必須ではない）。
- **`ModuleNotFoundError: No module named 'generate_samples'`** → train.py は起動時に無条件で
  piper-sample-generator の `generate_samples` を import する（`--generate_clips` 未使用でも）。
  本構成は同梱 stub（`train_local/piper_stub/`）で import だけ満たす。config.yaml の
  `piper_sample_generator_path` がこの stub を指していること＆**voice-agent ルートから実行**すること
  を確認（相対パスが CWD 基準で解決されるため）。合成正例も使うなら本物を clone してそのパスに変更。
- **依存（torchaudio/speechbrain/piper）で詰まる** → 完全ローカル特化の
  [lgpearson1771/openwakeword-trainer](https://github.com/lgpearson1771/openwakeword-trainer)
  が compat パッチ込みの 13 ステップ構成。これに VOICEVOX 正例を流す手もある。
- **ネガティブ不足のエラー** → `custom_negative_phrases` を設定して `--generate_clips` も付ける
  （この場合のみ Piper が必要）。
- **生声で反応しにくい** → 手順1の自声追加を増やす／`OWW_THRESHOLD` を下げる。
