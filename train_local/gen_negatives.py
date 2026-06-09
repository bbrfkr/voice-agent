"""
ウェイクワード学習用の「負例(negative)」クリップを VOICEVOX で生成し、
openWakeWord の negative_train / negative_test に直接振り分ける。

なぜ必要か:
  train.py は negative_train/test のクリップから adversarial_negative 特徴を作り、
  学習でこれを必須に使う（空ディレクトリ不可）。本構成は --generate_clips(piper) を
  使わないため、非ウェイクワード音声を VOICEVOX で負例として用意する。
  「ずんだ」「ずんだもち」等の“似て非なる”ハード負例を混ぜると誤発火が減る。

前提: VOICEVOX 起動中。先に gen_samples.py（正例）を済ませておくこと。
使い方（リポジトリのルートから実行）:
  python train_local/gen_negatives.py [--out my_custom_model/zundamon] [--test-ratio 0.1] [--max-styles N]
"""

import os
import argparse

from gen_samples import list_styles, synth   # 同じ VOICEVOX ヘルパを再利用

# 一般語 ＋「ずんだもん」に似たハード負例（先頭一致・部分一致）
NEG_PHRASES = [
    # 一般語・よくある発話
    "こんにちは", "おはよう", "こんばんは", "ありがとう", "さようなら",
    "もしもし", "おなかすいた", "きょうはいいてんき", "なんじですか",
    "おんがくをかけて", "でんきをけして", "ちょっとまって", "だいじょうぶ",
    "りんご", "みかん", "コンピュータ", "プログラム", "にほんご", "もういちど",
    # ハード負例（ずんだもん に似せる）
    "ずんだ", "ずんだもち", "ずんだあじ", "ずんだいろ", "だんご",
    "もんだい", "もんだもん", "ずんずん", "ずんだもんじゃない",
]
SPEEDS = [0.9, 1.0, 1.15]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("my_custom_model", "zundamon"))
    ap.add_argument("--test-ratio", type=float, default=0.1)
    ap.add_argument("--max-styles", type=int, default=0,
                    help="使う VOICEVOX スタイル数の上限（0=全部）。多いと時間がかかる")
    args = ap.parse_args()

    train_dir = os.path.join(args.out, "negative_train")
    test_dir = os.path.join(args.out, "negative_test")
    os.makedirs(train_dir, exist_ok=True)
    os.makedirs(test_dir, exist_ok=True)

    styles = list_styles()
    if args.max_styles > 0:
        styles = styles[:args.max_styles]
    every = max(2, int(round(1 / args.test_ratio)))   # every 件ごとに1件を test へ

    n = 0
    for pidx, phrase in enumerate(NEG_PHRASES):
        for (_, _, sid) in styles:
            for sp in SPEEDS:
                try:
                    wav = synth(phrase, sid, sp, 0.0, 1.0)
                except Exception as e:
                    print(f"  [skip] '{phrase}' style={sid}: {e}")
                    continue
                dst = test_dir if (n % every == 0) else train_dir
                fn = os.path.join(dst, f"neg_p{pidx}_s{sid}_sp{sp}.wav")
                with open(fn, "wb") as f:
                    f.write(wav)
                n += 1
                if n % 200 == 0:
                    print(f"  {n} 件生成…")

    print(f"完成: 負例 {n} 件 → {train_dir} / {test_dir}")
    print("次: train.py を --augment_clips --overwrite で再実行（負例特徴が作られる）")


if __name__ == "__main__":
    main()
