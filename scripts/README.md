# グローバル プッシュトゥトーク（remote-ptt）スクリプト

ブラウザのタブが**背面（非アクティブ）でも**プッシュトゥトークできるようにする、
OS 別のグローバルホットキー用サンプルです。仕組みはどれも同じで、サーバの
`POST /api/remote-ptt?state=start|stop`（`server/app.py`）を OS のホットキーから叩き、
接続中のブラウザに録音の開始/停止を WebSocket で指示します。

| OS | ファイル | 必要ツール |
|----|----------|-----------|
| Windows | `ptt-windows.ahk` | [AutoHotkey v2](https://www.autohotkey.com/) |
| Linux (X11) | `ptt-linux-sxhkd.conf` | [sxhkd](https://github.com/baskerville/sxhkd) |
| macOS | `ptt-macos-hammerspoon.lua` | [Hammerspoon](https://www.hammerspoon.org/) |

各ファイル冒頭のコメントにインストール手順があります。共通の編集ポイント:

- **URL / ポート**: 既定は `http://localhost:8000`。`.env` の `SERVER_PORT` を変えた場合は合わせる。
- **PTT キー**: 既定は `F8`。好みのキーに変更可。
- LAN の別端末から叩く場合は `localhost` をサーバの IP/ホスト名に変更（公開時は TLS/認証を）。

## 動作確認（curl）

スクリプトを入れる前に、API 単体はこれで試せます:

```bash
curl -s -X POST "http://localhost:8000/api/remote-ptt?state=start"
# …話す…
curl -s -X POST "http://localhost:8000/api/remote-ptt?state=stop"
```

## 補足

- 押しっぱなし中に `start` が複数回飛んでも、ブラウザ側は録音中なら無視するため安全です。
- Linux は X11 専用（sxhkd は Wayland では動きません）。Wayland では各コンポジタの
  ホットキー機能から同じ `curl` を press/release にバインドしてください。
- **キーを使わずに完全ハンズフリー**にしたい場合は、Web UI の「自動音声検出 (VAD)」を
  ON にしてください。声を検知して自動で録音開始・無音で停止します（背面タブでも動作）。
