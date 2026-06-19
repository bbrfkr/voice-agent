/* VOICEVOX 音声エージェント Web UI。
 *
 * 役割:
 *   - プッシュトゥトーク（ボタン押下中 / スペースキー押下中）でマイク録音（MediaRecorder）。
 *   - 離したら録音 blob を WebSocket でサーバへ送り、STT→LLM→TTS のイベントを受け取る。
 *   - サーバから届く TTS の wav を AudioContext で順番に再生（旧 Speaker のキュー再生を移植）。
 *   - 再生中に押し直したら即停止＋cancel 送信（バージイン）。
 */

const statusEl = document.getElementById("status");
const logEl = document.getElementById("log");
const pttEl = document.getElementById("ptt");
const logmodeEl = document.getElementById("logmode");
const speakerEl = document.getElementById("speaker");
const speedEl = document.getElementById("speed");
const vadModeEl = document.getElementById("vad-mode");
const hwMuteModeEl = document.getElementById("hw-mute-mode");
const volumeMeterContainer = document.getElementById("volume-meter-container");
const volumeMeterBar = document.getElementById("volume-meter-bar");

let ws = null;
let mediaStream = null;
let recorder = null;
let recChunks = [];
let recording = false;
let expectingAudio = false; // 直前の tts ヘッダに続く binary フレームを待っているか

let vadEnabled = false;
let hwMuteEnabled = false;
let vadInterval = null;
let vadSource = null;
let vadAnalyser = null;
let isSpeaking = false;
let silenceStartTime = null;

const VAD_THRESHOLD = 0.015; // 検出のしきい値
const VAD_SILENCE_DURATION = 1200; // ms 無音が続いたら録音停止
const VAD_STORAGE_KEY = "voice-agent-vad-enabled";
const HWMUTE_STORAGE_KEY = "voice-agent-hwmute-enabled";

// ───────── WebSocket ─────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => { setStatus("接続済み（マイク待機）", "ok"); sendConfig(); };
  ws.onclose = () => { setStatus("切断。再接続します…", "err"); setTimeout(connect, 1500); };
  ws.onerror = () => setStatus("WebSocket エラー", "err");

  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      handleEvent(JSON.parse(ev.data));
    } else if (expectingAudio) {
      expectingAudio = false;
      enqueueAudio(ev.data); // ArrayBuffer（wav）
    }
  };
}

function handleEvent(msg) {
  switch (msg.type) {
    case "stt":
      if (msg.text && msg.text.trim()) addBubble("user", msg.text);
      else addBubble("sys", "（聞き取れませんでした）");
      break;
    case "llm_delta":
      appendStreaming(msg.text);
      break;
    case "assistant":
      finalizeStreaming(msg.text + (msg.interrupted ? "（割り込みで中断）" : ""));
      break;
    case "task":
      if (msg.status === "delegating") addBubble("sys", `🛠️ 作業委譲: ${msg.instruction}`);
      else if (msg.status === "error") addBubble("sys", `⚠️ 作業中にエラー: ${msg.message}`);
      break;
    case "log_saved":
      addBubble("sys", `📝 Discord へ直送: ${msg.text}`);
      break;
    case "error":
      addBubble("sys", `⚠️ ${msg.message}`);
      break;
    case "tts":
      expectingAudio = true; // 次の binary フレームが wav
      break;
    case "remote_ptt":
      if (msg.action === "start") {
        startRecording();
      } else if (msg.action === "stop") {
        stopRecording();
      }
      break;
    case "turn_end":
      setStatus("接続済み（マイク待機）", "ok");
      break;
  }
}

// ───────── 会話ログ表示 ─────────
let streamingEl = null;
function addBubble(kind, text) {
  const el = document.createElement("div");
  el.className = `bubble ${kind}`;
  el.textContent = text;
  logEl.appendChild(el);
  logEl.scrollTop = logEl.scrollHeight;
  return el;
}
function appendStreaming(delta) {
  if (!streamingEl) streamingEl = addBubble("ai", "");
  streamingEl.textContent += delta;
  logEl.scrollTop = logEl.scrollHeight;
}
function finalizeStreaming(text) {
  if (streamingEl) streamingEl.textContent = text;
  else addBubble("ai", text);
  streamingEl = null;
}

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

// ───────── TTS 再生キュー（AudioContext） ─────────
let audioCtx = null;
const playQueue = [];
let playing = false;
let playGen = 0; // バージインで世代を進め、古い再生を無効化する

function enqueueAudio(arrayBuffer) {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const gen = playGen;
  audioCtx.decodeAudioData(arrayBuffer.slice(0)).then((buf) => {
    if (gen !== playGen) return; // 既にバージインで破棄された
    playQueue.push(buf);
    if (!playing) playNext();
  }).catch(() => {});
}
function playNext() {
  if (playQueue.length === 0) { playing = false; return; }
  playing = true;
  const buf = playQueue.shift();
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  src.onended = () => { if (playing) playNext(); };
  src._gen = playGen;
  src.start();
  currentSource = src;
}
let currentSource = null;
function stopPlayback() {
  playGen++;
  playQueue.length = 0;
  playing = false;
  if (currentSource) { try { currentSource.stop(); } catch (e) {} currentSource = null; }
}

// ───────── 録音（プッシュトゥトーク） ─────────
function pickMime() {
  const cands = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  for (const m of cands) if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  return "";
}

async function ensureMic() {
  if (mediaStream) return true;
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return true;
  } catch (e) {
    setStatus("マイクが使えません（HTTPS / localhost が必要）", "err");
    return false;
  }
}

async function startRecording() {
  if (recording) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!(await ensureMic())) return;

  // 再生中ならバージイン：停止して cancel を送る
  stopPlayback();
  ws.send(JSON.stringify({ type: "cancel" }));

  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();

  recChunks = [];
  const mime = pickMime();
  recorder = new MediaRecorder(mediaStream, mime ? { mimeType: mime } : undefined);
  recorder.ondataavailable = (e) => { if (e.data.size > 0) recChunks.push(e.data); };
  recorder.onstop = onRecStop;
  recorder.start();
  recording = true;
  pttEl.classList.add("recording");
  setStatus("聞いています…", "busy");
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  pttEl.classList.remove("recording");
  if (recorder && recorder.state !== "inactive") recorder.stop();

  // VADステートのリセット
  isSpeaking = false;
  silenceStartTime = null;
}

async function onRecStop() {
  const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
  if (blob.size < 1200) { setStatus("接続済み（マイク待機）", "ok"); return; } // 短すぎ＝押し損ね
  const mode = logmodeEl.checked ? "log" : "chat";
  ws.send(JSON.stringify({ type: "utterance", mode }));
  ws.send(await blob.arrayBuffer());
  setStatus("考えています…", "busy");
}

// ───────── 設定（話者 / 話速） ─────────
function sendConfig() {
  if (ws && ws.readyState === WebSocket.OPEN && speakerEl.value !== "") {
    ws.send(JSON.stringify({ type: "config", speaker: Number(speakerEl.value), speed: Number(speedEl.value) }));
  }
}
speakerEl.addEventListener("change", sendConfig);
speedEl.addEventListener("change", sendConfig);

// 話者ドロップダウンを VOICEVOX の一覧（人が読めるラベル）で組み立てる。
// 数値の話者IDをそのまま見せず、「話者名（スタイル名）」から選べるようにする。
async function loadSpeakers() {
  try {
    const res = await fetch("/api/speakers");
    const data = await res.json();
    const list = data.speakers || [];
    if (list.length === 0) throw new Error("空の話者一覧");
    speakerEl.innerHTML = "";
    for (const s of list) {
      const opt = document.createElement("option");
      opt.value = String(s.id);
      opt.textContent = s.label;
      speakerEl.appendChild(opt);
    }
    // 既定の話者IDがあれば選択（無ければ先頭）。選択値をサーバへ同期しておく。
    const def = String(data.default);
    speakerEl.value = list.some((s) => String(s.id) === def) ? def : String(list[0].id);
    sendConfig();
  } catch (e) {
    speakerEl.innerHTML = '<option value="">話者一覧を取得できません</option>';
  }
}
loadSpeakers();

// ───────── PTT 入力（ポインタ / キーボード） ─────────
pttEl.disabled = false;
pttEl.addEventListener("pointerdown", (e) => { e.preventDefault(); startRecording(); });
pttEl.addEventListener("pointerup", (e) => { e.preventDefault(); stopRecording(); });
pttEl.addEventListener("pointerleave", () => stopRecording());
pttEl.addEventListener("pointercancel", () => stopRecording());

window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && e.target.tagName !== "INPUT") { e.preventDefault(); startRecording(); }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space" && e.target.tagName !== "INPUT") { e.preventDefault(); stopRecording(); }
});

// ───────── マイク監視制御 (VAD & 物理ミュート連動) ─────────
async function updateMicMonitoring() {
  const needsMic = vadEnabled || hwMuteEnabled;
  if (!needsMic) {
    if (vadInterval) {
      clearInterval(vadInterval);
      vadInterval = null;
    }
    if (mediaStream) {
      const track = mediaStream.getAudioTracks()[0];
      if (track) {
        track.removeEventListener("mute", onTrackMute);
        track.removeEventListener("unmute", onTrackUnmute);
      }
    }
    updateVolumeMeter(0);
    return;
  }

  // マイクの初期化
  if (!(await ensureMic())) {
    console.warn("マイクの初期化に失敗しました。");
    return;
  }

  const track = mediaStream.getAudioTracks()[0];
  if (!track) return;

  // 1. 物理ミュート連動の監視設定
  track.removeEventListener("mute", onTrackMute);
  track.removeEventListener("unmute", onTrackUnmute);
  if (hwMuteEnabled) {
    track.addEventListener("mute", onTrackMute);
    track.addEventListener("unmute", onTrackUnmute);
    console.log("物理ミュート連動アクティブ。現在のミュート状態:", track.muted);
  }

  // 2. 音声自動検出 (VAD) の監視設定
  if (vadInterval) {
    clearInterval(vadInterval);
    vadInterval = null;
  }
  if (vadEnabled) {
    setupVadLoop();
  } else {
    updateVolumeMeter(0);
  }
}

function onTrackMute() {
  console.log("マイクがミュートされました。録音を停止します。");
  if (hwMuteEnabled) {
    stopRecording();
  }
}

function onTrackUnmute() {
  console.log("マイクミュートが解除されました。録音を開始します。");
  if (hwMuteEnabled) {
    startRecording();
  }
}

function setupVadLoop() {
  if (!mediaStream) return;
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioCtx.state === "suspended") {
    audioCtx.resume();
  }

  try {
    if (vadSource) {
      vadSource.disconnect();
    }
    vadSource = audioCtx.createMediaStreamSource(mediaStream);
    vadAnalyser = audioCtx.createAnalyser();
    vadAnalyser.fftSize = 256;
    vadSource.connect(vadAnalyser);
  } catch (e) {
    console.error("VADノード作成失敗:", e);
    return;
  }

  const bufferLength = vadAnalyser.fftSize;
  const dataArray = new Float32Array(bufferLength);
  isSpeaking = false;
  silenceStartTime = null;

  vadInterval = setInterval(() => {
    if (!vadEnabled) return;
    vadAnalyser.getFloatTimeDomainData(dataArray);

    let sum = 0;
    for (let i = 0; i < bufferLength; i++) {
      sum += dataArray[i] * dataArray[i];
    }
    const rms = Math.sqrt(sum / bufferLength);

    updateVolumeMeter(rms);

    if (rms > VAD_THRESHOLD) {
      silenceStartTime = null;
      if (!isSpeaking && !recording) {
        isSpeaking = true;
        console.log(`VAD: 発話を検出しました (RMS: ${rms.toFixed(4)})`);
        startRecording();
      }
    } else {
      if (isSpeaking && recording) {
        if (silenceStartTime === null) {
          silenceStartTime = Date.now();
        } else if (Date.now() - silenceStartTime > VAD_SILENCE_DURATION) {
          isSpeaking = false;
          silenceStartTime = null;
          console.log("VAD: 無音時間を検知したため録音を停止します。");
          stopRecording();
        }
      }
    }
  }, 100);
}

function updateVolumeMeter(rms) {
  if (!volumeMeterContainer || !volumeMeterBar) return;
  if (!vadEnabled) {
    volumeMeterContainer.style.display = "none";
    return;
  }
  volumeMeterContainer.style.display = "block";
  const percentage = Math.min(100, (rms / 0.1) * 100);
  volumeMeterBar.style.width = `${percentage}%`;

  if (rms > VAD_THRESHOLD) {
    volumeMeterBar.style.backgroundColor = "#5fb35f"; // 緑
  } else {
    volumeMeterBar.style.backgroundColor = "#e5c07b"; // 黄
  }
}

// UIイベントの紐付けと localStorage からの復元
vadEnabled = localStorage.getItem(VAD_STORAGE_KEY) === "true";
hwMuteEnabled = localStorage.getItem(HWMUTE_STORAGE_KEY) === "true";

vadModeEl.checked = vadEnabled;
hwMuteModeEl.checked = hwMuteEnabled;

vadModeEl.addEventListener("change", () => {
  vadEnabled = vadModeEl.checked;
  localStorage.setItem(VAD_STORAGE_KEY, vadEnabled);
  updateMicMonitoring();
});

hwMuteModeEl.addEventListener("change", () => {
  hwMuteEnabled = hwMuteModeEl.checked;
  localStorage.setItem(HWMUTE_STORAGE_KEY, hwMuteEnabled);
  updateMicMonitoring();
});

// 初期監視開始
updateMicMonitoring();

connect();
