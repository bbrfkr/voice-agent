"""
gen_samples.py が出力した wake_samples/ の WAV を、
openWakeWord 学習用の positive_train / positive_test へ振り分ける。

  python train_local/split_samples.py
        [--src wake_samples] [--out my_custom_model/zundamon] [--test-ratio 0.1]

config.yaml の output_dir=./my_custom_model, model_name=zundamon の場合、
--out は my_custom_model/zundamon（既定）でよい。
"""

import os
import glob
import shutil
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="wake_samples")
    ap.add_argument("--out", default=os.path.join("my_custom_model", "zundamon"))
    ap.add_argument("--test-ratio", type=float, default=0.1)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.src, "*.wav")))
    if not files:
        print(f"{args.src}/ に WAV がありません。先に gen_samples.py を実行してください。")
        return

    train_dir = os.path.join(args.out, "positive_train")
    test_dir = os.path.join(args.out, "positive_test")
    os.makedirs(train_dir, exist_ok=True)
    os.makedirs(test_dir, exist_ok=True)

    # 決定的に分割（step 個ごとに 1 件を test へ）
    n_test = max(1, int(len(files) * args.test_ratio))
    step = max(1, len(files) // n_test)

    n_train = n_test_done = 0
    for i, f in enumerate(files):
        if i % step == 0:
            dst = test_dir
            n_test_done += 1
        else:
            dst = train_dir
            n_train += 1
        shutil.copy2(f, os.path.join(dst, os.path.basename(f)))

    print(f"振り分け完了: train={n_train} 件 ({train_dir}), test={n_test_done} 件 ({test_dir})")
    print("次: config.yaml のデータセットパスを埋めて")
    print("  python train.py --training_config train_local/config.yaml --augment_clips --train_model --convert_to_tflite")


if __name__ == "__main__":
    main()
