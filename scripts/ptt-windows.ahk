; voice-agent — グローバル プッシュトゥトーク（Windows / AutoHotkey v2）
;
; 既定キー（F8）を「押している間だけ」録音する。ブラウザのタブが背面でも効く。
;   押下 → POST /api/remote-ptt?state=start
;   離す → POST /api/remote-ptt?state=stop
;
; 使い方:
;   1. AutoHotkey v2 をインストール: https://www.autohotkey.com/
;   2. BASE と PTT キーを環境に合わせて編集
;   3. このファイルをダブルクリックで常駐（タスクトレイに表示）
;
; ※ start が複数回飛んでもブラウザ側は recording 中なら無視するため安全。

#Requires AutoHotkey v2.0

BASE := "http://localhost:8000"   ; サーバの URL（SERVER_PORT に合わせる）

Ptt(state) {
    try {
        req := ComObject("MSXML2.XMLHTTP.6.0")
        req.open("POST", BASE "/api/remote-ptt?state=" state, false)
        req.send()
    }
    ; サーバ未起動などの失敗は黙って無視する
}

active := false

; ── PTT キー = F8（変えたい場合はこの2つのラベルのキー名を変更）──
F8:: {
    global active
    if !active {
        active := true
        Ptt("start")
    }
}
F8 Up:: {
    global active
    active := false
    Ptt("stop")
}
