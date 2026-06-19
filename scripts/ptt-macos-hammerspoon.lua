-- voice-agent — グローバル プッシュトゥトーク（macOS / Hammerspoon）
--
-- 既定キー（F8）を「押している間だけ」録音する。ブラウザのタブが背面でも効く。
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
local PTT_KEY = "f8"                    -- PTT キー

local function ptt(state)
    -- 非同期 POST。応答は使わないので無視する。
    hs.http.asyncPost(BASE .. "/api/remote-ptt?state=" .. state, "", nil, function() end)
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
