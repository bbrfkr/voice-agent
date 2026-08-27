# voice-dictation.exe をビルドする（Windows 上で実行すること）。
#
#   powershell -ExecutionPolicy Bypass -File dictation\build-windows.ps1
#
# PyInstaller はクロスコンパイルできないため、Windows の exe は Windows でしか作れない。
# 出来上がりは dist\voice-dictation.exe（設定ファイル dictation.ini も一緒に置かれる）。
#
# ※ このファイルは UTF-8 (BOM 付き) で保存すること。Windows PowerShell 5.1 は BOM が無いと
#   スクリプトを CP932 として読むため、日本語コメントが文字化けして構文エラーになる。

[CmdletBinding()]
param(
    [switch]$Clean,                # build/ dist/ を消してから作り直す
    [string]$Python = "python"     # 使う Python（例: -Python py / -Python "py -3.12"）
)

$ErrorActionPreference = "Stop"

# 外部コマンドの失敗は例外にならない（PowerShell 5.1 では try/catch で捕まらない）ので、
# 終了コードを毎回確認する。-AllowFailure を付けたときだけ終了コードを返して続行する。
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string[]]$Command,
        [switch]$AllowFailure
    )
    $exe = $Command[0]
    $rest = @()
    if ($Command.Count -gt 1) { $rest = $Command[1..($Command.Count - 1)] }
    & $exe @rest
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "コマンドが失敗しました (終了コード $LASTEXITCODE): $($Command -join ' ')"
    }
    # 値は返さない。コマンドの標準出力が戻り値に混ざると、呼び出し側の
    # 終了コード判定が配列との比較になって壊れるため。終了コードは $LASTEXITCODE を見る。
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    # -Python に "py -3.12" のような引数付きの指定が来ても扱えるように分解する
    $py = @($Python -split '\s+' | Where-Object { $_ -ne "" })

    Write-Host "== Python の確認 ==" -ForegroundColor Cyan
    try {
        Invoke-Native ($py + @("--version"))
    } catch {
        throw "Python が見つかりません: '$Python'。python.org からインストールして PATH を通すか、-Python py を指定してください。"
    }

    Write-Host "== 依存の導入 ==" -ForegroundColor Cyan
    Invoke-Native ($py + @("-m", "pip", "install", "--upgrade", "pip")) -AllowFailure

    # pip>=25.1 なら PEP 735 の --group が使える。古い pip 向けにフォールバックする。
    Invoke-Native ($py + @("-m", "pip", "install", "--group", "dictation", "--group", "dictation-build")) -AllowFailure
    if ($LASTEXITCODE -ne 0) {
        Write-Host "--group が使えないため個別に導入します" -ForegroundColor Yellow
        Invoke-Native ($py + @("-m", "pip", "install", "sounddevice>=0.4.6", "numpy>=1.24", "requests>=2.31", "pynput>=1.7", "pyinstaller>=6.0"))
    }

    if ($Clean) {
        Write-Host "== build/ dist/ を削除 ==" -ForegroundColor Cyan
        Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    }

    Write-Host "== exe のビルド ==" -ForegroundColor Cyan
    Invoke-Native ($py + @("-m", "PyInstaller", "dictation\dictation.spec", "--noconfirm"))

    $exe = Join-Path $root "dist\voice-dictation.exe"
    if (-not (Test-Path $exe)) { throw "exe が生成されませんでした: $exe" }

    # 設定ファイルを exe の隣に置く（既にあれば上書きしない）
    $ini = Join-Path $root "dist\dictation.ini"
    if (-not (Test-Path $ini)) {
        Copy-Item (Join-Path $PSScriptRoot "dictation.ini.example") $ini
        Write-Host "設定ファイルを作成しました: $ini" -ForegroundColor Green
    }

    $sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "完成: $exe ($sizeMB MB)" -ForegroundColor Green
    Write-Host "dist フォルダごと好きな場所へ移動して、exe をダブルクリックすれば常駐します。"
    Write-Host "設定は同じフォルダの dictation.ini を編集してください。"
} catch {
    Write-Host ""
    Write-Host "ビルドに失敗しました: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
