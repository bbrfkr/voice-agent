"""
学習用クリップの健全性チェック。録音した正例/負例が無音・極端に短い・声が切れていないかを
ざっと数値で確認する（再学習で反応しなくなった時の原因切り分け用）。

使い方（voice-agent ルートから）:
  python inspect_clips.py [--dir my_custom_model/zundamon]
"""

import os
import wave
import glob
import argparse

import numpy as np


def stats(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
    dur = n / sr if sr else 0
    rms = float(np.sqrt(np.mean(pcm ** 2))) if len(pcm) else 0.0
    peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
    return dur, rms, peak, sr


def report(label, d):
    files = sorted(glob.glob(os.path.join(d, "*.wav")))
    if not files:
        print(f"[{label}] (ファイルなし) {d}")
        return
    durs, rmss, peaks, srs = [], [], [], set()
    silent, tiny = 0, 0
    for f in files:
        dur, rms, peak, sr = stats(f)
        durs.append(dur); rmss.append(rms); peaks.append(peak); srs.add(sr)
        if rms < 50:       # ほぼ無音
            silent += 1
        if dur < 0.35:     # 短すぎ（語が入りきらない）
            tiny += 1
    durs, rmss, peaks = np.array(durs), np.array(rmss), np.array(peaks)
    print(f"[{label}] {len(files)} 件  sr={sorted(srs)}")
    print(f"   長さ  : 平均 {durs.mean():.2f}s  最小 {durs.min():.2f}s  最大 {durs.max():.2f}s")
    print(f"   音量RMS: 平均 {rmss.mean():.0f}  最小 {rmss.min():.0f}  最大 {rmss.max():.0f}")
    print(f"   ピーク : 平均 {peaks.mean():.0f}（32767が最大）")
    warn = []
    if silent:
        warn.append(f"⚠ ほぼ無音 {silent} 件")
    if tiny:
        warn.append(f"⚠ 0.35s 未満 {tiny} 件")
    if srs and sorted(srs) != [16000]:
        warn.append(f"⚠ 16kHz でないファイルあり {sorted(srs)}")
    if warn:
        print("   " + "  /  ".join(warn))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join("my_custom_model", "zundamon"))
    args = ap.parse_args()
    for label in ["positive_train", "positive_test", "negative_train", "negative_test"]:
        report(label, os.path.join(args.dir, label))
    print("\n目安: 正例は 平均0.6〜1.5s / RMS 数百〜数千 が普通。"
          "『ほぼ無音』や『0.35s未満』が多いと、その正例ではモデルが反応しなくなる。")


if __name__ == "__main__":
    main()
