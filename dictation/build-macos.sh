#!/usr/bin/env bash
# voice-dictation.app（macOS のアプリバンドル）をビルドする。macOS 上で実行すること。
#
#   bash dictation/build-macos.sh
#
# PyInstaller はクロスコンパイルできないため、macOS 用のアプリは macOS でしか作れない。
# 出来上がりは dist/voice-dictation.app（Finder からダブルクリックで起動できる）。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

echo "== 依存の導入 =="
python3 -m pip install --upgrade pip
python3 -m pip install --group dictation --group dictation-macos --group dictation-build \
  || python3 -m pip install "sounddevice>=0.4.6" "numpy>=1.24" "requests>=2.31" "pynput>=1.7" \
       "pyobjc-framework-Quartz>=9.0" "pyinstaller>=6.0"

echo "== アプリのビルド =="
python3 -m PyInstaller dictation/dictation.spec --noconfirm

app="dist/voice-dictation.app"
[ -d "$app" ] || { echo "アプリが生成されませんでした: $app" >&2; exit 1; }

# ad-hoc 署名。署名が無いとマイク／アクセシビリティの許可が再ビルドのたびに外れる。
echo "== ad-hoc 署名 =="
codesign --force --deep --sign - "$app"

# 設定ファイルは .app の中ではなく隣に置く（ユーザが編集するため）
[ -f dist/dictation.ini ] || cp dictation/dictation.ini.example dist/dictation.ini

cat <<MSG

完成: $root/$app

  1. dist/ フォルダごと /Applications などへ移動する
  2. voice-dictation.app をダブルクリック（初回は右クリック →「開く」）
  3. マイクの許可を求められたら許可する
  4. システム設定 → プライバシーとセキュリティ → アクセシビリティ に
     voice-dictation を追加して有効にする（キー監視と打鍵に必要）
  5. 一度終了して起動し直す

設定は dist/dictation.ini（.app の隣）を編集する。
Dock には出ない常駐アプリなので、終了は ctrl+option+Q。ログは:
  tail -f ~/Library/Logs/voice-dictation.log
MSG
