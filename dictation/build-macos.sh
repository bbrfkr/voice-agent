#!/usr/bin/env bash
# voice-dictation.app（macOS のアプリバンドル）をビルドする。macOS 上で実行すること。
#
#   bash dictation/build-macos.sh            # ビルド
#   bash dictation/build-macos.sh --clean    # build/ dist/ を消してから作り直す
#
# PyInstaller はクロスコンパイルできないため、macOS 用のアプリは macOS でしか作れない。
# 出来上がりは dist/voice-dictation.app（Finder からダブルクリックで起動できる）。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

venv=".venv-build"

if [ "${1:-}" = "--clean" ]; then
    echo "== build/ dist/ を削除 =="
    rm -rf build dist
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 が見つかりません。python.org の公式インストーラか Homebrew で導入してください。" >&2
    exit 1
fi

# 最近の macOS の Python は PEP 668 で保護されており、システムへ直接 pip install できない
# （externally-managed-environment エラーになる）。ビルド専用の venv を作れば制約を受けず、
# ユーザの Python 環境も汚さずに済む。
echo "== ビルド用の仮想環境を用意（$venv） =="
# 中断やディスク不足で pip の入っていない壊れた venv が残ることがある。ディレクトリの
# 有無だけで判定すると次回以降ずっと「pip が無い」で失敗するので、使えるか確かめて作り直す。
if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
    rm -rf "$venv"
    if ! python3 -m venv "$venv"; then
        echo "仮想環境を作成できませんでした（$venv）。ディスク容量と python3 の導入状態を確認してください。" >&2
        exit 1
    fi
fi
py="$venv/bin/python"

echo "== 依存の導入 =="
# pip 自体の更新は失敗しても致命的ではないので続行する
"$py" -m pip install --upgrade pip || echo "pip の更新に失敗しましたが続行します"

# pip>=25.1 なら PEP 735 の --group が使える。古い pip 向けにフォールバックする。
if ! "$py" -m pip install --group dictation --group dictation-macos --group dictation-build; then
    echo "--group が使えないため個別に導入します"
    "$py" -m pip install "sounddevice>=0.4.6" "numpy>=1.24" "requests>=2.31" "pynput>=1.7" \
        "pyobjc-framework-Quartz>=9.0" "pyinstaller>=6.0"
fi

echo "== アプリのビルド =="
"$py" -m PyInstaller dictation/dictation.spec --noconfirm

app="dist/voice-dictation.app"
[ -d "$app" ] || { echo "アプリが生成されませんでした: $app" >&2; exit 1; }

# ad-hoc 署名。署名が無いとマイク／アクセシビリティの許可が再ビルドのたびに外れて付け直しになる。
echo "== ad-hoc 署名 =="
codesign --force --deep --sign - "$app"
codesign --verify --verbose "$app" || echo "署名の検証に失敗しました（動作はしますが許可が外れやすくなります）"

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
