"""
Discord ボイスチャンネル版エージェントのエントリ（asyncio）。

bot を指定のボイスチャンネルに常駐させ、ウェイクワード無しでチャンネル内の発話を聞いて応答する。
  受信: VoiceRecvClient + AudioSink で話者ごとに PCM(48kHz/stereo) をバッファし、
        発話停止イベント（パケット途切れ）で 1 発話を確定する。
  処理: 16kHz/mono へ変換 → STT(リモート HTTP) → handle_turn(LLM/opencode) → VOICEVOX 合成
  送信: 合成 PCM を 48kHz/stereo に変換してボイスチャンネルへ再生する。
  バージイン: 再生中にユーザーが話し始めたら再生を即停止し、未再生の合成キューを捨てる。
  ログモード: STT テキストの切替コマンドで ON/OFF。ON 中は LLM/TTS を挟まず Discord へ直送する。

STT/TTS は HTTP の外部サービス（GPU サーバ集約）。重い処理は run_in_executor で逃がす。
設定は config.py（`.env` 駆動）に集約。値の編集は `.env` で行う。
"""

import asyncio
import queue
import sys
import threading
import time

import discord
from discord.ext import voice_recv

import config as C
import voice_agent as va

# 20ms 分の Discord PCM（48kHz / stereo / 16bit）= 48000*0.02*2ch*2byte = 3840 バイト
_FRAME_BYTES = int(va.DISCORD_RATE * 0.02) * va.DISCORD_CHANNELS * 2
_BYTES_PER_SEC = va.DISCORD_RATE * va.DISCORD_CHANNELS * 2  # 48kHz/stereo/16bit = 192000 B/s


# ───────────────────────────────── 再生（合成 PCM → ボイスチャンネル） ─────────────────────────────────
class _QueuedPCM(discord.AudioSource):
    """DiscordPlayer のキューから 20ms ずつ PCM を取り出して discord に渡す音源。"""

    def __init__(self, player: "DiscordPlayer"):
        self.p = player

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        p = self.p
        with p._lock:
            buf = p._buf
            while len(buf) < _FRAME_BYTES:
                try:
                    buf += p._q.get_nowait()
                except queue.Empty:
                    break
            if len(buf) >= _FRAME_BYTES:
                p._buf = buf[_FRAME_BYTES:]
                return buf[:_FRAME_BYTES]
            # キューが尽きた → このフレームで再生を終える（次の feed で再開）
            p._buf = b""
            p._idle.set()
            if buf:
                return buf + b"\x00" * (_FRAME_BYTES - len(buf))  # 半端は無音パディングで 1 フレーム
            return b""


class DiscordPlayer:
    """合成済み音声（float32）を 48kHz/stereo PCM に変換してボイスチャンネルへ流す。
    Speaker から feed()/clear()/wait_idle() で使われる。スレッド境界（合成ワーカー →
    discord のイベントループ）を call_soon_threadsafe で跨ぐ。"""

    def __init__(self, vc: voice_recv.VoiceRecvClient, loop: asyncio.AbstractEventLoop):
        self.vc = vc
        self.loop = loop
        self._q: queue.Queue[bytes] = queue.Queue()
        self._buf = b""
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def feed(self, data, sr: int) -> None:
        pcm = va.to_discord_pcm(data, sr)
        if not pcm:
            return
        self._idle.clear()
        self._q.put(pcm)
        self.loop.call_soon_threadsafe(self._ensure_playing)

    def _ensure_playing(self) -> None:
        try:
            if self.vc.is_connected() and not self.vc.is_playing():
                self.vc.play(_QueuedPCM(self), after=self._after)
        except Exception as e:
            print(f"[play] 再生開始に失敗: {e}", file=sys.stderr)

    def _after(self, err) -> None:
        if err:
            print(f"[play] 再生エラー: {err}", file=sys.stderr)

    def clear(self) -> None:
        """再生を即停止し、未再生のキューを破棄する（バージイン用）。"""
        with self._lock:
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._buf = b""
        self._idle.set()
        self.loop.call_soon_threadsafe(self._stop)

    def _stop(self) -> None:
        try:
            if self.vc.is_playing():
                self.vc.stop_playing()
        except Exception:
            pass

    def is_active(self) -> bool:
        if not self._idle.is_set():
            return True
        try:
            return bool(self.vc.is_playing())
        except Exception:
            return False

    def wait_idle(self, timeout: float = 30.0) -> None:
        self._idle.wait(timeout)


# ───────────────────────────────── 受信（発話のバッファリングと確定） ─────────────────────────────────
class SegmentSink(voice_recv.AudioSink):
    """話者ごとに受信 PCM をバッファし、発話停止で 1 発話として確定する。
    再生中にユーザーが一定時間話し続けたらバージイン（割り込み）を発火する。"""

    def __init__(self, agent: "Agent"):
        super().__init__()
        self.agent = agent
        self.loop = agent.loop
        self._lock = threading.Lock()
        self._bufs: dict[int, list[bytes]] = {}
        self._lens: dict[int, int] = {}
        self._seen: set[int] = set()  # 受信デバッグ: 話者ごとに初回パケットを 1 度だけ通知
        self._barge_min = int(C.BARGE_IN_MIN_SEC * _BYTES_PER_SEC)
        self._max_bytes = int(C.MAX_UTTERANCE_SEC * _BYTES_PER_SEC)

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data) -> None:
        if user is None or getattr(user, "bot", False):
            return
        pcm = data.pcm
        if not pcm:
            return
        uid = user.id
        if uid not in self._seen:
            self._seen.add(uid)
            print(f"[recv] {getattr(user, 'display_name', uid)} の音声パケット受信を確認")
        with self._lock:
            buf = self._bufs.setdefault(uid, [])
            prev = self._lens.get(uid, 0)
            buf.append(pcm)
            new_len = prev + len(pcm)
            self._lens[uid] = new_len
            over_max = new_len >= self._max_bytes
        # バージイン: 再生中に最低継続秒を超えた「瞬間」に 1 回だけ割り込む
        if C.BARGE_IN_ENABLED and self.agent.is_speaking() and prev < self._barge_min <= new_len:
            self.loop.call_soon_threadsafe(self.agent.on_barge_in)
        if over_max:  # 最大長で強制確定（保険）
            self._finalize(uid, user)

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member) -> None:
        print(f"[recv] 発話停止イベント: {getattr(member, 'display_name', member)}")
        self._finalize(member.id, member)

    def _finalize(self, uid: int, member) -> None:
        if getattr(member, "bot", False):
            return
        with self._lock:
            chunks = self._bufs.pop(uid, None)
            self._lens.pop(uid, None)
        if not chunks:
            return
        pcm = b"".join(chunks)
        self.loop.call_soon_threadsafe(self.agent.submit_utterance, member, pcm)

    def cleanup(self) -> None:
        with self._lock:
            self._bufs.clear()
            self._lens.clear()


# ───────────────────────────────── オーケストレーション ─────────────────────────────────
class Agent:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.vc: voice_recv.VoiceRecvClient | None = None
        self.player: DiscordPlayer | None = None
        self.speaker: va.Speaker | None = None
        self.opencode = va.OpenCode()
        self.dlog = va.DiscordLogger()
        self.messages = [{"role": "system", "content": C.SYSTEM_PROMPT}]
        self.log_mode = False
        self._current: va.TurnInterrupt | None = None
        self._utterances: asyncio.Queue = asyncio.Queue()

    def attach_voice(self, vc: voice_recv.VoiceRecvClient) -> None:
        self.vc = vc
        self.player = DiscordPlayer(vc, self.loop)
        self.speaker = va.Speaker(self.player)
        if getattr(C, "WARMUP", True):
            self.loop.run_in_executor(None, va.warmup, self.speaker, list(self.messages))

    def is_speaking(self) -> bool:
        return bool(self.player and self.player.is_active())

    def on_barge_in(self) -> None:
        cur = self._current
        if cur is None or cur.triggered.is_set():
            return
        print("（割り込みを検出 → 再生停止）")
        cur.triggered.set()
        if self.speaker:
            self.speaker.interrupt()

    def submit_utterance(self, member, pcm: bytes) -> None:
        self._utterances.put_nowait((member, pcm))

    async def consume(self) -> None:
        while True:
            member, pcm = await self._utterances.get()
            try:
                await self._process(member, pcm)
            except Exception as e:
                print(f"[turn error] {e}", file=sys.stderr)
            finally:
                self._utterances.task_done()

    async def _process(self, member, pcm: bytes) -> None:
        assert self.speaker is not None
        loop = asyncio.get_running_loop()
        audio = va.discord_pcm_to_16k_mono(pcm)
        dur = audio.size / va.SAMPLE_RATE
        if audio.size < int(C.MIN_UTTERANCE_SEC * va.SAMPLE_RATE):
            print(f"[recv] 発話が短すぎてスキップ（{dur:.2f}s < {C.MIN_UTTERANCE_SEC}s）")
            return
        t_end = time.monotonic()  # 発話完了時刻（総遅延の起点）
        text = await loop.run_in_executor(None, va.transcribe, audio)
        if not text:
            return
        print(f"あなた（{member.display_name}）: {text}")

        # ログモードの切替コマンド（LLM・会話ログには流さない）
        cmd = va._match_log_command(text)
        if cmd is not None:
            await self._toggle_log_mode(cmd)
            return

        if self.log_mode:
            self.dlog.log(text)
            print("[logmode] Discord へ直送")
            return

        self.dlog.user(text)
        self.speaker.clear_interrupt()
        self.speaker.set_anchor(t_end)
        self._current = va.TurnInterrupt()
        try:
            await loop.run_in_executor(
                None, va.handle_turn, text, self.messages, self.speaker, self.opencode, self._current, self.dlog
            )
        finally:
            self._current = None

    async def _toggle_log_mode(self, cmd: str) -> None:
        assert self.speaker is not None
        loop = asyncio.get_running_loop()
        self.speaker.clear_interrupt()
        if cmd == "on":
            if self.log_mode:
                return
            if not self.dlog.log_enabled:
                self.speaker.say("ログモードの送信先が設定されていないので、オンにできません")
                print("[logmode] DISCORD_WEBHOOK_URL_LOGMODE 未設定のため ON にできません", file=sys.stderr)
                await loop.run_in_executor(None, self.speaker.wait_done)
                return
            self.log_mode = True
            print("[logmode] ON")
            self.speaker.say("ログモードがオンになりました")
        else:  # off
            if not self.log_mode:
                self.speaker.say("ログモードはもともとオフです")
                await loop.run_in_executor(None, self.speaker.wait_done)
                return
            self.log_mode = False
            print("[logmode] OFF")
            self.speaker.say("ログモードがオフになりました")
        await loop.run_in_executor(None, self.speaker.wait_done)


# ───────────────────────────────── 起動 ─────────────────────────────────
async def _run() -> None:
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
        except Exception as e:
            print(f"[discord] libopus のロードに失敗（音声送受信に必要）: {e}", file=sys.stderr)

    intents = discord.Intents.default()
    intents.voice_states = True
    # voice-recv は発話者を SSRC→メンバーで解決する。発話停止イベント
    # (on_voice_member_speaking_stop) はギルドのメンバーキャッシュ
    # (guild.get_member) で解決できたときだけ発火し、解決できないと握り潰される。
    # キャッシュを埋めるには特権インテント「SERVER MEMBERS INTENT」が必須
    # （Developer Portal 側でも有効化が必要。無効のまま要求すると起動時に
    # PrivilegedIntentsRequired で落ちる）。これが無いと発話が一切確定せず無応答になる。
    intents.members = True
    client = discord.Client(intents=intents)
    loop = asyncio.get_running_loop()
    agent = Agent(loop)

    @client.event
    async def on_ready() -> None:
        print(f"[discord] ログイン: {client.user}")
        if agent.vc is not None:
            return  # 再接続時の二重接続を防ぐ
        channel = client.get_channel(C.DISCORD_VOICE_CHANNEL_ID)
        if not isinstance(channel, discord.VoiceChannel):
            print(
                f"[discord] ボイスチャンネル(ID={C.DISCORD_VOICE_CHANNEL_ID})が見つかりません。"
                "DISCORD_VOICE_CHANNEL_ID を確認してください。",
                file=sys.stderr,
            )
            return
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        agent.attach_voice(vc)
        vc.listen(SegmentSink(agent))
        loop.create_task(agent.consume())
        print(f"[discord] ボイスチャンネル『{channel.name}』に接続。発話を待っています（Ctrl+C で終了）。")

    await client.start(C.DISCORD_BOT_TOKEN)


def main() -> None:
    if not C.DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN が未設定です。`.env` に設定してください。", file=sys.stderr)
        raise SystemExit(1)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n終了します。")


if __name__ == "__main__":
    main()
