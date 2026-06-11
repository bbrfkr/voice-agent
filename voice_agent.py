"""
低遅延・音声エージェント（Windows クライアント）

フロー:
  「ずんだもん」(openWakeWord) → 録音(VAD) → faster-whisper(STT) →
  llama.cpp(会話LLM, ストリーミング)
     ├ 通常会話     : 句点ごとに VOICEVOX で逐次再生（生成と再生をパイプライン）
     └ [[TASK]] 検出: opencode serve に作業委譲 → 結果を LLM が音声で要約

  ログモード（「ずんだもん」→「ログモード」で ON）:
     ウェイクワード不要の連続リスニングに切り替わり、全発話を LLM・TTS を挟まず
     STT 結果のまま専用 Discord Webhook へ直送する（音声メモ・口述筆記用）。
     解除は「ずんだもん」→「ログモード終了」（切替だけはモード中もウェイクワード経由。
     メモ本文に解除フレーズが入っても誤解除しない）。

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
import unicodedata
from collections import deque


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
# 早出し用の緩い区切り（読点を含む）。応答の1文目だけここで先に喋り出す。
_SOFT_BOUNDARY = re.compile(r"[、，,。．！？!?\n]")
_TASK_SENTINEL = "[[TASK]]"


# ───────────────────────────────── ウェイクワード（openWakeWord, OSS） ─────────────────────────────────
class WakeWord:
    """openWakeWord による検出器。process(pcm)->bool で 1 フレーム判定する。
    ウェイクワード待受と、バージイン(wakeword モード)で共用する。"""

    def __init__(self):
        # 共有の特徴抽出モデル（melspectrogram / embedding / silero VAD）を必要なら取得
        try:
            openwakeword.utils.download_models(model_names=[])
        except Exception:
            pass
        kwargs = dict(wakeword_models=[C.OWW_MODEL_PATH],
                      inference_framework=C.OWW_FRAMEWORK)
        # 誤発火対策①: openWakeWord 内蔵の Silero VAD で「人の声」以外をゲートする
        self.vad_threshold = getattr(C, "OWW_VAD_THRESHOLD", 0.0)
        if self.vad_threshold > 0:
            try:
                self.model = OWWModel(vad_threshold=self.vad_threshold, **kwargs)
            except Exception as e:
                print(f"[wake] VAD 付き初期化に失敗、VAD なしで続行: {e}", file=sys.stderr)
                self.vad_threshold = 0.0
                self.model = OWWModel(**kwargs)
        else:
            self.model = OWWModel(**kwargs)
        self.threshold = C.OWW_THRESHOLD
        # 誤発火対策②: 直近約1秒の入力 RMS がこの値未満なら発火を無視（0 で無効）
        self.min_rms = getattr(C, "WAKE_MIN_RMS", 0)
        self._rms_hist = deque(maxlen=12)   # 80ms × 12 ≒ 直近1秒
        # OWW_DEBUG=True で、声に対する検出スコアを表示（閾値調整・感度確認用）。
        # 既定 True（診断中）。安定したら config.py に OWW_DEBUG=False を足す。
        self.debug = getattr(C, "OWW_DEBUG", True)
        self.last_score = 0.0

    def process(self, pcm) -> bool:
        arr = np.asarray(pcm, dtype=np.int16)
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        self._rms_hist.append(rms)
        scores = self.model.predict(arr)
        mx = max(scores.values()) if scores else 0.0
        self.last_score = float(mx)
        hit = mx >= self.threshold
        loud = int(max(self._rms_hist))
        if hit and self.min_rms > 0 and loud < self.min_rms:
            if self.debug:
                print(f"[wake] score={mx:.2f} だが直近RMS {loud} < 下限 {self.min_rms} のため無視")
            return False
        # 0.1 以上に上がった瞬間だけ出す（無音時の 0.00 連発を避ける）
        if self.debug and mx >= 0.1:
            mark = "  ← 発火！" if hit else ""
            print(f"[wake] score={mx:.2f} rms={loud} (閾値 {self.threshold}){mark}")
        return hit

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
        self._anchor = None   # ユーザー発話完了時刻。次に音が鳴る直前に総遅延を表示する
        self._stream = None   # 再生専用 OutputStream（再生スレッドだけが触る）
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def set_anchor(self, t: float | None):
        """ユーザーの発話完了時刻を覚える。次の再生開始時に
        『発話完了→応答音声』のトータル遅延を一度だけログする。"""
        self._anchor = t

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
        """再生を即停止し、未再生のキューを捨てる（バージイン用）。
        注意: ここで sd.stop() 等の PortAudio 呼び出しはしない。再生スレッドの
        sd.play()/sd.wait() と別スレッドの sd.stop() が重なると PortAudio 内部で
        デッドロックすることがある（バージイン時に全体がスタックする既知症状）。
        停止は _play() のチャンクループがフラグを見て自前で行う（〜0.1s で反応）。"""
        self._interrupted = True
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
                    if self._anchor is not None:
                        print(f"[total] 発話完了→応答音声 {time.monotonic() - self._anchor:.2f}s")
                        self._anchor = None
                    self._play(data, sr)
            except Exception as e:
                print(f"[TTS error] {e}", file=sys.stderr)
            finally:
                self.q.task_done()

    def _play(self, data, sr):
        """専用 OutputStream にチャンクで書き込み、合間に割り込みフラグを確認する。
        再生に関わる PortAudio 呼び出しをこのスレッドだけに閉じ込めるのが目的
        （sd.play/sd.stop のスレッド間競合によるデッドロック回避）。
        書き終えたら stop() で必ず止める。流しっぱなしにすると無音アイドル中に
        underrun を撒き散らし、PulseAudio ごと不安定になってマイク入力まで死ぬ。"""
        channels = data.shape[1] if data.ndim > 1 else 1
        st = self._stream
        if st is None or st.samplerate != sr or st.channels != channels:
            if st is not None:
                st.close()
            st = self._stream = sd.OutputStream(samplerate=sr, channels=channels,
                                                dtype="float32")
        if st.stopped:
            st.start()
        step = max(256, int(sr * 0.05))   # 約50msごとにフラグを見る
        for i in range(0, len(data), step):
            if self._interrupted:
                st.abort()   # バッファに残った音も捨てて即黙る（同一スレッドなので安全）
                return
            st.write(np.ascontiguousarray(data[i:i + step]))
        st.stop()   # 書いた分を鳴らし切ってから停止（アイドル中の underrun 防止）

    def _synth(self, text: str):
        # 1) audio_query 2) synthesis の2段（VOICEVOX 標準）
        _t0 = time.monotonic()
        q = requests.post(
            f"{C.VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": C.VOICEVOX_SPEAKER},
            timeout=30,
        )
        q.raise_for_status()
        query = q.json()
        query["speedScale"] = C.VOICEVOX_SPEED
        query["volumeScale"] = getattr(C, "VOICEVOX_VOLUME", 1.0)
        r = requests.post(
            f"{C.VOICEVOX_URL}/synthesis",
            params={"speaker": C.VOICEVOX_SPEAKER},
            data=json.dumps(query),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        if getattr(C, "TURN_TIMING", True):
            print(f"[tts] 合成 {time.monotonic() - _t0:.2f}s（{len(text)}字）")
        return r.content


# ───────────────────────────────── 会話ログ（Discord Webhook） ─────────────────────────────────
class DiscordLogger:
    """会話ログを Discord Webhook へ POST する。Speaker と同様の
    キュー + 別スレッドの投げ捨て式で、送信時間をターンのレイテンシに乗せない。
    送信失敗は stderr に出すだけで会話は止めない。
    「あなた」と AI で別の Webhook URL（DISCORD_WEBHOOK_URL_USER / _AI）を使うと、
    Discord 側で投稿者（名前・アイコン）が分かれて読みやすい。片方しか設定されて
    いなければ両方そちらへ送り、発話者名を本文に前置して区別する。両方空なら無効。
    ログモード（STT 直送）は会話ログとは別の Webhook（DISCORD_WEBHOOK_URL_LOGMODE）
    へ送る。送信ワーカーは全 Webhook で共用する。"""

    _LIMIT = 1900   # Discord の content 上限 2000 字への安全マージン

    def __init__(self):
        user_url = getattr(C, "DISCORD_WEBHOOK_URL_USER", "")
        ai_url = getattr(C, "DISCORD_WEBHOOK_URL_AI", "")
        log_url = getattr(C, "DISCORD_WEBHOOK_URL_LOGMODE", "")
        self._urls = {"user": user_url or ai_url, "ai": ai_url or user_url,
                      "log": log_url}
        self._shared = not (user_url and ai_url)   # URL 共用時は発話者名を前置
        self.enabled = bool(user_url or ai_url)
        self.log_enabled = bool(log_url)
        self.q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        if self.enabled or self.log_enabled:
            threading.Thread(target=self._run, daemon=True).start()
        if self.enabled:
            print("[discord] 会話ログ送信を有効化"
                  + ("（単一 Webhook・発話者名を前置）" if self._shared else "（あなた/AI 別 Webhook）"))
        if self.log_enabled:
            print("[discord] ログモードの送信先を有効化")

    def user(self, text: str):
        self._post("user", text)

    def ai(self, text: str):
        self._post("ai", text)

    def log(self, text: str):
        """ログモード: STT 結果をそのまま専用 Webhook へ送る（発話者名は付けない）。"""
        self._post("log", text)

    def _post(self, role: str, text: str):
        text = (text or "").strip()
        url = self._urls.get(role)
        if not url or not text:
            return
        if role != "log" and self._shared:
            name = "あなた" if role == "user" else "ずんだもん"
            text = f"**{name}**: {text}"
        for i in range(0, len(text), self._LIMIT):
            self.q.put((url, text[i:i + self._LIMIT]))

    def _run(self):
        while True:
            url, content = self.q.get()
            try:
                r = requests.post(url, json={"content": content}, timeout=10)
                if r.status_code == 429:   # レート制限: 指定秒だけ待って1回だけ再送
                    time.sleep(float(r.headers.get("Retry-After", "1")) + 0.5)
                    r = requests.post(url, json={"content": content}, timeout=10)
                r.raise_for_status()
            except Exception as e:
                print(f"[discord] 送信失敗（無視）: {e}", file=sys.stderr)
            finally:
                self.q.task_done()


# ───────────────────────────────── STT（faster-whisper） ─────────────────────────────────
def _transcribe(whisper, audio) -> str:
    """音声を文字起こしして結合テキストを返す（STT_TIMING で所要を表示）。"""
    t0 = time.monotonic()
    segments, _ = whisper.transcribe(
        audio, language=C.WHISPER_LANGUAGE,
        beam_size=getattr(C, "WHISPER_BEAM_SIZE", 1),
        vad_filter=getattr(C, "WHISPER_VAD_FILTER", False),
    )
    text = "".join(s.text for s in segments).strip()
    if getattr(C, "STT_TIMING", True):
        dur = len(audio) / SAMPLE_RATE
        print(f"[stt] {time.monotonic() - t0:.2f}s（音声 {dur:.1f}s）")
    return text


# ───────────────────────────────── ログモード（STT → Discord 直送） ─────────────────────────────────
def _normalize_command(text: str) -> str:
    """発話コマンド照合用の正規化。STT の表記ゆれ（「ログ モード」「ろぐもーど。」等）を
    吸収するため、NFKC → 空白・記号の除去 → ひらがな→カタカナ統一 → 英字小文字化を行う。"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^ぁ-ゖァ-ヶー一-龠a-zA-Z0-9]", "", text)
    text = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)
    return text.lower()


_LOG_ON_CMDS = {_normalize_command(s) for s in C.LOG_MODE_ON_COMMANDS.split(",") if s.strip()}
_LOG_OFF_CMDS = {_normalize_command(s) for s in C.LOG_MODE_OFF_COMMANDS.split(",") if s.strip()}


def _match_log_command(text: str) -> str | None:
    """発話がログモードの切替コマンドなら "on"/"off"、違えば None を返す。
    誤発火対策として、正規化後の発話**全体**が同義語に完全一致したときだけ反応する
    （会話文中に「ログ」が出ただけでは切り替えない）。
    ログモード中は全発話が Discord 直送になるため、解除側を先に判定する。"""
    t = _normalize_command(text)
    if t in _LOG_OFF_CMDS:
        return "off"
    if t in _LOG_ON_CMDS:
        return "on"
    return None


def handle_log_turn(cmd: str, speaker: Speaker, dlog: DiscordLogger) -> bool:
    """通常会話中にウェイクワード経由で受けた切替コマンドを処理し、新しいモード状態を
    返す。TTS を使わないモードで現在状態が見えないため、必ず 1 回読み上げて
    フィードバックする。True を返すと run_log_mode の連続リスニングへ入る。"""
    speaker.clear_interrupt()
    if cmd == "off":
        speaker.say("ログモードはもともとオフです")
        speaker.wait_done()
        return False
    if not dlog.log_enabled:
        speaker.say("ログモードの送信先が設定されていないので、オンにできません")
        speaker.wait_done()
        print("[logmode] DISCORD_WEBHOOK_URL_LOGMODE 未設定のため ON にできません",
              file=sys.stderr)
        return False
    speaker.say("ログモードがオンになりました")
    speaker.wait_done()
    print("[logmode] ON")
    return True


def record_log_utterance(recorder, wake: "WakeWord", drain: bool = False):
    """ログモード用の録音。発話を末尾無音まで録りつつ、各フレームをウェイクワード
    検出にも通す（BargeInMonitor と同じ並走パターン）。「ずんだもん」が発火したら
    録音中でも即打ち切る（言いかけの音声は解除操作とみなして捨てる）。
    連続リスニングなので開始タイムアウトは設けず、喋り出すまで待ち続ける。
    drain=True で直前の TTS（切替フィードバック等）の自己エコーを先に捨てる。
    返り値: (音声 or None, ウェイクワードが発火したか)。"""
    frame_sec = FRAME_LENGTH / SAMPLE_RATE
    hang_frames = int(C.SILENCE_HANG_SEC / frame_sec)
    max_frames = int(C.MAX_UTTERANCE_SEC / frame_sec)
    if drain:
        _drain(recorder)
    lead = deque(maxlen=max(1, int(0.4 / frame_sec)))   # 発話頭の取りこぼし防止の前置き
    frames = []
    silence_run = 0
    started = False
    while True:
        pcm = recorder.read()
        if wake.process(pcm):
            return None, True
        arr = np.array(pcm, dtype=np.int16)
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        if not started:
            lead.append(arr)
            if rms >= C.SILENCE_RMS:
                started = True
                frames = list(lead)
            continue
        frames.append(arr)
        if rms >= C.SILENCE_RMS:
            silence_run = 0
        else:
            silence_run += 1
            if silence_run >= hang_frames:
                break
        if len(frames) >= max_frames:
            break
    return np.concatenate(frames).astype(np.float32) / 32768.0, False


def run_log_mode(recorder, wake: "WakeWord", whisper, speaker: Speaker,
                 dlog: DiscordLogger):
    """ログモードの連続リスニング本体。ウェイクワードなしで全発話を STT →
    専用 Webhook へ直送し続け、「ずんだもん」→ 解除コマンドで OFF になったら返る。
    解除をウェイクワード経由に限定することで、メモ本文に「ログモード終了」等が
    含まれていても誤解除しない。マイクを再初期化することがあるため recorder を返す。"""
    print('[logmode] 連続リスニング中（発話はそのまま Discord へ）。'
          '解除は「ずんだもん」→「ログモード終了」')
    drain = True   # 直前に ON フィードバックの TTS が鳴っている
    while True:
        try:
            audio, woke = record_log_utterance(recorder, wake, drain=drain)
        except OSError as e:
            recorder = _recover_recorder(recorder, e)
            drain = False
            continue
        drain = False
        if woke:
            # コマンド窓: 次の発話が解除コマンドなら OFF。それ以外は送信せず聞き続ける
            wake.reset()
            acknowledge(speaker)
            try:
                cmd_audio, _ = record_utterance(recorder)
            except OSError as e:
                recorder = _recover_recorder(recorder, e)
                continue
            cmd_text = ""
            if cmd_audio is not None and len(cmd_audio) >= SAMPLE_RATE * 0.3:
                cmd_text = _transcribe(whisper, cmd_audio)
            if cmd_text:
                print(f"あなた: {cmd_text}")
            speaker.clear_interrupt()
            if cmd_text and _match_log_command(cmd_text) == "off":
                speaker.say("ログモードがオフになりました")
                speaker.wait_done()
                print("[logmode] OFF")
                return recorder
            speaker.say("ログモードのままです")
            speaker.wait_done()
            drain = True   # フィードバックの自己エコーを次の録音前に捨てる
            continue
        if audio is None or len(audio) < SAMPLE_RATE * 0.3:
            continue
        text = _transcribe(whisper, audio)
        if not text:
            continue
        print(f"あなた: {text}")
        dlog.log(text)
        print("[logmode] Discord へ直送")


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
        t0 = time.monotonic()
        recorder.read()
        if time.monotonic() - t0 > half:
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
    if getattr(C, "LLAMA_DISABLE_THINKING", False):
        # thinking を切らないと、思考トークンを吐き終わるまで content が来ず
        # 初回の音出しがまるごと遅れる（実測で TTFT 2.4s → 0.7s）。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
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
def _trim_history(messages):
    """system(先頭) + 直近 LLAMA_MAX_HISTORY 件だけ残す。長時間の会話で
    プロンプトが伸び続けて TTFT が悪化するのを防ぐ。タスクの「作業結果」全文も
    ここで自然に押し出される。先頭が assistant 始まりにならないよう調整する。"""
    keep = getattr(C, "LLAMA_MAX_HISTORY", 20)
    if keep <= 0 or len(messages) - 1 <= keep:
        return
    del messages[1:len(messages) - keep]
    while len(messages) > 1 and messages[1]["role"] != "user":
        del messages[1]


def handle_turn(user_text, messages, speaker: Speaker, opencode: OpenCode, monitor,
                dlog: DiscordLogger):
    messages.append({"role": "user", "content": user_text})
    _trim_history(messages)

    buffer = ""          # 生成テキスト全体
    sent_buf = ""        # TTS にまだ流していない端数
    decided = False      # 雑談/タスクの判定が済んだか
    is_task = False
    first_done = False   # 1文目を喋り出したか（早出し制御）

    t0 = time.monotonic()             # LLM 呼び出し開始（STT 完了直後）
    t_first_token = None         # 最初のトークンが来た時刻
    t_first_say = None           # 最初の音をキューに積んだ時刻

    for delta in llm_stream(messages):
        if monitor.triggered.is_set():   # バージインで中断
            break
        if t_first_token is None:
            t_first_token = time.monotonic()
        buffer += delta

        # 先頭を覗いて「雑談」か「[[TASK]]」かを一度だけ判定
        if not decided:
            head = buffer.lstrip()
            if len(head) < len(_TASK_SENTINEL):
                continue  # まだ判定に足る文字が来ていない
            decided = True
            is_task = head.startswith(_TASK_SENTINEL)
            if is_task:
                continue
            # 雑談確定。ここまでの buffer は現 delta を既に含むので、そのまま発話対象へ。
            sent_buf = buffer
        elif is_task:
            continue  # タスク時は喋らず全文を貯める
        else:
            sent_buf += delta

        # 雑談: 1文目は読点/文字数でも早出しし、初回の音出しを縮める。
        # 2文目以降は文単位（1文目を喋る裏で生成されるので無音にならない）。
        if not first_done:
            sent_buf, flushed = _flush_first(sent_buf, speaker)
            if not flushed:
                continue
            first_done = True
            if t_first_say is None:
                t_first_say = time.monotonic()
        sent_buf = _flush_sentences(sent_buf, speaker)

    if getattr(C, "TURN_TIMING", True) and t_first_token is not None:
        ttft = t_first_token - t0
        if t_first_say is not None:
            print(f"[turn] TTFT {ttft:.2f}s / 初音 {t_first_say - t0:.2f}s")
        else:
            print(f"[turn] TTFT {ttft:.2f}s（音声出力なし）")

    if monitor.triggered.is_set():
        if buffer.strip():
            print(f"ずんだもん: {buffer.strip()}（割り込みで中断）")
            dlog.ai(f"{buffer.strip()}（割り込みで中断）")
        messages.append({"role": "assistant", "content": buffer})
        return

    if is_task:
        instruction = buffer.lstrip()[len(_TASK_SENTINEL):].strip()
        messages.append({"role": "assistant", "content": buffer})
        print(f"  → opencode へ委譲: {instruction}")
        dlog.ai(f"🛠️ 作業委譲: {instruction}")
        speaker.say("わかりました、やってみますね")  # 待ち時間を隠すフィラー
        try:
            result = opencode.run(instruction)
        except Exception as e:
            speaker.say("作業中にエラーが出ちゃいました")
            print(f"[opencode error] {e}", file=sys.stderr)
            dlog.ai(f"⚠️ 作業中にエラー: {e}")
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
            dlog.ai(summary)
        messages.append({"role": "assistant", "content": summary})
    else:
        if sent_buf.strip():
            speaker.say(sent_buf)   # 端数を流し切る
        if buffer.strip():
            print(f"ずんだもん: {buffer.strip()}")
            dlog.ai(buffer)
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


def _flush_first(buf: str, speaker: Speaker) -> tuple[str, bool]:
    """応答の1文目だけ、初回の音出しを早めるために緩い基準で TTS に流す。
      ・句点(。！？等)が来たら無条件で流す
      ・読点(、,)なら最小文字数を超えたとき流す（短すぎる細切れを避ける）
      ・どちらも来なくても上限文字数に達したら、そこで区切って流す
    早出しできたら (残り, True)、まだ流せないなら (buf, False) を返す。"""
    hard = _SENT_BOUNDARY.search(buf)
    soft = _SOFT_BOUNDARY.search(buf)
    if hard:
        end = hard.end()
    elif soft and soft.end() >= C.FIRST_FLUSH_MIN_CHARS:
        end = soft.end()
    elif len(buf) >= C.FIRST_FLUSH_MAX_CHARS:
        end = C.FIRST_FLUSH_MAX_CHARS
    else:
        return buf, False
    chunk = buf[:end].strip()
    if chunk:
        speaker.say(chunk)
        return buf[end:], True
    # 区切りはあったが中身が空白だけ → 区切りを捨てて継続（まだ喋っていない）
    return buf[end:], False


# ───────────────────────────────── 録音（VAD） ─────────────────────────────────
def record_utterance(recorder: PvRecorder, seed=None,
                     assume_started=False) -> tuple[np.ndarray | None, float | None]:
    """ウェイクワード後（またはバージイン後）の発話を、末尾無音まで録る。
    seed: 既に拾い済みの発話先頭（int16 ndarray）。バージイン継続時に前置きする。
    assume_started: True なら発話開始済みとして扱い、開始待ちタイムアウトを無効化する。
    返り値: (音声, 発話完了時刻)。発話完了時刻は無音判定の待ち時間を含まない
    『実際に喋り終わった瞬間』の monotonic 時刻（トータル遅延の計測起点）。"""
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
    rms_vals = []   # SILENCE_RMS の調整用に分布を出す
    while True:
        pcm = recorder.read()
        arr = np.array(pcm, dtype=np.int16)
        frames.append(arr)
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        rms_vals.append(rms)
        n += 1

        if rms >= C.SILENCE_RMS:
            started = True
            silence_run = 0
        else:
            silence_run += 1

        if not started:
            if n >= start_timeout_frames:
                return None, None  # 何も喋らなかった
            continue
        if silence_run >= hang_frames:
            # 末尾の無音 → 発話終了。実際に喋り終わったのは hang ぶん前
            speech_end = time.monotonic() - silence_run * frame_sec
            break
        if n >= max_frames:
            speech_end = time.monotonic()
            break            # 上限

    if getattr(C, "RMS_DEBUG", True) and rms_vals:
        voiced = [v for v in rms_vals if v >= C.SILENCE_RMS]
        silent = [v for v in rms_vals if v < C.SILENCE_RMS]
        v_med = int(np.median(voiced)) if voiced else 0
        v_max = int(max(voiced)) if voiced else 0
        s_med = int(np.median(silent)) if silent else 0
        print(f"[rms] 発話 中央値 {v_med} / 最大 {v_max}、"
              f"無音 中央値 {s_med}（閾値 SILENCE_RMS={C.SILENCE_RMS}）")

    audio = np.concatenate(frames).astype(np.float32) / 32768.0
    return audio, speech_end


# ───────────────────────────────── 起動時ウォームアップ ─────────────────────────────────
def _warmup(speaker: Speaker, messages, whisper):
    """初回の体感遅延を吸収する。VOICEVOX(初回合成 JIT)・LLM(接続/プロンプト前処理)・
    Whisper(CUDA カーネル初期化) を起動時に1回だけ温め、最初の発話から
    ウォーム並みの速さで応答できるようにする。失敗は無視（本番ループの妨げにしない）。"""
    print("ウォームアップ中…")
    t0 = time.monotonic()
    try:
        speaker._synth("あ")   # 捨て合成（再生はしない）
    except Exception as e:
        print(f"[warmup] VOICEVOX 失敗（無視）: {e}", file=sys.stderr)
    try:
        # 無音1秒を transcribe して CUDA カーネルを初期化（segments は遅延評価なので回し切る）
        segments, _ = whisper.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                         language=C.WHISPER_LANGUAGE)
        list(segments)
    except Exception as e:
        print(f"[warmup] Whisper 失敗（無視）: {e}", file=sys.stderr)
    try:
        # system prompt を投げてプロンプト前処理を温める。最初のトークンで即打ち切り。
        for _ in llm_stream(messages + [{"role": "user", "content": "ping"}]):
            break
    except Exception as e:
        print(f"[warmup] LLM 失敗（無視）: {e}", file=sys.stderr)
    print(f"ウォームアップ完了（{time.monotonic() - t0:.1f}s）")


# ───────────────────────────────── マイクの自己回復 ─────────────────────────────────
def _recover_recorder(recorder, err):
    """マイクが読めなくなったら作り直して復帰する。WSLg の PulseAudio は
    出力側の不調などで瞬断することがあり、その際 PvRecorder.read() が
    OSError を投げて戻らなくなるため、プロセスごと落とさず繋ぎ直す。"""
    print(f"[audio] マイク読み取りに失敗、再初期化します: {err}", file=sys.stderr)
    try:
        recorder.delete()
    except Exception:
        pass
    last = None
    for _ in range(5):
        time.sleep(1.0)
        try:
            r = PvRecorder(frame_length=FRAME_LENGTH,
                           device_index=C.INPUT_DEVICE_INDEX)
            r.start()
            print("[audio] マイクを再初期化しました")
            return r
        except Exception as e:
            last = e
    raise RuntimeError(f"マイクを再初期化できませんでした: {last}")


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
    dlog = DiscordLogger()
    messages = [{"role": "system", "content": C.SYSTEM_PROMPT}]

    if getattr(C, "WARMUP", True):
        _warmup(speaker, messages, whisper)

    recorder.start()  # マイクは終始動かしっぱなし（バージイン監視のため）
    log_mode = False  # ログモード。「ずんだもん」→「ログモード」で ON → 連続リスニング
    print('準備完了。「ずんだもん」と話しかけてください（Ctrl+C で終了）。')
    try:
        while True:
            # ── ログモード: OFF に戻るまで連続リスニング（この中でブロックする） ──
            if log_mode:
                recorder = run_log_mode(recorder, wake, whisper, speaker, dlog)
                log_mode = False
                continue

            try:
                pcm = recorder.read()
            except OSError as e:
                recorder = _recover_recorder(recorder, e)
                continue
            if not wake.process(pcm):
                continue
            wake.reset()  # 検出直後にバッファを消して連続誤発火を防ぐ

            # ── ウェイクワード検出 → ターン連鎖（バージインで継続） ──
            seed = None          # 直前のバージインで拾った発話先頭
            beep_next = True     # この発話の前にビープを鳴らすか
            while True:
                if beep_next:
                    acknowledge(speaker)
                try:
                    audio, t_speech_end = record_utterance(
                        recorder, seed=seed, assume_started=(seed is not None))
                except OSError as e:
                    recorder = _recover_recorder(recorder, e)
                    break   # このターンは諦めてウェイクワード待ちへ
                seed = None
                if audio is None or len(audio) < SAMPLE_RATE * 0.3:
                    print("（聞き取れませんでした）")
                    break

                user_text = _transcribe(whisper, audio)
                if not user_text:
                    print("（無音）")
                    break

                print(f"あなた: {user_text}")

                # ログモードへの切替コマンド（LLM・会話ログには流さない）
                cmd = _match_log_command(user_text)
                if cmd is not None:
                    log_mode = handle_log_turn(cmd, speaker, dlog)
                    break   # ターン終了 → ON なら外側の連続リスニングへ

                dlog.user(user_text)
                speaker.clear_interrupt()
                speaker.set_anchor(t_speech_end)  # 次の音出しで「発話完了→応答音声」を表示
                monitor = BargeInMonitor(recorder, wake, speaker, C.BARGE_IN_MODE)
                monitor.start()
                try:
                    handle_turn(user_text, messages, speaker, opencode, monitor, dlog)
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
