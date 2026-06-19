# グローバル ホットキー（remote-ptt / remote-logmode）スクリプト

ブラウザのタブが**背面（非アクティブ）でも**操作できるようにする、
OS 別のグローバルホットキー用サンプルです。仕組みはどれも同じで、サーバの API を
OS のホットキーから叩き、接続中のブラウザへ WebSocket で指示を送ります。

各スクリプトは既定で次の2つのキーをバインドします（キーは各ファイルで変更可）:

| キー | 動作 | API |
|------|------|-----|
| `F8` | 押している間だけ録音（プッシュトゥトーク） | `POST /api/remote-ptt?state=start\|stop` |
| `F9` | ログモードの ON/OFF を切り替え（ON 中は STT 結果を Discord へ直送） | `POST /api/remote-logmode?state=toggle` |

| OS | ファイル | 必要ツール |
|----|----------|-----------|
| Windows | `ptt-windows.ahk` | [AutoHotkey v2](https://www.autohotkey.com/) |
| Linux (X11) | `ptt-linux-sxhkd.conf` | [sxhkd](https://github.com/baskerville/sxhkd) |
| macOS | `ptt-macos-hammerspoon.lua` | [Hammerspoon](https://www.hammerspoon.org/) |

各ファイル冒頭のコメントにインストール手順があります。共通の編集ポイント:

- **URL / ポート**: 既定は `http://localhost:8000`。`.env` の `SERVER_PORT` を変えた場合は合わせる。
- **PTT キー / ログモードキー**: 既定は `F8` / `F9`。好みのキーに変更可。
- LAN の別端末から叩く場合は `localhost` をサーバの IP/ホスト名に変更（公開時は TLS/認証を）。

## 動作確認（curl）

スクリプトを入れる前に、API 単体はこれで試せます:

```bash
curl -s -X POST "http://localhost:8000/api/remote-ptt?state=start"
# …話す…
curl -s -X POST "http://localhost:8000/api/remote-ptt?state=stop"

# ログモードの切り替え（押すたびに ON/OFF。on/off を明示する場合は state=on|off）
curl -s -X POST "http://localhost:8000/api/remote-logmode?state=toggle"
```

## 補足

- 押しっぱなし中に `start` が複数回飛んでも、ブラウザ側は録音中なら無視するため安全です。
- ログモードの状態はブラウザ側（チェックボックス）が保持します。複数のタブを開いている場合、
  `toggle` は各タブで個別に反転するため、確実に揃えたい時は `state=on`／`state=off` を使ってください。
- Linux は X11 専用（sxhkd は Wayland では動きません）。Wayland では各コンポジタの
  ホットキー機能から同じ `curl` を press/release にバインドしてください。
- **キーを使わずに完全ハンズフリー**にしたい場合は、Web UI の「自動音声検出 (VAD)」を
  ON にしてください。声を検知して自動で録音開始・無音で停止します（背面タブでも動作）。
