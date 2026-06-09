"""
ウェイクワード学習用「正例(positive)」サンプル生成（VOICEVOX）

VOICEVOX の全話者スタイル × 速度 × ピッチ × 抑揚 で「ずんだもん」を合成し、
16kHz mono WAV を openWakeWord 学習の positive_train / positive_test に直接振り分ける。
（負例 gen_negatives.py と対称。旧 split_samples.py の振り分けは本スクリプトへ統合した。）

前提: VOICEVOX 起動中（config.VOICEVOX_URL が指す先）。
使い方（リポジトリのルートから実行）:
    python train_local/gen_samples.py [--out my_custom_model/zundamon] [--test-ratio 0.1]
出力: {out}/positive_train, {out}/positive_test に WAV。既存ファイルは消さず**追記**する
      （自声録音 record_wakeword.py のクリップとはファイル名系統が別なので衝突しない）。
"""

import os
import sys
import json
import argparse

import requests

# train_local/ から実行されるため、リポジトリ直下の config.py を import 可能にする
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C

PHRASE = "ずんだもん"          # ウェイクワード（表記ゆれを足したいなら下の VARIANTS へ）
VARIANTS = [PHRASE]            # 例: ["ずんだもん", "ずんだもーん"]

# バリエーション（声色は全話者スタイルで稼ぐので、ここは韻律のゆらぎ）
SPEEDS      = [0.85, 1.0, 1.15, 1.3]   # speedScale
PITCHES     = [-0.05, 0.0, 0.05]       # pitchScale
INTONATIONS = [1.0, 1.3]               # intonationScale


def list_styles():
    """VOICEVOX の全話者スタイルを (話者名, スタイル名, styleId) で返す。"""
    r = requests.get(f"{C.VOICEVOX_URL}/speakers", timeout=30)
    r.raise_for_status()
    out = []
    for sp in r.json():
        for st in sp.get("styles", []):
            out.append((sp["name"], st["name"], st["id"]))
    return out


def synth(text, style_id, speed, pitch, intonation):
    """1 サンプル合成して 16kHz mono の WAV バイト列を返す。"""
    q = requests.post(f"{C.VOICEVOX_URL}/audio_query",
                      params={"text": text, "speaker": style_id}, timeout=30)
    q.raise_for_status()
    query = q.json()
    query["speedScale"] = speed
    query["pitchScale"] = pitch
    query["intonationScale"] = intonation
    query["outputSamplingRate"] = 16000   # openWakeWord は 16kHz
    query["outputStereo"] = False
    r = requests.post(f"{C.VOICEVOX_URL}/synthesis",
                      params={"speaker": style_id},
                      data=json.dumps(query),
                      headers={"Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()
    return r.content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("my_custom_model", "zundamon"),
                    help="出力ベース（{out}/positive_train|test に振り分け）")
    ap.add_argument("--test-ratio", type=float, default=0.1)
    args = ap.parse_args()

    train_dir = os.path.join(args.out, "positive_train")
    test_dir = os.path.join(args.out, "positive_test")
    os.makedirs(train_dir, exist_ok=True)
    os.makedirs(test_dir, exist_ok=True)

    styles = list_styles()
    total = len(styles) * len(VARIANTS) * len(SPEEDS) * len(PITCHES) * len(INTONATIONS)
    every = max(2, int(round(1 / args.test_ratio)))   # every 件ごとに1件を test へ
    print(f"{len(styles)} スタイル → 最大 {total} 件を生成します。")

    n = 0
    for (spk, stname, sid) in styles:
        for vi, text in enumerate(VARIANTS):
            for sp in SPEEDS:
                for pi in PITCHES:
                    for it in INTONATIONS:
                        try:
                            wav = synth(text, sid, sp, pi, it)
                        except Exception as e:
                            print(f"  [skip] style={sid}: {e}")
                            continue
                        dst = test_dir if (n % every == 0) else train_dir
                        fn = os.path.join(
                            dst, f"wake_s{sid}_v{vi}_sp{sp}_pi{pi}_it{it}.wav")
                        with open(fn, "wb") as f:
                            f.write(wav)
                        n += 1
        print(f"  {spk} / {stname} (id={sid})  累計 {n}")

    print(f"\n完成: 正例 {n} 件 → {train_dir} / {test_dir}")
    print("次: 負例を用意 → python train_local/gen_negatives.py")


if __name__ == "__main__":
    main()
