# グローバル ホットキー（remote-ptt / remote-logmode / remote-vad）スクリプト

ブラウザのタブが**背面（非アクティブ）でも**操作できるようにする、
OS 別のグローバルホットキー用サンプルです。仕組みはどれも同じで、サーバの API を
OS のホットキーから叩き、接続中のブラウザへ WebSocket で指示を送ります。

各スクリプトは既定で次のキーをバインドします（キーは各ファイルで変更可）。
ログモード／VAD はトグルではなく **ON/OFF を別キーに割り当てて状態を確定**させます
（複数タブを開いていても確実に揃う）。F13 以降は OS/ブラウザの既存ショートカット
（F11 全画面・F12 開発者ツール等）と衝突しにくいキーです:

| キー | 動作 | API |
|------|------|-----|
| `F13` | 押している間だけ録音（プッシュトゥトーク） | `POST /api/remote-ptt?state=start\|stop` |
| `F14` | ログモード **ON**（STT 結果を Discord へ直送） | `POST /api/remote-logmode?state=on` |
| `F15` | ログモード **OFF** | `POST /api/remote-logmode?state=off` |
| `F16` | 自動音声検出 (VAD) **ON**（声を検知して自動録音） | `POST /api/remote-vad?state=on` |
| `F17` | 自動音声検出 (VAD) **OFF** | `POST /api/remote-vad?state=off` |

| OS | ファイル | 必要ツール |
|----|----------|-----------|
| Windows | `ptt-windows.ahk` | [AutoHotkey v2](https://www.autohotkey.com/) |
| Linux (X11) | `ptt-linux-sxhkd.conf` | [sxhkd](https://github.com/baskerville/sxhkd) |
| macOS | `ptt-macos-hammerspoon.lua` | [Hammerspoon](https://www.hammerspoon.org/) |

各ファイル冒頭のコメントにインストール手順があります。共通の編集ポイント:

- **URL / ポート**: 既定は `http://localhost:8000`。`.env` の `SERVER_PORT` を変えた場合は合わせる。
- **各キー**: 既定は PTT=`F13`、ログモード ON/OFF=`F14`/`F15`、VAD ON/OFF=`F16`/`F17`。好みのキーに変更可。
- LAN の別端末から叩く場合は `localhost` をサーバの IP/ホスト名に変更（公開時は TLS/認証を）。

## 動作確認（curl）

スクリプトを入れる前に、API 単体はこれで試せます:

```bash
curl -s -X POST "http://localhost:8000/api/remote-ptt?state=start"
# …話す…
curl -s -X POST "http://localhost:8000/api/remote-ptt?state=stop"

# ログモードの状態を確定（state=on|off。state=toggle で反転も可）
curl -s -X POST "http://localhost:8000/api/remote-logmode?state=on"
curl -s -X POST "http://localhost:8000/api/remote-logmode?state=off"

# 自動音声検出 (VAD) の状態を確定（state=on|off。state=toggle で反転も可）
curl -s -X POST "http://localhost:8000/api/remote-vad?state=on"
curl -s -X POST "http://localhost:8000/api/remote-vad?state=off"
```

## 補足

- 押しっぱなし中に `start` が複数回飛んでも、ブラウザ側は録音中なら無視するため安全です。
- ログモード／VAD の状態はブラウザ側（チェックボックス）が保持します。サンプルスクリプトは
  ON/OFF を別キーに割り当てて `state=on`／`state=off` を送るため、複数タブを開いていても状態が確実に揃います
  （`state=toggle` は各タブで個別に反転するので、状態を揃えたい用途には使いません）。
- Linux は X11 専用（sxhkd は Wayland では動きません）。Wayland では各コンポジタの
  ホットキー機能から同じ `curl` を press/release にバインドしてください。
- **キーを使わずに完全ハンズフリー**にしたい場合は、Web UI の「自動音声検出 (VAD)」を
  ON にしてください。声を検知して自動で録音開始・無音で停止します（背面タブでも動作）。
