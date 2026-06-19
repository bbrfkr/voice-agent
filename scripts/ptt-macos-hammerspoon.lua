-- voice-agent — グローバル プッシュトゥトーク（macOS / Hammerspoon）
--
-- 既定キー（F13）を「押している間だけ」録音する。ブラウザのタブが背面でも効く。
-- ログモード／VAD はトグルではなく ON/OFF を別キーに割り当てて状態を確定させる
-- （複数タブを開いていても確実に揃う）。
--
-- 使い方:
--   1. Hammerspoon をインストール: https://www.hammerspoon.org/
--   2. この内容を ~/.hammerspoon/init.lua にコピー（または require で読み込む）
--   3. Hammerspoon を起動し、初回はアクセシビリティ権限を許可する
--      （システム設定 > プライバシーとセキュリティ > アクセシビリティ）
--   4. メニューバーから Reload Config
--
-- BASE（ポート）は SERVER_PORT に合わせて書き換える。

local BASE = "http://localhost:8000"   -- サーバの URL
local PTT_KEY = "f13"                   -- PTT キー
local LOGMODE_ON_KEY = "f14"            -- ログモード ON
local LOGMODE_OFF_KEY = "f15"           -- ログモード OFF
local VAD_ON_KEY = "f16"                -- 自動音声検出 (VAD) ON
local VAD_OFF_KEY = "f17"               -- 自動音声検出 (VAD) OFF

local function post(path)
    -- 非同期 POST。応答は使わないので無視する。
    hs.http.asyncPost(BASE .. path, "", nil, function() end)
end

local function ptt(state)
    post("/api/remote-ptt?state=" .. state)
end

local active = false
local targetCode = hs.keycodes.map[PTT_KEY]

-- keyDown は押しっぱなしで連続発火するため active フラグで1回に抑える。
-- グローバル変数に保持してガベージコレクトされないようにする。
pttTap = hs.eventtap.new(
    { hs.eventtap.event.types.keyDown, hs.eventtap.event.types.keyUp },
    function(e)
        if e:getKeyCode() ~= targetCode then
            return false
        end
        if e:getType() == hs.eventtap.event.types.keyDown then
            if not active then
                active = true
                ptt("start")
            end
        else
            active = false
            ptt("stop")
        end
        return false -- イベントは他アプリにもそのまま通す
    end
)
pttTap:start()

-- ── ログモード ON = F14 / OFF = F15 ──
-- ON の間は STT 結果を Discord へ直送。単発の押下なので hs.hotkey で十分。
logmodeOnHotkey = hs.hotkey.bind({}, LOGMODE_ON_KEY, function()
    post("/api/remote-logmode?state=on")
end)
logmodeOffHotkey = hs.hotkey.bind({}, LOGMODE_OFF_KEY, function()
    post("/api/remote-logmode?state=off")
end)

-- ── 自動音声検出 (VAD) ON = F16 / OFF = F17 ──
-- ON の間は声を検知して自動で録音開始・無音で停止。
vadOnHotkey = hs.hotkey.bind({}, VAD_ON_KEY, function()
    post("/api/remote-vad?state=on")
end)
vadOffHotkey = hs.hotkey.bind({}, VAD_OFF_KEY, function()
    post("/api/remote-vad?state=off")
end)
