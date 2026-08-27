"""ディクテーションクライアントのコマンドライン入口。

    python -m dictation --server http://localhost:8000 --key f13

F13 を押している間だけ録音し、離した時点（および話の切れ目ごと）に文字起こしして
アクティブウィンドウへ打ち込む。

exe をダブルクリックで起動する運用では引数を渡せないため、同じ場所に置いた
`dictation.ini` を既定値として読む（`settings.py`）。優先順位は
コマンドライン引数 > dictation.ini > 組み込みの既定値。
"""

import argparse
import os
import sys
from pathlib import Path

from dictation import logfile
from dictation.audio import MicStream, VadParams, list_devices
from dictation.hotkey import PttListener, QuitHotkey
from dictation.inject import TextInjector, create_injector
from dictation.runner import Dictation, RunnerOptions
from dictation.settings import find_config
from dictation.settings import load as load_config
from dictation.stt_client import TranscribeClient

DEFAULT_SERVER = os.environ.get("VOICE_AGENT_URL", "http://localhost:8000")
#: 端末の無い起動（macOS の .app / ウィンドウ無し exe）でも確実に終了できるようにする
DEFAULT_QUIT_HOTKEY = "<ctrl>+<alt>+q"


class ConsoleInjector:
    """`--dry-run` 用。打鍵せずに標準出力へ出すだけ（GUI の無い環境での動作確認向け）。"""

    def type_text(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


def _prog() -> str:
    """ヘルプに出す起動コマンド名（exe 化されている場合は exe 名）。"""
    return Path(sys.executable).name if getattr(sys, "frozen", False) else "python -m dictation"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_prog(),
        description="音声をアクティブウィンドウへ文字入力する（voice-agent の STT を利用）",
    )
    p.add_argument("--config", default=None, help="設定ファイルのパス（既定: exe と同じ場所の dictation.ini）")
    p.add_argument("--server", default=DEFAULT_SERVER, help=f"voice-agent サーバの URL（既定: {DEFAULT_SERVER}）")
    p.add_argument("--key", default="f13", help="プッシュトゥトークのキー（既定: f13）")
    p.add_argument(
        "--quit-hotkey",
        default=DEFAULT_QUIT_HOTKEY,
        help=f"アプリを終了させるホットキー（既定: {DEFAULT_QUIT_HOTKEY}。空文字で無効）",
    )
    p.add_argument("--device", default=None, help="入力デバイスの番号または名前（既定: OS の既定デバイス）")
    p.add_argument("--list-devices", action="store_true", help="入力デバイス一覧を表示して終了")
    p.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "windows", "macos"],
        help="打鍵バックエンド（既定: auto＝OS から自動判定）",
    )
    p.add_argument("--dry-run", action="store_true", help="打鍵せず標準出力に表示する（動作確認用）")
    p.add_argument("--char-delay-ms", type=int, default=0, help="1 文字ごとの待ち ms（取りこぼすアプリ向け。既定: 0）")
    p.add_argument("--join", default="", help="セグメント間に挟む文字列（英語なら ' ' が自然。既定: 空）")
    p.add_argument("--quiet", action="store_true", help="認識結果をコンソールに出さない")
    g = p.add_argument_group("無音区切り（VAD）")
    g.add_argument("--threshold", type=float, default=VadParams.threshold, help="発話とみなす RMS しきい値")
    g.add_argument("--silence-ms", type=int, default=VadParams.silence_ms, help="この無音が続いたら区切って送る")
    g.add_argument("--min-speech-ms", type=int, default=VadParams.min_speech_ms, help="これ未満の発話は捨てる")
    g.add_argument("--max-segment-ms", type=int, default=VadParams.max_segment_ms, help="区切りが来なくても送る長さ")
    g.add_argument(
        "--no-split",
        action="store_true",
        help="話の切れ目で送らず、キーを離したときにまとめて送る（逐次入力しない）",
    )
    return p


def _device(value: str | None) -> int | str | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _parse(argv: list[str] | None) -> argparse.Namespace:
    """dictation.ini を既定値に敷いた上でコマンドライン引数を解釈する。

    --config だけを先に読む必要があるので、2 段階で解釈している。
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    known, _ = pre.parse_known_args(argv)

    parser = build_parser()
    path = find_config(known.config)
    if path is not None:
        parser.set_defaults(**load_config(path))
        print(f"設定ファイル: {path}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # 端末が無い起動（macOS の .app など）では、この時点で出力先をログファイルへ逃がす
    log = logfile.setup()

    try:
        args = _parse(argv)
    except (OSError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2

    if args.list_devices:
        print(list_devices())
        return 0

    vad = VadParams(
        threshold=args.threshold,
        silence_ms=args.silence_ms,
        min_speech_ms=args.min_speech_ms,
        max_segment_ms=args.max_segment_ms,
    )
    if args.no_split:
        # 押している間は区切らない。上限だけ非常に大きくして、離したときの flush に任せる。
        vad.silence_ms = 24 * 60 * 60 * 1000
        vad.max_segment_ms = 24 * 60 * 60 * 1000

    injector: TextInjector
    if args.dry_run:
        injector = ConsoleInjector()
    else:
        try:
            injector = create_injector(args.backend, char_delay_ms=args.char_delay_ms)
        except RuntimeError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 2

    client = TranscribeClient(args.server)
    mic = MicStream(device=_device(args.device))
    app = Dictation(client, injector, mic, vad, RunnerOptions(join=args.join, quiet=args.quiet))

    try:
        ptt = PttListener(args.key, app.press, app.release)
        quit_key = QuitHotkey(args.quit_hotkey, ptt.stop) if args.quit_hotkey else None
    except ValueError as e:  # 不明なキー名 / 不正なホットキー
        print(f"エラー: {e}", file=sys.stderr)
        return 2

    print(f"サーバ: {client.url}")
    print(f"プッシュトゥトーク: {args.key} を押している間だけ録音します")
    if quit_key is not None:
        print(f"終了: {quit_key.combo}（端末があれば Ctrl+C でも可）")
    if args.dry_run:
        print("※ --dry-run: 実際の打鍵はせず表示するだけです")
    if log is not None:
        print(f"ログ: {log}")

    try:
        with mic.open():
            app.start()
            if quit_key is not None:
                quit_key.start()
            ptt.run()
    except KeyboardInterrupt:
        pass
    finally:
        if quit_key is not None:
            quit_key.stop()
        app.shutdown()
    print("終了しました")
    return 0
