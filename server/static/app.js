/* ずんだもん音声エージェント Web UI。
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

let ws = null;
let mediaStream = null;
let recorder = null;
let recChunks = [];
let recording = false;
let expectingAudio = false; // 直前の tts ヘッダに続く binary フレームを待っているか

// ───────── WebSocket ─────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => setStatus("接続済み（マイク待機）", "ok");
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
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "config", speaker: Number(speakerEl.value), speed: Number(speedEl.value) }));
  }
}
speakerEl.addEventListener("change", sendConfig);
speedEl.addEventListener("change", sendConfig);

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

connect();
