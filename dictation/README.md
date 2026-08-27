# dictation — 音声をアクティブウィンドウへ直接入力する

押している間だけ話すと、**いま入力フォーカスがあるウィンドウ**（エディタ・ブラウザ・Slack など）へ
文字が流し込まれる音声入力クライアント。文字起こしは voice-agent サーバの
`POST /api/transcribe`（faster-whisper）をそのまま使う。

```
[デスクトップOS: Windows / macOS]                    [voice-agent サーバ: WSL2 / Docker]
  マイク ──► 無音で区切る(VAD) ──► WAV ──HTTP──►  faster-whisper (GPU)
                                                          │
  アクティブウィンドウ ◄── Unicode 打鍵 ◄── テキスト ◄─────┘
```

## なぜクライアントを分けるのか

キーイベントの注入は **アクティブウィンドウを持つデスクトップ上のプロセス**からしか行えない。
WSL2 のサーバや Docker コンテナから Windows のウィンドウへは打てないため、サーバ（STT/GPU）は
そのままに、**このクライアントだけを入力先の OS で動かす**。Windows からは WSL2 のポート
フォワーディングにより `http://localhost:8000` でサーバへ到達できる。

## 「stream で入力」の実現方法

Whisper は発話全体を見て確定する非ストリーミングのモデルなので、文字単位の逐次確定はできない。
代わりに **無音（既定 1.2 秒）で区切った発話セグメント単位**で文字起こしし、確定した端から打鍵する。
押しっぱなしで話し続けると、区切りが来るたびに文字が流れ込む。

キーを離したときにまとめて 1 回だけ送りたい場合は `--no-split`。

## exe を作る（Windows）

**PyInstaller はクロスコンパイルできないため、Windows 用の exe は Windows 上でしか作れない**
（WSL2 側ではビルドできない）。Windows で以下を実行すると `dist\voice-dictation.exe` ができる。

```powershell
git clone <このリポジトリ>            # または WSL 側のリポジトリを \\wsl$ 経由で参照
cd voice-agent
powershell -ExecutionPolicy Bypass -File dictation\build-windows.ps1
```

スクリプトが依存の導入（`sounddevice` / `pynput` / `requests` / `numpy` / `pyinstaller`）と
ビルドをまとめて行う。出来上がるのは **単一 exe（約 30MB・Python のインストール不要）** と、
その隣に置かれる設定ファイル `dictation.ini`。

```
dist\
  voice-dictation.exe    ← ダブルクリックで常駐
  dictation.ini          ← 設定（サーバURL・キー・VAD しきい値など）
```

`dist\` フォルダごと好きな場所へ移動して使う。**exe をダブルクリック**すると小さな
コンソールウィンドウが開き、そのまま常駐する（認識結果がここに流れる。終了はウィンドウを
閉じるか `Ctrl+C`）。ウィンドウは最小化しておけばよい。

- **Windows 起動時に自動で立ち上げる**: `Win+R` → `shell:startup` で開くフォルダへ
  `voice-dictation.exe` のショートカットを置く。
- `-Clean` を付けると `build\` `dist\` を消してから作り直す。
- 使う Python を指定したい場合は `-Python "py -3.12"`。

### macOS（`.app` として一発起動）

```bash
bash dictation/build-macos.sh
```

`dist/voice-dictation.app` ができる。**Finder からダブルクリックで起動**でき、Terminal は要らない
（初回だけ、未署名アプリなので右クリック →「開く」で許可する）。

```
dist/
  voice-dictation.app    ← ダブルクリックで常駐
  dictation.ini          ← 設定（.app の中ではなく隣に置く）
```

初回起動時にやること:

1. **マイクの許可**を求められるので許可する
2. **システム設定 → プライバシーとセキュリティ → アクセシビリティ** に `voice-dictation` を
   追加して有効にする（キー監視と打鍵の両方に必要。許可が無いとイベントは黙って捨てられる）
3. いったん終了して起動し直す

**macOS 起動時に自動で立ち上げる**: システム設定 → 一般 → ログイン項目 に
`voice-dictation.app` を追加する。

#### Windows との違い

`.app` には端末が付かないため、Windows 版のようなコンソールウィンドウは出ない。代わりに:

- **Dock にも出ない**常駐アプリとして動く（`LSUIElement`）
- 認識結果やエラーは **ログファイル**に出る: `tail -f ~/Library/Logs/voice-dictation.log`
- 終了は **終了ホットキー**（既定 `ctrl+option+Q`）。効かないときは `killall voice-dictation`

ビルドスクリプトは ad-hoc 署名（`codesign --sign -`）まで行う。署名しておかないと、
再ビルドのたびにマイク／アクセシビリティの許可が外れて付け直しになる。

## 設定ファイル（dictation.ini）

exe と同じフォルダの `dictation.ini` が既定値になる。優先順位は
**コマンドライン引数 > dictation.ini > 組み込みの既定値**。
項目の一覧と意味は [`dictation.ini.example`](dictation.ini.example) を参照（`--config PATH`
で別の場所のファイルも指定できる）。

```ini
[dictation]
server = http://localhost:8000
key = f13
threshold = 0.015
silence_ms = 1200
```

## exe を作らずに動かす（開発時）

Python が入っている環境ならそのまま実行できる。

```powershell
pip install --group dictation      # pip>=25.1。macOS は --group dictation-macos も
python -m dictation
```

> pip<25.1 で `--group` が使えない場合:
> `pip install "sounddevice>=0.4.6" "numpy>=1.24" "requests>=2.31" "pynput>=1.7"`
> （macOS はこれに `"pyobjc-framework-Quartz>=9.0"` を足す）

## 使い方

**F13 を押している間だけ**録音し、離すと残りを確定して打ち込む（押している最中も、
話の切れ目ごとに逐次入力される）。

終了は **`Ctrl+Alt+Q`**（macOS は `ctrl+option+Q`）。端末から起動している場合は `Ctrl+C` でも
よい。終了ホットキーは `dictation.ini` の `quit_hotkey` で変更でき、空欄にすると無効になる。

通常は `dictation.ini` に書いておけばよいが、コマンドラインからも指定できる
（exe の代わりに `python -m dictation` でも同じ）。

```powershell
voice-dictation.exe                       # 既定（F13 / dictation.ini の設定）
voice-dictation.exe --key f9              # キーを変える
voice-dictation.exe --list-devices        # 入力デバイス一覧
voice-dictation.exe --device 3            # デバイスを指定
voice-dictation.exe --dry-run             # 打鍵せず画面に出すだけ（動作確認用）
voice-dictation.exe --join " "            # 英語ディクテーション（セグメント間に空白）
```

F13 は F11（全画面）・F12（開発者ツール）と衝突しにくいため既定にしている
（`scripts/` のグローバルホットキー用サンプルと同じ思想）。キーボードに F13 が無い場合は
`--key f9` などに変えるか、常駐ツールで空きキーを F13 へリマップする。

サーバ URL は `dictation.ini` の `server` / `--server` のほか、環境変数 `VOICE_AGENT_URL` でも指定できる。

## 調整

| オプション | 既定 | 効果 |
|---|---|---|
| `--threshold` | `0.015` | 発話とみなす RMS。周囲がうるさい環境では上げる |
| `--silence-ms` | `1200` | この無音で区切って送る。短くすると細かく速く入るが誤区切りが増える |
| `--min-speech-ms` | `300` | これ未満の発話は捨てる（クリック音・咳などの誤検出よけ） |
| `--max-segment-ms` | `15000` | 区切りが来なくても強制的に送る長さ |
| `--char-delay-ms` | `0` | 1 文字ごとの待ち。取りこぼすアプリでは `5`〜`10` 程度に |
| `--no-split` | off | 押している間は区切らず、離したときにまとめて送る |
| `--quit-hotkey` | `<ctrl>+<alt>+q` | 終了ホットキー。空文字で無効 |

既定のしきい値は Web UI の VAD（`server/static/app.js`）と揃えてある。

## 文字の注入方式

IME をバイパスして **Unicode を直接打鍵**する（Windows: `SendInput` + `KEYEVENTF_UNICODE` /
macOS: `CGEventKeyboardSetUnicodeString`）。日本語入力でも変換候補ウィンドウと干渉せず、
IME の ON/OFF に関係なく確定済みの文字がそのまま入る。

## うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| 文字が全く入らない（Windows） | 入力先が管理者権限で動いていると UIPI にブロックされる。クライアントも管理者として実行する |
| 文字が全く入らない（macOS） | アクセシビリティ権限が未許可。許可後はターミナルを再起動する |
| 文字が一部抜ける | アプリが一括入力に追随できていない。`--char-delay-ms 5` を試す |
| キーを押しても反応しない | そのキーが OS/常駐ソフトに奪われている。`--key` を変える |
| 短い発話が無視される | `--min-speech-ms` を下げる（例: `150`） |
| 話の途中で区切られる | `--silence-ms` を上げる（例: `1800`）か `--no-split` |
| 何も認識されない | `--dry-run` でテキストが出るか確認 → 出ないならマイク（`--list-devices`）かサーバ URL の問題 |
| exe が一瞬で閉じる | 設定ミスなどのエラー。異常終了時は Enter 待ちで止まるので、その表示を読む |
| exe が SmartScreen に止められる | 署名していないため。「詳細情報」→「実行」を選ぶ |
| ウイルス対策ソフトに消される | PyInstaller の onefile 形式でよくある誤検出。除外設定に追加する |
| .app が「開発元を確認できません」で開けない | 未署名のため。右クリック →「開く」で一度許可する |
| .app が起動しているか分からない | Dock に出ない設計。`~/Library/Logs/voice-dictation.log` を見る |
| .app を終了できない | 終了ホットキー（既定 `ctrl+option+Q`）。効かなければ `killall voice-dictation` |
| 再ビルドしたら許可が外れた | ad-hoc 署名を忘れている。`codesign --force --deep --sign - dist/voice-dictation.app` |

## 構成

| ファイル | 役割 |
|---|---|
| `audio.py` | マイク取り込み（sounddevice）と無音による発話セグメント分割 |
| `stt_client.py` | サーバの `/api/transcribe` を叩く（接続を使い回す） |
| `inject.py` | 打鍵バックエンドの選択（OS 自動判定） |
| `inject_windows.py` | Windows の `SendInput` + `KEYEVENTF_UNICODE` |
| `inject_macos.py` | macOS の Quartz `CGEvent` |
| `hotkey.py` | グローバル PTT キーの監視（pynput） |
| `runner.py` | 録音 → STT → 打鍵の配線（送信は 1 スレッドに直列化して順序を保つ） |
| `settings.py` | `dictation.ini` の読み込み（.app の隣・ユーザ設定領域も探す） |
| `logfile.py` | 端末が無いとき（.app）に出力をログファイルへ逃がす |
| `make_icons.py` | アイコン（`icon.ico` / `icon.icns`）の生成 |
| `cli.py` | コマンドライン入口 |
| `dictation.spec` | PyInstaller のビルド定義（exe 化） |
| `build-windows.ps1` / `build-macos.sh` | ビルド用スクリプト |
| `dictation.ini.example` | 設定ファイルの雛形（全項目の説明つき） |
