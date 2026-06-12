"""
Discord ボイスチャンネル常駐版・音声エージェント

ローカルのマイク/スピーカー（PulseAudio）の代わりに、Discord のボイスチャンネル（VC）へ
bot として常駐する。スマホ等、Discord app が動く端末ならどこからでも音声で呼び出せる。

フロー（音声 I/O 以外はローカル版 voice_agent.py と同じ）:
  VC のユーザー音声（Opus 48kHz, discord-ext-voice-recv で受信）
    → 16kHz モノラル化 → DiscordRecorder（PvRecorder 互換の仮想レコーダー）
    → 「ずんだもん」(openWakeWord) → 録音(VAD) → faster-whisper(STT)
    → llama.cpp(会話LLM) → VOICEVOX(TTS) → VC へ再生（discord.PCMAudio）

設計:
  - discord.py は asyncio ベースだが、既存の同期パイプラインはスレッドのまま動かし、
    音声だけキューで橋渡しする（voice_agent.py の関数群をそのまま import して使う）。
  - Discord は「誰かが話している間」しか音声パケットを送ってこないため、
    DiscordRecorder.read() はタイムアウト時にゼロフレーム（無音）を合成して返す。
    これが無いと既存の無音終端判定（SILENCE_HANG_SEC）やバージイン監視が進まない。
  - bot の再生音声は受信ストリームに混ざらない（エコー問題が消える）ので、
    BARGE_IN_MODE=energy（自由発話の割り込み）が安全に使える。
  - WAKE_MODE=always で、ウェイクワードなしの常時リッスン（発話即ターン）にもできる。

設定は config.py（.env）の Discord セクションを参照。導入手順は DISCORD.md。
"""

import asyncio
import io
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import soundfile as sf
import soxr
import discord
from discord.ext import voice_recv

import config as C
import voice_agent as va

# 注意: Discord は 2026-03 からボイスの E2EE（DAVE プロトコル）を必須化しており、
# 非対応クライアントは接続自体が拒否される（WebSocket close 4017）。受信側の DAVE 復号は
# PyPI 版 discord-ext-voice-recv では未対応のため、対応 PR を取り込んだ fork を commit 固定で
# 使っている（pyproject.toml の dependency group と wiki の判断記録を参照）。
from voice_agent import (
    SAMPLE_RATE, FRAME_LENGTH,
    WakeWord, Speaker, DiscordLogger, OpenCode, BargeInMonitor,
    handle_turn, handle_log_turn, run_log_mode, record_utterance, acknowledge,
    _match_log_command, _warmup,
)

DISCORD_SR = 48000      # Discord の音声は 48kHz / 16bit / ステレオ固定
DISCORD_FRAME_BYTES = 3840   # 20ms × 48kHz × 2ch × 2byte（discord.PCMAudio の読み出し単位）


# ───────────────────────────────── 仮想レコーダー（VC 受信 → 16kHz フレーム） ─────────────────────────────────
class DiscordRecorder:
    """PvRecorder 互換の仮想レコーダー。Sink から届く 48kHz ステレオ PCM を
    16kHz モノラルへ変換し、openWakeWord/whisper が期待する 1280 サンプル（80ms）
    フレームに組み直してキューへ積む。read() は既存コードの想定どおり
    『常に実時間でフレームが返る』ように、パケットが無い間はゼロフレームを合成する。"""

    frame_length = FRAME_LENGTH

    def __init__(self):
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=256)  # 80ms×256 ≒ 20秒
        self._buf = np.zeros(0, dtype=np.float32)
        self._rs = soxr.ResampleStream(DISCORD_SR, SAMPLE_RATE, 1, dtype="float32")
        self._lock = threading.Lock()
        self._silence = np.zeros(FRAME_LENGTH, dtype=np.int16)

    def feed(self, pcm48k_stereo: bytes):
        """受信スレッドから 20ms チャンクを受け取り、16k モノラル化して積む。"""
        arr = np.frombuffer(pcm48k_stereo, dtype=np.int16)
        mono = arr.reshape(-1, 2).astype(np.float32).mean(axis=1)
        with self._lock:
            down = self._rs.resample_chunk(mono)
            if len(down):
                self._buf = np.concatenate([self._buf, down])
            while len(self._buf) >= FRAME_LENGTH:
                frame = self._buf[:FRAME_LENGTH].astype(np.int16)
                self._buf = self._buf[FRAME_LENGTH:]
                try:
                    self._q.put_nowait(frame)
                except queue.Full:
                    # エージェント側が長く詰まったら古い方から捨てる（実時間を優先）
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._q.put_nowait(frame)
                    except queue.Full:
                        pass

    def read(self) -> np.ndarray:
        """次の 80ms フレームを返す。実フレームが届かなければ無音フレームを返す
        （Discord は発話中しかパケットを送らないため、無音は受信側で合成する）。"""
        try:
            return self._q.get(timeout=FRAME_LENGTH / SAMPLE_RATE)
        except queue.Empty:
            return self._silence

    # PvRecorder 互換の体裁（既存コードが呼ぶが、仮想レコーダーでは何もしない）
    def start(self):
        pass

    def stop(self):
        pass

    def delete(self):
        pass


class AgentSink(voice_recv.AudioSink):
    """VC のユーザー音声を DiscordRecorder へ流し込む受け口。
    ユーザーごとに別ストリームで届くため、bot 自身と対象外ユーザーはここで弾ける。
    ※ v1 の制約: 複数ユーザーの同時発話は 1 本のキューに混ざる（個人利用前提）。"""

    def __init__(self, recorder: DiscordRecorder, allowed_ids: set[int]):
        super().__init__()
        self.recorder = recorder
        self.allowed_ids = allowed_ids

    def wants_opus(self) -> bool:
        return False   # デコード済み PCM（48kHz/16bit/ステレオ）で受け取る

    def write(self, user, data):
        if user is None or getattr(user, "bot", False):
            return
        if self.allowed_ids and user.id not in self.allowed_ids:
            return
        self.recorder.feed(data.pcm)

    def cleanup(self):
        pass


# ───────────────────────────────── 仮想スピーカー（TTS → VC 再生） ─────────────────────────────────
def _to_discord_pcm(data: np.ndarray, sr: int) -> bytes:
    """float32 音声を Discord 送信形式（48kHz/16bit/ステレオ）の生 PCM に変換する。
    末尾は 20ms フレーム境界までゼロ詰めする（PCMAudio は端数フレームを捨てるため）。"""
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != DISCORD_SR:
        data = soxr.resample(data, sr, DISCORD_SR)
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1).tobytes()
    pad = -len(stereo) % DISCORD_FRAME_BYTES
    return stereo + b"\x00" * pad


class DiscordSpeaker(Speaker):
    """Speaker のキュー/ワーカー/割り込み機構はそのまま継承し、出力先だけ
    ローカルスピーカー(sounddevice) から Discord の VC に替える。"""

    def __init__(self, get_vc):
        self._get_vc = get_vc   # 再接続で VoiceClient が替わるため関数で受ける
        super().__init__()

    def _play(self, data, sr):
        vc = self._get_vc()
        if vc is None:
            print("[discord] VC 未接続のため再生をスキップ", file=sys.stderr)
            return
        # 再生口は VC ごとに1本。ビープ（_play_beep）等と重なったら空くのを少し待つ
        for _ in range(40):   # 最大2秒
            if not vc.is_playing() or self._interrupted:
                break
            time.sleep(0.05)
        if self._interrupted:
            return
        pcm = _to_discord_pcm(data, sr)
        source = discord.PCMAudio(io.BytesIO(pcm))
        done = threading.Event()
        try:
            vc.play(source, after=lambda _e: done.set())
        except discord.ClientException as e:
            print(f"[discord] 再生開始に失敗（無視）: {e}", file=sys.stderr)
            return
        # 約50msごとに割り込みフラグを確認（バージインで vc.stop() → after が発火して抜ける）。
        # 注意: VC が切断されると discord.py の再生スレッドは再接続待ちで無期限ブロックし、
        # after が永遠に呼ばれないことがある（パイプライン全体が凍る）。再生実時間+5秒の
        # デッドラインと「再接続で VoiceClient が替わった」検出で必ず抜ける。
        deadline = time.monotonic() + len(pcm) / (DISCORD_SR * 4) + 5.0
        while not done.wait(0.05):
            if self._interrupted or time.monotonic() > deadline or self._get_vc() is not vc:
                try:
                    vc.stop()
                except Exception:
                    pass
                if not self._interrupted:
                    print("[discord] 再生が完了しないため打ち切り（VC 切断/再接続の可能性）",
                          file=sys.stderr)
                return


def _make_discord_beep(get_vc):
    """voice_agent._play_beep（sounddevice 依存）の差し替え用: VC にビープを鳴らす。"""
    t = np.linspace(0, 0.12, int(DISCORD_SR * 0.12), endpoint=False)
    tone = _to_discord_pcm(0.2 * np.sin(2 * np.pi * 880 * t).astype(np.float32), DISCORD_SR)

    def beep():
        vc = get_vc()
        if vc is None:
            return
        done = threading.Event()
        try:
            vc.play(discord.PCMAudio(io.BytesIO(tone)), after=lambda _e: done.set())
        except discord.ClientException:
            return   # 何か再生中なら鳴らさない（ack より発話を優先）
        done.wait(0.5)

    return beep


# ───────────────────────────────── 発話音声の収集（学習データ採取用） ─────────────────────────────────
def _dump_utterance(audio: np.ndarray, text: str):
    """認識した発話を 16kHz モノラル wav として UTTERANCE_DUMP_DIR へ保存する。
    ウェイクワードモデルの追加学習用に、実環境（Discord 経由）の音声を集めるための
    デバッグ機能。ファイル名に STT 結果を含め、正例/負例の仕分けをしやすくする。
    失敗しても会話は止めない。"""
    try:
        os.makedirs(C.UTTERANCE_DUMP_DIR, exist_ok=True)
        label = re.sub(r"[^ぁ-ゖァ-ヶー一-龠a-zA-Z0-9]", "", text)[:24] or "unrecognized"
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{label}.wav"
        sf.write(os.path.join(C.UTTERANCE_DUMP_DIR, name), audio, SAMPLE_RATE,
                 subtype="PCM_16")
    except Exception as e:
        print(f"[dump] 保存失敗（無視）: {e}", file=sys.stderr)


def _install_dump_hook():
    """UTTERANCE_DUMP_DIR 設定時、STT を通る全発話（通常会話・ログモード・コマンド窓
    すべて）を保存するよう voice_agent._transcribe をラップする。収集はログモード中の
    発話も対象（ログモードは LLM/TTS の応答を挟まないため、連続録音に都合がよい）。"""
    orig = va._transcribe

    def transcribe_and_dump(whisper, audio):
        text = orig(whisper, audio)
        _dump_utterance(audio, text)
        return text

    va._transcribe = transcribe_and_dump
    print(f"[dump] 発話音声を {C.UTTERANCE_DUMP_DIR} へ保存します（学習データ採取モード）")


# ───────────────────────────────── エージェント本体（別スレッド・同期） ─────────────────────────────────
def agent_main(recorder: DiscordRecorder, get_vc):
    """voice_agent.main() のメインループ相当。音声 I/O を仮想レコーダー/スピーカーに
    差し替え、WAKE_MODE=always（ウェイクワードなしの常時リッスン）分岐を足した版。
    discord.py のイベントループとは独立した通常スレッドで動く。"""
    print("モデル読み込み中…")
    wake = WakeWord()
    try:
        whisper = va.WhisperModel(C.WHISPER_MODEL, device=C.WHISPER_DEVICE,
                                  compute_type=C.WHISPER_COMPUTE)
    except Exception as e:
        if C.WHISPER_DEVICE != "cpu":
            print(f"[警告] GPU で Whisper を初期化できませんでした（{e}）。CPU にフォールバックします。\n"
                  "      ROCm 構成では /dev/kfd・/dev/dri のマウントと RENDER_GID を確認してください。")
            whisper = va.WhisperModel(C.WHISPER_MODEL, device="cpu", compute_type="int8")
        else:
            raise

    speaker = DiscordSpeaker(get_vc)
    # 既存コードの音声出力・自己回復をこの環境向けに差し替える:
    #   _play_beep        : sounddevice 依存 → VC 再生版
    #   _recover_recorder : PvRecorder を作り直す処理だが、仮想レコーダーは OSError を
    #                       投げないので呼ばれない。万一に備えそのまま返すだけにする。
    va._play_beep = _make_discord_beep(get_vc)
    va._recover_recorder = lambda rec, err: rec
    if C.UTTERANCE_DUMP_DIR:
        _install_dump_hook()

    opencode = OpenCode()
    dlog = DiscordLogger()
    messages = [{"role": "system", "content": C.SYSTEM_PROMPT}]

    if getattr(C, "WARMUP", True):
        _warmup(speaker, messages, whisper)

    # ── 収集専用モード: 会話せず、全発話を録音 → STT → 保存だけする ──
    # ウェイクワード判定を一切通さないので、検出可否に関係なく全サンプルが集まる。
    # ログモードと違い「ずんだもん」で解除コマンド窓に入ることもない。
    if getattr(C, "COLLECT_ONLY", False) and C.UTTERANCE_DUMP_DIR:
        print("[collect] 収集専用モード（応答なし・全発話を保存）。終了は COLLECT_ONLY を"
              "外してコンテナ再起動")
        count = 0
        while True:
            pcm = recorder.read()
            arr = np.asarray(pcm, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            if rms < C.SILENCE_RMS:
                continue
            audio, _ = record_utterance(recorder, seed=arr, assume_started=True)
            if audio is None or len(audio) < SAMPLE_RATE * 0.3:
                continue
            text = va._transcribe(whisper, audio)   # dump フック経由で保存される
            count += 1
            print(f"[collect] {count} 件目（{len(audio)/SAMPLE_RATE:.1f}s）: {text}")

    wake_always = C.WAKE_MODE.strip().lower() == "always"
    log_mode = False
    if wake_always:
        print("準備完了。常時リッスン中です（WAKE_MODE=always）。そのまま話しかけてください。")
    else:
        print('準備完了。「ずんだもん」と話しかけてください。')

    while True:
        # ── ログモード: OFF に戻るまで連続リスニング（この中でブロックする） ──
        if log_mode:
            recorder = run_log_mode(recorder, wake, whisper, speaker, dlog)
            log_mode = False
            continue

        pcm = recorder.read()
        seed0 = None
        if wake_always:
            # 常時リッスン: 喋り出し（RMS が閾値超え）を検出したらそのままターンへ
            arr = np.asarray(pcm, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            if rms < C.SILENCE_RMS:
                continue
            seed0 = arr
        else:
            if not wake.process(pcm):
                continue
            wake.reset()  # 検出直後にバッファを消して連続誤発火を防ぐ

        # ── ターン連鎖（バージインで継続）。voice_agent.main() と同じ構造 ──
        seed = seed0
        beep_next = seed0 is None   # 呼びかけ式のときだけ合図を鳴らす
        while True:
            if beep_next:
                acknowledge(speaker)
            audio, t_speech_end = record_utterance(
                recorder, seed=seed, assume_started=(seed is not None))
            seed = None
            if audio is None or len(audio) < SAMPLE_RATE * 0.3:
                print("（聞き取れませんでした）")
                break

            # va._transcribe 経由で呼ぶ（UTTERANCE_DUMP_DIR 設定時の dump フックを通すため）
            user_text = va._transcribe(whisper, audio)
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
            speaker.set_anchor(t_speech_end)
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
                monitor.join()

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
            break                               # 通常終了 → 待ち受けへ


# ───────────────────────────────── Discord bot（asyncio） ─────────────────────────────────
class VoiceAgentBot(discord.Client):
    """指定のボイスチャンネルに常駐し、受信音声を AgentSink → DiscordRecorder へ
    流し込む。切断されたら自動で再接続する。エージェント本体は別スレッドで起動する。"""

    def __init__(self, recorder: DiscordRecorder, allowed_ids: set[int]):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.recorder = recorder
        self.allowed_ids = allowed_ids
        self.vc: voice_recv.VoiceRecvClient | None = None
        self._agent_started = False

    def get_vc(self):
        """エージェントスレッドから安全に呼べる『今つながっている VoiceClient』の取得口。"""
        vc = self.vc
        return vc if vc is not None and vc.is_connected() else None

    async def setup_hook(self):
        self._keeper = self.loop.create_task(self._keep_connected())

    async def _keep_connected(self):
        await self.wait_until_ready()
        while not self.is_closed():
            if self.get_vc() is None:
                try:
                    await self._connect_voice()
                except Exception as e:
                    print(f"[discord] VC 接続に失敗、10秒後に再試行: {e}", file=sys.stderr)
            await asyncio.sleep(10)

    async def _connect_voice(self):
        channel = self.get_channel(C.DISCORD_VOICE_CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(C.DISCORD_VOICE_CHANNEL_ID)
        if not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(
                f"DISCORD_VOICE_CHANNEL_ID={C.DISCORD_VOICE_CHANNEL_ID} はボイスチャンネルではありません")
        # 再接続時に古いクライアントが残っていれば片付ける
        if self.vc is not None:
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
        self.vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        self.vc.listen(AgentSink(self.recorder, self.allowed_ids))
        print(f"[discord] ボイスチャンネル「{channel.name}」に接続しました")
        if not self._agent_started:
            self._agent_started = True
            threading.Thread(target=agent_main, args=(self.recorder, self.get_vc),
                             daemon=True).start()

    async def on_ready(self):
        print(f"[discord] ログイン: {self.user}")


def _parse_allowed_ids(raw: str) -> set[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            sys.exit(f"DISCORD_ALLOWED_USER_IDS の値が不正です: {part}（数値のユーザー ID をカンマ区切りで）")
        ids.add(int(part))
    return ids


def main():
    if not C.DISCORD_BOT_TOKEN:
        sys.exit("DISCORD_BOT_TOKEN が未設定です（.env を確認。導入手順は DISCORD.md）")
    if not C.DISCORD_VOICE_CHANNEL_ID:
        sys.exit("DISCORD_VOICE_CHANNEL_ID が未設定です（.env を確認。導入手順は DISCORD.md）")
    allowed_ids = _parse_allowed_ids(C.DISCORD_ALLOWED_USER_IDS)
    if allowed_ids:
        print(f"[discord] 反応するユーザーを {len(allowed_ids)} 名に限定")

    recorder = DiscordRecorder()
    bot = VoiceAgentBot(recorder, allowed_ids)
    try:
        bot.run(C.DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        print("\n終了します。")


if __name__ == "__main__":
    main()
