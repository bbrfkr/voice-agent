#!/usr/bin/env bash
# openWakeWord ローカル学習に必要な大容量データを取得する。
# 公式 notebook(automatic_model_training.ipynb) のDLセルと同じ物。
#
#   bash download_datasets.sh [DATA_DIR]   # 既定 /data/oww
#
# 直接DLできる .npy 特徴ファイルのみここで取得。
# RIR / 背景ノイズ(FMA・AudioSet) は HuggingFace datasets 経由なので prepare_aux_data.py で整える。
# （AudioSet は Parquet 化され .tar 直DLは廃止。datasets ストリーミングで取得する）
set -euo pipefail

DATA="${1:-/data/oww}"
mkdir -p "$DATA"
cd "$DATA"
echo "保存先: $DATA"

echo "== [1/2] ネガティブ特徴 ACAV100M（数GB）=="
wget -c "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"

echo "== [2/2] FP 検証特徴 =="
wget -c "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy"

echo
echo "直接DL分は完了。次に RIR / FMA / AudioSet の取得・16k化:"
echo "  pip install -r requirements_dataprep.txt"
echo "  python prepare_aux_data.py --data $DATA"
