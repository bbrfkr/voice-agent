"""
低遅延・音声エージェント（Windows クライアント）

フロー:
  「ずんだもん」(openWakeWord) → 録音(VAD) → faster-whisper(STT) →
  llama.cpp(会話LLM, ストリーミング)
     ├ 通常会話     : 句点ごとに VOICEVOX で逐次再生（生成と再生をパイプライン）
     └ [[TASK]] 検出: opencode serve に作業委譲 → 結果を LLM が音声で要約

設定は config.py（環境変数 / `.env` から読み込むローダ）に分離。値の編集は `.env` で行う。
"""

import io
import os
import re
import sys
import time
import json
import glob
import queue
import threading


def _register_cuda_dll_dirs():
    """Windows で faster-whisper(CTranslate2) が要求する CUDA12 cuBLAS/cuDNN の
    DLL を見つけられるようにする。pip の nvidia-* wheel は DLL を
    site-packages/nvidia/<pkg>/bin に置くが、Windows の DLL 検索パスに載らないため、
    CTranslate2 が cublas64_12.dll を遅延ロードする時に失敗する。
    add_dll_directory と PATH の両方へ登録して確実に解決する。
    ※ faster_whisper を import する前に呼ぶこと（import 時に native lib が走るため）。
    依存: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
    """
    if sys.platform != "win32":
        return
    import site
    bases = list(site.getsitepackages())
    user = getattr(site, "getusersitepackages", lambda: None)()
    if user:
        bases.append(user)
    bases += [p for p in sys.path if p.endswith("site-packages")]
    found, seen = [], set()
    for base in bases:
        for binp in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            binp = os.path.normpath(binp)
            if binp in seen or not os.path.isdir(binp):
                continue
            seen.add(binp)
            try:
                os.add_dll_directory(binp)
            except OSError:
                pass
            os.environ["PATH"] = binp + os.pathsep + os.environ.get("PATH", "")
            found.append(binp)
    has_cublas = any(glob.glob(os.path.join(d, "cublas64_*.dll")) for d in found)
    if found and has_cublas:
        print(f"CUDA DLL ディレクトリを登録: {len(found)} 件")
    elif found:
        print(f"⚠ nvidia の bin を {len(found)} 件登録しましたが cublas64_*.dll が見当たりません。"
              "nvidia-cublas-cu12 を入れ直してください。")
    else:
        print("⚠ nvidia-* の DLL が見つかりません。CUDA を使うには:\n"
              "    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")


# faster_whisper(CTranslate2) を import する前に DLL 検索パスを通す
_register_cuda_dll_dirs()

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf
import openwakeword
from openwakeword.model import Model as OWWModel
from pvrecorder import PvRecorder
from faster_whisper import WhisperModel

import config as C

SAMPLE_RATE = 16000   # whisper / openWakeWord とも 16kHz 固定
FRAME_LENGTH = 1280   # 80ms。openWakeWord の推奨フレーム長

# 文の区切り（ここで TTS に流す単位を切る）
_SENT_BOUNDARY = re.compile(r"[。．！？!?\n]")
_TASK_SENTINEL = "[[TASK]]"


# ───────────────────────────────── ウェイクワード（openWakeWord, OSS） ─────────────────────────────────
class WakeWord:
    """openWakeWord による検出器。process(pcm)->bool で 1 フレーム判定する。
    ウェイクワード待受と、バージイン(wakeword モード)で共用する。"""

    def __init__(self):
        # 共有の特徴抽出モデル（melspectrogram / embedding）を必要なら取得
        try:
            openwakeword.utils.download_models(model_names=[])
        except Exception:
            pass
        self.model = OWWModel(
            wakeword_models=[C.OWW_MODEL_PATH],
            inference_framework=C.OWW_FRAMEWORK,
        )
        self.threshold = C.OWW_THRESHOLD
        # OWW_DEBUG=True で、声に対する検出スコアを表示（閾値調整・感度確認用）。
        # 既定 True（診断中）。安定したら config.py に OWW_DEBUG=False を足す。
        self.debug = getattr(C, "OWW_DEBUG", True)
        self.last_score = 0.0

    def process(self, pcm) -> bool:
        arr = np.asarray(pcm, dtype=np.int16)
        scores = self.model.predict(arr)
        mx = max(scores.values()) if scores else 0.0
        self.last_score = float(mx)
        # 0.1 以上に上がった瞬間だけ出す（無音時の 0.00 連発を避ける）
        if self.debug and mx >= 0.1:
            hit = "  ← 発火！" if mx >= self.threshold else ""
            print(f"[wake] score={mx:.2f} (閾値 {self.threshold}){hit}")
        return mx >= self.threshold

    def reset(self):
        """検出直後に内部バッファを消し、連続誤発火を防ぐ。"""
        self.model.reset()


# ───────────────────────────────── TTS（VOICEVOX） ─────────────────────────────────
class Speaker:
    """文字列をキューで受け取り、別スレッドで VOICEVOX 合成→再生する。
    生成(LLM)と発話(TTS)を重ねることで体感遅延を下げる。
    interrupt() でバージイン（再生の即時停止＋未再生キュー破棄）に対応。"""

    def __init__(self):
        self.q: "queue.Queue[str|None]" = queue.Queue()
        self._interrupted = False
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def say(self, text: str):
        text = text.strip()
        if text and not self._interrupted:
            self.q.put(text)

    def wait_done(self):
        """キューに積んだ発話を全て喋り終えるまで待つ（割り込み時は即座に返る）。"""
        self.q.join()

    def clear_interrupt(self):
        """次ターン開始時に割り込み状態を解除する。"""
        self._interrupted = False

    def interrupt(self):
        """再生を即停止し、未再生のキューを捨てる（バージイン用）。"""
        self._interrupted = True
        sd.stop()  # 再生中の sd.wait() を解除
        while True:
            try:
                self.q.get_nowait()
                self.q.task_done()
            except queue.Empty:
                break

    def _run(self):
        while True:
            text = self.q.get()
            if text is None:
                self.q.task_done()
                break
            try:
                if self._interrupted:
                    continue
                wav = self._synth(text)
                if wav is not None and not self._interrupted:
                    data, sr = sf.read(io.BytesIO(wav), dtype="float32")
                    sd.play(data, sr)
                    sd.wait()
            except Exception as e:
                print(f"[TTS error] {e}", file=sys.stderr)
            finally:
                self.q.task_done()

    def _synth(self, text: str):
        # 1) audio_query 2) synthesis の2段（VOICEVOX 標準）
        q = requests.post(
            f"{C.VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": C.VOICEVOX_SPEAKER},
            timeout=30,
        )
        q.raise_for_status()
        query = q.json()
        query["speedScale"] = C.VOICEVOX_SPEED
        r = requests.post(
            f"{C.VOICEVOX_URL}/synthesis",
            params={"speaker": C.VOICEVOX_SPEAKER},
            data=json.dumps(query),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        return r.content


def _play_beep():
    """短い確認音（TTS を待たず即フィードバック）。"""
    sr = 44100
    t = np.linspace(0, 0.12, int(sr * 0.12), endpoint=False)
    tone = 0.2 * np.sin(2 * np.pi * 880 * t).astype(np.float32)
    sd.play(tone, sr)
    sd.wait()


def acknowledge(speaker):
    """ウェイクワード検出時のフィードバック。
    ACK_MODE: "voice"=VOICEVOX で返事 / "beep"=ビープ / "both" / "off"。
    後方互換: ACK_MODE 未設定なら ACK_BEEP=True を beep として扱う。"""
    mode = getattr(C, "ACK_MODE", None)
    if mode is None:
        mode = "beep" if getattr(C, "ACK_BEEP", True) else "off"
    if mode in ("beep", "both"):
        _play_beep()
    if mode in ("voice", "both"):
        text = getattr(C, "ACK_VOICE_TEXT", "はい")
        if text:
            speaker.clear_interrupt()   # 直前の割り込み状態で ack が抑制されないように
            speaker.say(text)
            speaker.wait_done()         # 返事を鳴らし切ってから録音へ（直後にバッファを drain）


def _drain(recorder):
    """PvRecorder のバッファに溜まった古い音声（ack の自己エコー等）を捨て、
    『今』から録音できるようにする。バックログは即返り、追いつくと read() が
    約1フレーム分ブロックする。その差で検出して止める。"""
    half = (recorder.frame_length / SAMPLE_RATE) * 0.5
    for _ in range(2000):
        t0 = time.time()
        recorder.read()
        if time.time() - t0 > half:
            break


# ───────────────────────────────── バージイン監視 ─────────────────────────────────
class BargeInMonitor(threading.Thread):
    """ターン中だけ走り、再生中のマイクを監視してユーザーの割り込みを検出する。
      mode="wakeword": 再生中にもう一度「ずんだもん」で割り込み（エコーに強い・既定）
      mode="energy"  : 一定以上の声量で割り込み（自然だがヘッドセット推奨）
      mode="off"     : 割り込み無効（フレームは捨て読みしてバッファ溢れを防ぐ）
    検出すると speaker.interrupt() で再生を止め、result に結果を残す。
      result: None=割り込み無し / "wake"=「ずんだもん」 / np.ndarray=拾った発話の先頭
    """

    def __init__(self, recorder, wake, speaker, mode):
        super().__init__(daemon=True)
        self.recorder = recorder
        self.wake = wake
        self.speaker = speaker
        self.mode = mode
        # 注意: 属性名 `_stop` は threading.Thread._stop() メソッドを潰し、
        # join() 時に `TypeError: 'Event' object is not callable` を招くため使わない。
        self._stop_event = threading.Event()
        self.triggered = threading.Event()
        self.result = None

    def run(self):
        try:
            self._run()
        except Exception as e:
            # スレッド内の例外は通常表に出ない。バージインが効かない原因を可視化する。
            import traceback
            print(f"[barge-in] 監視スレッドが例外終了: {e}", file=sys.stderr)
            traceback.print_exc()

    def _run(self):
        debug = getattr(C, "BARGE_IN_DEBUG", True)
        if debug:
            print(f"[barge-in] 監視開始 mode={self.mode}")
        if self.mode == "off":
            while not self._stop_event.is_set():
                self.recorder.read()  # 捨て読み（バッファ溢れ防止）
            return

        frame_sec = FRAME_LENGTH / SAMPLE_RATE
        need = max(1, int(C.BARGE_IN_MIN_SEC / frame_sec))
        run_len = 0
        seed = []
        frames_read = 0
        while not self._stop_event.is_set():
            pcm = self.recorder.read()
            frames_read += 1
            if debug and frames_read % 25 == 0:   # 約2秒ごとに生存確認
                print(f"[barge-in] 監視中… {frames_read} フレーム読込（mode={self.mode}）")
            if self.mode == "wakeword":
                if self.wake.process(pcm):
                    self._fire("wake")
                    return
            else:  # energy
                arr = np.array(pcm, dtype=np.int16)
                rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
                if rms >= C.BARGE_IN_RMS:
                    run_len += 1
                    seed.append(arr)
                    if run_len >= need:
                        self._fire(np.concatenate(seed))
                        return
                else:
                    run_len = 0
                    seed = []
        if debug:
            print(f"[barge-in] 監視終了（{frames_read} フレーム）")

    def _fire(self, result):
        self.result = result
        self.triggered.set()
        self.wake.reset()
        self.speaker.interrupt()

    def stop(self):
        self._stop_event.set()


# ───────────────────────────────── 会話 LLM（llama.cpp / OpenAI 互換） ─────────────────────────────────
def llm_stream(messages):
    """OpenAI 互換 /chat/completions を stream で叩き、トークン(delta)を yield する。"""
    payload = {
        "model": C.LLAMA_MODEL,
        "messages": messages,
        "temperature": C.LLAMA_TEMPERATURE,
        "max_tokens": C.LLAMA_MAX_TOKENS,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {C.LLAMA_API_KEY}"}
    with requests.post(
        f"{C.LLAMA_BASE_URL}/chat/completions",
        json=payload,
        headers=headers,
        stream=True,
        timeout=120,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


# ───────────────────────────────── opencode serve（作業委譲） ─────────────────────────────────
class OpenCode:
    def __init__(self):
        self.session_id = None

    def _ensure_session(self):
        if self.session_id:
            return
        r = requests.post(f"{C.OPENCODE_BASE_URL}/session", json={}, timeout=30)
        r.raise_for_status()
        self.session_id = r.json()["id"]

    def run(self, instruction: str) -> str:
        """opencode に作業を投げ、応答テキストを返す（同期）。"""
        self._ensure_session()
        body = {
            "providerID": C.OPENCODE_PROVIDER_ID,
            "modelID": C.OPENCODE_MODEL_ID,
            "parts": [{"type": "text", "text": instruction}],
        }
        r = requests.post(
            f"{C.OPENCODE_BASE_URL}/session/{self.session_id}/message",
            json=body,
            timeout=600,
        )
        r.raise_for_status()
        return _extract_text(r.json())


def _extract_text(obj) -> str:
    """opencode の応答 JSON から text パートをかき集める（版差に対する保険つき）。"""
    texts = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == "text" and isinstance(x.get("text"), str):
                texts.append(x["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return "\n".join(t for t in texts if t.strip()).strip() or "(応答が空でした)"


# ───────────────────────────────── ターン処理 ─────────────────────────────────
def handle_turn(user_text, messages, speaker: Speaker, opencode: OpenCode, monitor):
    messages.append({"role": "user", "content": user_text})

    buffer = ""          # 生成テキスト全体
    sent_buf = ""        # TTS にまだ流していない端数
    decided = False      # 雑談/タスクの判定が済んだか
    is_task = False

    for delta in llm_stream(messages):
        if monitor.triggered.is_set():   # バージインで中断
            break
        buffer += delta

        # 先頭を覗いて「雑談」か「[[TASK]]」かを一度だけ判定
        if not decided:
            head = buffer.lstrip()
            if len(head) < len(_TASK_SENTINEL):
                continue  # まだ判定に足る文字が来ていない
            decided = True
            is_task = head.startswith(_TASK_SENTINEL)
            if not is_task:
                # ここまでの buffer は現 delta を既に含むので、そのまま発話対象に
                # 引き継いで flush する。下の sent_buf += delta は通さない（二重発話防止）。
                sent_buf = _flush_sentences(buffer, speaker)
            continue

        if is_task:
            continue  # タスク時は喋らず全文を貯める

        # 雑談: 文が完成するたび TTS キューへ（生成と再生がパイプライン）
        sent_buf += delta
        sent_buf = _flush_sentences(sent_buf, speaker)

    if monitor.triggered.is_set():
        if buffer.strip():
            print(f"ずんだもん: {buffer.strip()}（割り込みで中断）")
        messages.append({"role": "assistant", "content": buffer})
        return

    if is_task:
        instruction = buffer.lstrip()[len(_TASK_SENTINEL):].strip()
        messages.append({"role": "assistant", "content": buffer})
        print(f"  → opencode へ委譲: {instruction}")
        speaker.say("わかりました、やってみますね")  # 待ち時間を隠すフィラー
        try:
            result = opencode.run(instruction)
        except Exception as e:
            speaker.say("作業中にエラーが出ちゃいました")
            print(f"[opencode error] {e}", file=sys.stderr)
            speaker.wait_done()
            return
        if monitor.triggered.is_set():   # 作業中に割り込まれたら要約しない
            return
        # 結果を LLM に渡して音声向けに要約させる
        messages.append({"role": "user",
                         "content": f"作業結果:\n{result}\n\n{C.SUMMARIZE_PROMPT}"})
        summary = ""
        sb = ""
        for delta in llm_stream(messages):
            if monitor.triggered.is_set():
                break
            summary += delta
            sb += delta
            sb = _flush_sentences(sb, speaker)
        if sb.strip() and not monitor.triggered.is_set():
            speaker.say(sb)
        if summary.strip():
            print(f"ずんだもん: {summary.strip()}")
        messages.append({"role": "assistant", "content": summary})
    else:
        if sent_buf.strip():
            speaker.say(sent_buf)   # 端数を流し切る
        if buffer.strip():
            print(f"ずんだもん: {buffer.strip()}")
        messages.append({"role": "assistant", "content": buffer})

    speaker.wait_done()


def _flush_sentences(buf: str, speaker: Speaker) -> str:
    """buf の中の完成した文を TTS に流し、未完の端数を返す。"""
    while True:
        m = _SENT_BOUNDARY.search(buf)
        if not m:
            return buf
        end = m.end()
        sentence = buf[:end].strip()
        if sentence:
            speaker.say(sentence)
        buf = buf[end:]


# ───────────────────────────────── 録音（VAD） ─────────────────────────────────
def record_utterance(recorder: PvRecorder, seed=None,
                     assume_started=False) -> np.ndarray | None:
    """ウェイクワード後（またはバージイン後）の発話を、末尾無音まで録る。
    seed: 既に拾い済みの発話先頭（int16 ndarray）。バージイン継続時に前置きする。
    assume_started: True なら発話開始済みとして扱い、開始待ちタイムアウトを無効化する。"""
    frame_sec = recorder.frame_length / SAMPLE_RATE
    hang_frames = int(C.SILENCE_HANG_SEC / frame_sec)
    start_timeout_frames = int(C.START_TIMEOUT_SEC / frame_sec)
    max_frames = int(C.MAX_UTTERANCE_SEC / frame_sec)

    frames = []
    if seed is not None and len(seed) > 0:
        frames.append(np.asarray(seed, dtype=np.int16))
    else:
        _drain(recorder)   # ack の自己エコー/バックログを捨て、今から録る
    silence_run = 0
    started = assume_started or (seed is not None)
    n = 0
    while True:
        pcm = recorder.read()
        arr = np.array(pcm, dtype=np.int16)
        frames.append(arr)
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        n += 1

        if rms >= C.SILENCE_RMS:
            started = True
            silence_run = 0
        else:
            silence_run += 1

        if not started:
            if n >= start_timeout_frames:
                return None  # 何も喋らなかった
            continue
        if silence_run >= hang_frames:
            break            # 末尾の無音 → 発話終了
        if n >= max_frames:
            break            # 上限

    audio = np.concatenate(frames).astype(np.float32) / 32768.0
    return audio


# ───────────────────────────────── メインループ ─────────────────────────────────
def main():
    print("モデル読み込み中…")
    wake = WakeWord()
    # CUDA DLL は import 前に _register_cuda_dll_dirs() 済み（モジュール冒頭）
    try:
        whisper = WhisperModel(C.WHISPER_MODEL, device=C.WHISPER_DEVICE,
                               compute_type=C.WHISPER_COMPUTE)
    except Exception as e:
        if C.WHISPER_DEVICE != "cpu":
            print(f"[警告] CUDA で Whisper を初期化できませんでした（{e}）。CPU にフォールバックします。\n"
                  "      GPU を使うには: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
            whisper = WhisperModel(C.WHISPER_MODEL, device="cpu", compute_type="int8")
        else:
            raise
    recorder = PvRecorder(frame_length=FRAME_LENGTH,
                          device_index=C.INPUT_DEVICE_INDEX)
    speaker = Speaker()
    opencode = OpenCode()
    messages = [{"role": "system", "content": C.SYSTEM_PROMPT}]

    recorder.start()  # マイクは終始動かしっぱなし（バージイン監視のため）
    print('準備完了。「ずんだもん」と話しかけてください（Ctrl+C で終了）。')
    try:
        while True:
            pcm = recorder.read()
            if not wake.process(pcm):
                continue
            wake.reset()  # 検出直後にバッファを消して連続誤発火を防ぐ

            # ── ウェイクワード検出 → ターン連鎖（バージインで継続） ──
            seed = None          # 直前のバージインで拾った発話先頭
            beep_next = True     # この発話の前にビープを鳴らすか
            while True:
                if beep_next:
                    acknowledge(speaker)
                audio = record_utterance(recorder, seed=seed,
                                         assume_started=(seed is not None))
                seed = None
                if audio is None or len(audio) < SAMPLE_RATE * 0.3:
                    print("（聞き取れませんでした）")
                    break

                t0 = time.time()
                segments, _ = whisper.transcribe(
                    audio, language=C.WHISPER_LANGUAGE,
                    beam_size=getattr(C, "WHISPER_BEAM_SIZE", 1),
                    vad_filter=getattr(C, "WHISPER_VAD_FILTER", False),
                )
                user_text = "".join(s.text for s in segments).strip()
                if getattr(C, "STT_TIMING", True):
                    dur = len(audio) / SAMPLE_RATE
                    print(f"[stt] {time.time() - t0:.2f}s（音声 {dur:.1f}s）")
                if not user_text:
                    print("（無音）")
                    break

                print(f"あなた: {user_text}")
                speaker.clear_interrupt()
                monitor = BargeInMonitor(recorder, wake, speaker, C.BARGE_IN_MODE)
                monitor.start()
                try:
                    handle_turn(user_text, messages, speaker, opencode, monitor)
                except Exception as e:
                    print(f"[turn error] {e}", file=sys.stderr)
                    speaker.say("ごめんなさい、うまく処理できませんでした")
                    speaker.wait_done()
                finally:
                    monitor.stop()
                    monitor.join()  # マイクを次に読む前に監視スレッドを止める

                r = monitor.result
                if isinstance(r, np.ndarray):       # energy 割り込み: 続きを録る
                    print("（割り込みを検出）")
                    seed = r
                    beep_next = False
                    continue
                if r == "wake":                     # 「ずんだもん」で割り込み: 録り直し
                    print("（「ずんだもん」で割り込み）")
                    beep_next = True
                    continue
                break                               # 通常終了 → ウェイクワード待ちへ
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        recorder.delete()


if __name__ == "__main__":
    main()
