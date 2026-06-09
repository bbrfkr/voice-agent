"""
自分の声でウェイクワード学習データを録音する（WSL から実行。マイクは PulseAudio 経由で
ホスト Windows から渡す。設定は DOCKER.md 参照）。

なぜ必要か:
  既定モデルは VOICEVOX 合成音声のみで学習しているため、生声では誤発火/未発火が出やすい。
  あなた自身の声の「ずんだもん」(正例) と、あなたの声の非ウェイクワード発話＋部屋の環境音
  (負例) を足して再学習すると、実環境での精度が大きく上がる。

特徴:
  - エージェントと同じ PvRecorder(16kHz mono) で録音 → 推論時と音響特性を揃える
  - 既存の VOICEVOX クリップに「追記」する（上書きしない）
  - train/test に自動振り分け

使い方（リポジトリのルートから実行 / config.py が必要）:
  # 正例（「ずんだもん」を 60 回。ビープ後に1回ずつ言う）
  python train_local/record_wakeword.py --label positive --count 60

  # 負例：自分の声で“紛らわしい語”や雑談を録る
  python train_local/record_wakeword.py --label negative --count 40

  # 負例：部屋の環境音（無言で生活音・TV など）。--ambient で連続録音を分割保存
  python train_local/record_wakeword.py --label negative --ambient --seconds 60

  オプション:
    --seconds 1.8     1クリップの長さ（通常録音時）。ambient時は総録音秒数
    --gap 0.6         クリップ間の待ち
    --manual          毎回 Enter を押してから録音（自分のペースで）
    --test-ratio 0.1  test に回す割合
    --out my_custom_model/zundamon   出力ベース（既定）
"""

import os
import sys
import time
import wave
import argparse

import numpy as np
from pvrecorder import PvRecorder

# train_local/ から実行されるため、リポジトリ直下の config.py を import 可能にする
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C

SAMPLE_RATE = 16000
FRAME = 512   # 32ms。録音長の分解能


def _beep(freq=880, ms=180):
    # Windows は winsound.Beep が最も確実（既定の音声デバイスで鳴る）。
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(int(freq), int(ms))
            return
        except Exception as e:
            print(f"（ビープ不可: {e}）", file=sys.stderr)
    # 非 Windows or フォールバック: sounddevice
    try:
        import sounddevice as sd
        sec = ms / 1000.0
        t = np.linspace(0, sec, int(44100 * sec), endpoint=False)
        sd.play((0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32), 44100)
        sd.wait()
    except Exception as e:
        print(f"（ビープ不可: {e}）", file=sys.stderr)


def _save_wav(path, pcm_int16):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)          # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(np.asarray(pcm_int16, dtype=np.int16).tobytes())


def _drain(recorder, max_frames=2000):
    """PvRecorder の内部バッファに溜まった古いフレームを捨て、『今』から録れるようにする。
    バックログのフレームは即座に返るが、追いついたら read() が約フレーム長ブロックする。
    その差で『追いついた』を検出して止める。"""
    half = (FRAME / SAMPLE_RATE) * 0.5
    for _ in range(max_frames):
        t = time.time()
        recorder.read()
        if (time.time() - t) > half:   # リアルタイムに追いついた
            break


def _record_seconds(recorder, seconds):
    n_frames = max(1, int(round(seconds * SAMPLE_RATE / FRAME)))
    buf = []
    for _ in range(n_frames):
        buf.extend(recorder.read())
    return np.asarray(buf, dtype=np.int16)


def _trim_silence(pcm, thresh, pad_lead=0.1, pad_trail=0.2, win_ms=20):
    """前後の無音を削り、発話の前に pad_lead・後ろに pad_trail の余白を残す。
    thresh 未満を無音とみなす。全部無音なら元のまま返す。
    末尾余白は録音長が足りないと頭打ちになる点に注意（その場合は --seconds を増やす）。"""
    win = max(1, int(SAMPLE_RATE * win_ms / 1000))
    n = len(pcm) // win
    if n == 0:
        return pcm
    f = pcm[:n * win].astype(np.float32).reshape(n, win)
    rms = np.sqrt(np.mean(f ** 2, axis=1))
    voiced = np.where(rms >= thresh)[0]
    if len(voiced) == 0:
        return pcm
    start = max(0, voiced[0] * win - int(SAMPLE_RATE * pad_lead))
    end = min(len(pcm), (voiced[-1] + 1) * win + int(SAMPLE_RATE * pad_trail))
    return pcm[start:end]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", choices=["positive", "negative"], required=True)
    ap.add_argument("--count", type=int, default=60, help="録音するクリップ数（ambient以外）")
    ap.add_argument("--seconds", type=float, default=2.5,
                    help="1クリップの長さ秒（ambient時は総録音秒数）。短いと言い切る前に切れる")
    ap.add_argument("--gap", type=float, default=0.6, help="クリップ間の待ち秒")
    ap.add_argument("--manual", action="store_true", help="毎回 Enter を押してから録音")
    ap.add_argument("--no-trim", action="store_true",
                    help="前後無音の自動トリミングを無効化（既定は有効）")
    ap.add_argument("--trim-rms", type=float, default=None,
                    help="トリミングの無音閾値 RMS（既定: config.SILENCE_RMS）")
    ap.add_argument("--pad-lead", type=float, default=0.1, help="発話前に残す余白秒")
    ap.add_argument("--pad-end", type=float, default=0.2, help="発話後に残す余白秒")
    ap.add_argument("--ambient", action="store_true",
                    help="環境音モード：連続録音して --seconds 秒を 1.8s 単位で分割保存")
    ap.add_argument("--test-ratio", type=float, default=0.1)
    ap.add_argument("--out", default=os.path.join("my_custom_model", "zundamon"))
    args = ap.parse_args()

    train_dir = os.path.join(args.out, f"{args.label}_train")
    test_dir = os.path.join(args.out, f"{args.label}_test")
    os.makedirs(train_dir, exist_ok=True)
    os.makedirs(test_dir, exist_ok=True)
    every = max(2, int(round(1 / args.test_ratio)))
    trim_rms = args.trim_rms if args.trim_rms is not None else getattr(C, "SILENCE_RMS", 350)

    recorder = PvRecorder(frame_length=FRAME,
                          device_index=getattr(C, "INPUT_DEVICE_INDEX", -1))
    recorder.start()
    # 既存ファイルと衝突しない連番開始位置（タイムスタンプ接頭辞で一意化）
    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved = 0
    try:
        if args.ambient:
            print(f"環境音モード：{args.seconds:.0f} 秒、無言で部屋の生活音・TV などを流してください。")
            print("3 秒後に開始…"); time.sleep(3)
            _drain(recorder)   # 待機中に溜まった古い音声を捨てる
            pcm = _record_seconds(recorder, args.seconds)
            chunk = int(1.8 * SAMPLE_RATE)
            for i in range(0, len(pcm) - chunk, chunk):
                dst = test_dir if (saved % every == 0) else train_dir
                _save_wav(os.path.join(dst, f"mic_amb_{stamp}_{saved:04d}.wav"),
                          pcm[i:i + chunk])
                saved += 1
            print(f"環境音 {saved} クリップを保存しました。")
        else:
            if args.label == "positive":
                print('ビープ後に毎回はっきり「ずんだもん」と言ってください。')
                print("声色・距離・速さ・向きを少しずつ変えると頑健になります。")
            else:
                print('ビープ後に“紛らわしい語/雑談”を言ってください（例：ずんだ・ずんだもち・')
                print('こんにちは・今日はいい天気・音楽かけて 等）。毎回違う言葉が理想です。')
            for i in range(args.count):
                if args.manual:
                    input(f"[{i+1}/{args.count}] Enter で録音開始…")
                else:
                    print(f"[{i+1}/{args.count}] 3..2..1..", end="", flush=True)
                    time.sleep(0.6)
                _beep()
                _drain(recorder)   # ビープ中に溜まった古い音声を捨て、今から録る
                print(" 🔴録音中（今どうぞ）", end="", flush=True)
                pcm = _record_seconds(recorder, args.seconds)
                if not args.no_trim:
                    pcm = _trim_silence(pcm, trim_rms,
                                        pad_lead=args.pad_lead, pad_trail=args.pad_end)
                dst = test_dir if (saved % every == 0) else train_dir
                _save_wav(os.path.join(dst, f"mic_{args.label}_{stamp}_{saved:04d}.wav"), pcm)
                saved += 1
                print(f" ✓保存 ({len(pcm)/SAMPLE_RATE:.1f}s)")
                if not args.manual:
                    time.sleep(args.gap)
    except KeyboardInterrupt:
        print("\n中断しました。")
    finally:
        recorder.delete()

    print(f"\n完了：{saved} 件 → {train_dir} / {test_dir}")
    print("次：train.py を --overwrite 付きで再学習（新クリップから特徴を作り直す）")
    print("  uv run python train.py --training_config train_local/config.yaml "
          "--augment_clips --overwrite --train_model")


if __name__ == "__main__":
    main()
