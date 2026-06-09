"""
ウェイクワード学習用サンプル生成（VOICEVOX）

VOICEVOX の全話者スタイル × 速度 × ピッチ × 抑揚 で指定フレーズを合成し、
16kHz mono WAV を出力する。openWakeWord 学習の「正例（positive）」に使う。

前提: VOICEVOX エンジンが起動していること（config.VOICEVOX_URL）。
使い方:
    python gen_samples.py
出力: ./wake_samples/ に WAV が並ぶ。これを openWakeWord の学習ノートブックの
      正例サンプルとして読み込ませる（README「学習用サンプルの生成」参照）。
"""

import os
import json
import requests

import config as C

PHRASE = "ずんだもん"          # ウェイクワード（表記ゆれを足したいなら下の VARIANTS へ）
VARIANTS = [PHRASE]            # 例: ["ずんだもん", "ずんだもーん"]
OUT_DIR = "wake_samples"

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
    os.makedirs(OUT_DIR, exist_ok=True)
    styles = list_styles()
    total = len(styles) * len(VARIANTS) * len(SPEEDS) * len(PITCHES) * len(INTONATIONS)
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
                        fn = os.path.join(
                            OUT_DIR,
                            f"wake_s{sid}_v{vi}_sp{sp}_pi{pi}_it{it}.wav",
                        )
                        with open(fn, "wb") as f:
                            f.write(wav)
                        n += 1
        print(f"  {spk} / {stname} (id={sid})  累計 {n}")

    print(f"\n完成: {n} 件を {OUT_DIR}/ に出力しました。")
    print("openWakeWord 学習の正例サンプルとして使ってください。")


if __name__ == "__main__":
    main()
