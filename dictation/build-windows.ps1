# voice-dictation.exe をビルドする（Windows 上で実行すること）。
#
#   powershell -ExecutionPolicy Bypass -File dictation\build-windows.ps1
#
# PyInstaller はクロスコンパイルできないため、Windows の exe は Windows でしか作れない。
# 出来上がりは dist\voice-dictation.exe（設定ファイル dictation.ini も一緒に置かれる）。

param(
    [switch]$Clean,                      # build/ dist/ を消してから作り直す
    [string]$Python = "python"           # 使う Python（例: -Python py -3.12）
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    Write-Host "== 依存の導入 ==" -ForegroundColor Cyan
    # pip>=25.1 なら PEP 735 の --group が使える。古い pip 向けにフォールバックする。
    & $Python -m pip install --upgrade pip
    try {
        & $Python -m pip install --group dictation --group dictation-build
    } catch {
        Write-Host "--group が使えないため個別に導入します" -ForegroundColor Yellow
        & $Python -m pip install "sounddevice>=0.4.6" "numpy>=1.24" "requests>=2.31" "pynput>=1.7" "pyinstaller>=6.0"
    }

    if ($Clean) {
        Write-Host "== build/ dist/ を削除 ==" -ForegroundColor Cyan
        Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    }

    Write-Host "== exe のビルド ==" -ForegroundColor Cyan
    & $Python -m PyInstaller dictation\dictation.spec --noconfirm

    $exe = Join-Path $root "dist\voice-dictation.exe"
    if (-not (Test-Path $exe)) { throw "exe が生成されませんでした: $exe" }

    # 設定ファイルを exe の隣に置く（既にあれば上書きしない）
    $ini = Join-Path $root "dist\dictation.ini"
    if (-not (Test-Path $ini)) {
        Copy-Item (Join-Path $PSScriptRoot "dictation.ini.example") $ini
        Write-Host "設定ファイルを作成しました: $ini" -ForegroundColor Green
    }

    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "完成: $exe ($size MB)" -ForegroundColor Green
    Write-Host "dist\ フォルダごと好きな場所へ移動して、exe をダブルクリックすれば常駐します。"
    Write-Host "設定は同じフォルダの dictation.ini を編集してください。"
} finally {
    Pop-Location
}
