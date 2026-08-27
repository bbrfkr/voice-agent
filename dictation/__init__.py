"""アクティブウィンドウへ音声入力するディクテーションクライアント。

マイク → 無音区切り（VAD）→ voice-agent サーバの `/api/transcribe`（faster-whisper）
→ アクティブウィンドウへ Unicode 打鍵、という一方向のパイプライン。

サーバ（FastAPI + faster-whisper）は WSL2/Docker のままでよく、**このクライアントだけ
デスクトップ OS 側（Windows / macOS）で動かす**。キー入力の注入はアクティブウィンドウを
持つ OS 上のプロセスからしか行えないため。
"""
