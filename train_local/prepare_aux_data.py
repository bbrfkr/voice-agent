"""
RIR と背景ノイズを 16kHz mono WAV に整える（openWakeWord 学習の augment 用）。

  pip install -r requirements_dataprep.txt
  python prepare_aux_data.py --data /data/oww [--audioset-count 4000]

出力:
  {data}/mit_rirs/*.wav     ← RIR（HuggingFace: davidscripka/MIT_environmental_impulse_responses）
  {data}/noise_16k/*.wav    ← AudioSet(balanced) を 16k 化（背景ノイズ）
config.yaml の rir_paths / background_paths をこの2ディレクトリに合わせること。

前提: FFmpeg（datasets の音声デコード=torchcodec が依存）→ `sudo apt-get install -y ffmpeg`
注意: FMA(rudraml/fma) はスクリプト型で新しい datasets では読めないため既定スキップ。
      背景ノイズは AudioSet のみで賄う（必要なら --fma-count>0 ＋ datasets<3.0）。
各ソースは個別に try/except し、壊れた物だけスキップして続行する。
"""

import os
import argparse

import numpy as np
import soundfile as sf
import librosa
from datasets import load_dataset

SR = 16000


def write_wav(path, audio, sr_in):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:                       # ステレオ → モノ
        audio = audio.mean(axis=1)
    if sr_in != SR:
        audio = librosa.resample(audio, orig_sr=sr_in, target_sr=SR)
    sf.write(path, audio, SR)


def do_rirs(data):
    out = os.path.join(data, "mit_rirs")
    os.makedirs(out, exist_ok=True)
    ds = load_dataset("davidscripka/MIT_environmental_impulse_responses",
                      split="train", streaming=True)
    n = 0
    for row in ds:
        a = row["audio"]
        write_wav(os.path.join(out, f"rir_{n:05d}.wav"), a["array"], a["sampling_rate"])
        n += 1
    print(f"RIR: {n} 件 → {out}")


def do_fma(data, count):
    # 注意: rudraml/fma は読み込みスクリプト(fma.py)方式。新しい datasets(>=3.0)は
    # スクリプト型を廃止したため RuntimeError になる。使うなら `datasets<3.0` が必要。
    # 既定ではスキップ（背景ノイズは AudioSet で賄う）。
    out = os.path.join(data, "noise_16k")
    os.makedirs(out, exist_ok=True)
    ds = load_dataset("rudraml/fma", name="small", split="train", streaming=True)
    n = 0
    for row in ds:
        if n >= count:
            break
        a = row["audio"]
        try:
            write_wav(os.path.join(out, f"fma_{n:05d}.wav"), a["array"], a["sampling_rate"])
        except Exception as e:
            print(f"  [skip fma] {e}")
            continue
        n += 1
    print(f"FMA: {n} 件 → {out}")


def do_audioset(data, count):
    # AudioSet は Parquet 化されたため datasets ストリーミングで取得（config="balanced"）。
    out = os.path.join(data, "noise_16k")
    os.makedirs(out, exist_ok=True)
    ds = load_dataset("agkphysics/AudioSet", "balanced", split="train", streaming=True)
    n = 0
    for row in ds:
        if n >= count:
            break
        a = row["audio"]
        try:
            write_wav(os.path.join(out, f"audioset_{n:05d}.wav"), a["array"], a["sampling_rate"])
        except Exception as e:
            print(f"  [skip audioset] {e}")
            continue
        n += 1
    print(f"AudioSet: {n} 件 → {out}")


def _safe(label, fn):
    """1ソースが壊れても全体を止めないためのラッパ（HF側の仕様変更に強くする）。"""
    try:
        fn()
    except Exception as e:
        print(f"[警告] {label} をスキップしました: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data/oww")
    ap.add_argument("--fma-count", type=int, default=0,
                    help="FMA(music)の件数。rudraml/fma は新しい datasets では読めない"
                         "(スクリプト廃止)ため既定0=スキップ。使うなら datasets<3.0 が必要")
    ap.add_argument("--audioset-count", type=int, default=4000,
                    help="AudioSet(背景ノイズ)の件数。多いほど頑健")
    ap.add_argument("--skip-rir", action="store_true")
    ap.add_argument("--skip-audioset", action="store_true")
    args = ap.parse_args()

    if not args.skip_rir:
        _safe("RIR", lambda: do_rirs(args.data))
    if args.fma_count > 0:
        _safe("FMA", lambda: do_fma(args.data, args.fma_count))
    if not args.skip_audioset:
        _safe("AudioSet", lambda: do_audioset(args.data, args.audioset_count))

    print(f"\n完了。config.yaml を以下に合わせる:")
    print(f"  rir_paths:        [{os.path.join(args.data, 'mit_rirs')}]")
    print(f"  background_paths: [{os.path.join(args.data, 'noise_16k')}]")


if __name__ == "__main__":
    main()
