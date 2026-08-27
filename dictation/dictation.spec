# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller のビルド定義（`voice-dictation.exe` を作る）。

    pyinstaller dictation/dictation.spec --noconfirm

exe は Windows 上でしかビルドできない（PyInstaller はクロスコンパイルに対応していない）。
macOS で実行すれば同じ定義から `voice-dictation.app`（Finder からダブルクリックで
起動できるアプリバンドル）ができる。

onefile（単一 exe）にしているので配布は exe 1 つで済む。設定は exe と同じ場所に置いた
`dictation.ini` から読む（`dictation/settings.py`）。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

HERE = Path(SPECPATH)  # noqa: F821  （SPECPATH は PyInstaller が注入する）
ROOT = HERE.parent

# sounddevice は PortAudio の共有ライブラリを _sounddevice_data として同梱している。
# 通常は hooks が拾うが、環境差で取りこぼすことがあるため明示的に集める。
datas = []
binaries = []
for pkg in ("sounddevice", "_sounddevice_data"):
    try:
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)
    except Exception as e:  # 未インストール等は無視（hooks 任せにする）
        print(f"[spec] {pkg} の収集をスキップ: {e}")

# pynput はバックエンドを実行時に動的 import するので、静的解析では拾えない。
if sys.platform == "win32":
    hiddenimports = ["pynput.keyboard._win32", "pynput.mouse._win32"]
elif sys.platform == "darwin":
    # Quartz（pyobjc）は打鍵バックエンドが関数内で import するうえ、pyobjc 自体が
    # サブモジュールを動的に解決するため、明示しておかないと取りこぼすことがある。
    hiddenimports = ["pynput.keyboard._darwin", "pynput.mouse._darwin", "Quartz"]
else:
    hiddenimports = []

IS_MAC = sys.platform == "darwin"
# Linux ビルド（spec の検証用）ではアイコンを使わない
icon = str(HERE / ("icon.icns" if IS_MAC else "icon.ico")) if sys.platform in ("win32", "darwin") else None

a = Analysis(  # noqa: F821
    [str(HERE / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # サーバ側の重い依存（faster-whisper/torch 等）はクライアントには不要。
    # 誤って引き込むと exe が数百 MB になるので明示的に外す。
    excludes=["torch", "faster_whisper", "ctranslate2", "fastapi", "uvicorn", "av", "tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="voice-dictation",
    debug=False,
    strip=False,
    upx=False,
    # Windows はコンソールを残す（認識結果がその場で流れ、Ctrl+C で終了できる）。
    # macOS の .app には端末が付かないので、出力は自動でログファイルへ回る（logfile.py）。
    console=not IS_MAC,
    icon=icon,
)

if IS_MAC:
    app = BUNDLE(  # noqa: F821
        exe,
        name="voice-dictation.app",
        icon=icon,
        # TCC（マイク／アクセシビリティの許可）はこの ID 単位で記憶されるので、
        # 一度許可すれば再ビルドしても引き継がれる（ad-hoc 署名と併用すること）。
        bundle_identifier="dev.bbrfkr.voice-dictation",
        info_plist={
            "CFBundleShortVersionString": "1.0.0",
            # この文言が無いと macOS はマイクへのアクセスを問答無用で拒否する。
            "NSMicrophoneUsageDescription": "話した内容を文字起こしして、入力中のウィンドウへ打ち込みます。",
            # 常駐ユーティリティなので Dock には出さない（終了は終了ホットキー）。
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
        },
    )
