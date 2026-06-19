; voice-agent — グローバル プッシュトゥトーク（Windows / AutoHotkey v2）
;
; 既定キー（F13）を「押している間だけ」録音する。ブラウザのタブが背面でも効く。
;   押下 → POST /api/remote-ptt?state=start
;   離す → POST /api/remote-ptt?state=stop
; ログモード／VAD はトグルではなく ON/OFF を別キーに割り当てて状態を確定させる
; （複数タブを開いていても確実に揃う）。
;
; 使い方:
;   1. AutoHotkey v2 をインストール: https://www.autohotkey.com/
;   2. BASE と各キーを環境に合わせて編集
;   3. このファイルをダブルクリックで常駐（タスクトレイに表示）
;
; ※ start が複数回飛んでもブラウザ側は recording 中なら無視するため安全。

#Requires AutoHotkey v2.0

BASE := "http://localhost:8000"   ; サーバの URL（SERVER_PORT に合わせる）

Post(path) {
    try {
        req := ComObject("MSXML2.XMLHTTP.6.0")
        req.open("POST", BASE path, false)
        req.send()
    }
    ; サーバ未起動などの失敗は黙って無視する
}

Ptt(state) {
    Post("/api/remote-ptt?state=" state)
}

active := false

; ── PTT キー = F13（変えたい場合はこの2つのラベルのキー名を変更）──
F13:: {
    global active
    if !active {
        active := true
        Ptt("start")
    }
}
F13 Up:: {
    global active
    active := false
    Ptt("stop")
}

; ── ログモード ON = F14 / OFF = F15 ──
; ON の間は STT 結果を Discord へ直送。
F14:: {
    Post("/api/remote-logmode?state=on")
}
F15:: {
    Post("/api/remote-logmode?state=off")
}

; ── 自動音声検出 (VAD) ON = F16 / OFF = F17 ──
; ON の間は声を検知して自動で録音開始・無音で停止。
F16:: {
    Post("/api/remote-vad?state=on")
}
F17:: {
    Post("/api/remote-vad?state=off")
}
