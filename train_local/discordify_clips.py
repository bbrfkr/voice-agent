"""Discord 経由の音響劣化を学習クリップに追加する（Opus 往復）。

Discord ボイスチャンネル経由の音声は、クライアントの Opus エンコード（voip モード・
低ビットレート）→ サーバ転送 → bot 側デコード → 48k→16k リサンプルを経るため、
ローカルマイクの生音声で学習した openWakeWord モデルはスコアが全体に沈む
（実測でローカル比 2〜4 割減）。本スクリプトは既存の正例/負例クリップに
**16k → 48k → Opus(voip) encode → decode → 16k** の往復をかけた複製
（`<元名>_discord.wav`）を同じディレクトリに追加し、学習データの音響特性を
Discord 経由の実入力に近づける。

- 既存ファイルは消さない（追記のみ）。`_discord.wav` が既にあるクリップはスキップ
  （再実行は冪等）。`_discord.wav` 自身にはかけない（劣化の二重適用を防ぐ）。
- ビットレートはファイル名から決定論的に 32/48/64 kbps を選ぶ（Discord の既定 64kbps と、
  モバイル/低帯域時の低レートをカバー。再実行しても同じ結果になる）。
- 依存: ffmpeg（libopus 入り。`sudo apt-get install -y ffmpeg`）

使い方（リポジトリ直下から）:
    python train_local/discordify_clips.py                # 既定: zundamon の全 4 ディレクトリ
    python train_local/discordify_clips.py --dirs my_custom_model/zundamon/positive_train
実行後は README 手順 4 を `--overwrite` 付きで再実行して特徴を作り直す。
"""

import argparse
import hashlib
import subprocess
import sys
from multiprocessing import Pool
from pathlib import Path

DEFAULT_DIRS = [
    "my_custom_model/zundamon/positive_train",
    "my_custom_model/zundamon/positive_test",
    "my_custom_model/zundamon/negative_train",
    "my_custom_model/zundamon/negative_test",
]
BITRATES = ["32k", "48k", "64k"]
SUFFIX = "_discord"


def _bitrate_for(name: str) -> str:
    """ファイル名から決定論的にビットレートを選ぶ（再実行で結果が変わらないように）。"""
    h = int(hashlib.sha1(name.encode()).hexdigest(), 16)
    return BITRATES[h % len(BITRATES)]


def _convert(src: Path) -> str | None:
    """1 ファイルを Opus 往復させて <stem>_discord.wav を作る。失敗時はメッセージを返す。"""
    dst = src.with_name(src.stem + SUFFIX + ".wav")
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(src),
         "-ar", "48000", "-c:a", "libopus", "-b:a", _bitrate_for(src.name),
         "-application", "voip", "-frame_duration", "20", "-f", "ogg", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    dec = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", "-",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        stdin=enc.stdout, stderr=subprocess.DEVNULL,
    )
    enc.stdout.close()
    enc.wait()
    if enc.returncode != 0 or dec.returncode != 0 or not dst.exists():
        dst.unlink(missing_ok=True)
        return f"変換失敗: {src}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dirs", nargs="*", default=DEFAULT_DIRS,
                    help="対象ディレクトリ（既定: zundamon の正例/負例 4 つ）")
    ap.add_argument("--jobs", type=int, default=8, help="並列数（既定 8）")
    args = ap.parse_args()

    targets = []
    for d in args.dirs:
        p = Path(d)
        if not p.is_dir():
            print(f"スキップ（無いディレクトリ）: {d}", file=sys.stderr)
            continue
        wavs = sorted(p.glob("*.wav"))
        names = {w.name for w in wavs}
        for w in wavs:
            if w.stem.endswith(SUFFIX):          # 劣化済みクリップ自身には掛けない
                continue
            if (w.stem + SUFFIX + ".wav") in names:  # 既に変換済み（冪等）
                continue
            targets.append(w)

    if not targets:
        print("追加すべきクリップはありません（すべて変換済み）。")
        return
    print(f"{len(targets)} 件を Opus 往復変換します（並列 {args.jobs}）…")
    errors = 0
    with Pool(args.jobs) as pool:
        for i, err in enumerate(pool.imap_unordered(_convert, targets, chunksize=32), 1):
            if err:
                errors += 1
                print(err, file=sys.stderr)
            if i % 1000 == 0:
                print(f"  {i}/{len(targets)} 完了")
    print(f"完了: {len(targets) - errors} 件追加（失敗 {errors} 件）")


if __name__ == "__main__":
    main()
