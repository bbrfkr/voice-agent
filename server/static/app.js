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
const clearEl = document.getElementById("clear");
const vadModeEl = document.getElementById("vad-mode");
const vadThresholdEl = document.getElementById("vad-threshold");
const vadSilenceEl = document.getElementById("vad-silence");
const volumeMeterContainer = document.getElementById("volume-meter-container");
const volumeMeterBar = document.getElementById("volume-meter-bar");

let ws = null;
let mediaStream = null;
let recorder = null;
let recChunks = [];
let recording = false;
let expectingAudio = false; // 直前の tts ヘッダに続く binary フレームを待っているか

let vadEnabled = false;
let vadInterval = null;
let vadSource = null;
let vadAnalyser = null;
let isSpeaking = false;
let silenceStartTime = null;

// VAD パラメータは UI から調整でき、localStorage に保存する（下記は既定値）。
let VAD_THRESHOLD = 0.015; // 検出のしきい値（RMS）
let VAD_SILENCE_DURATION = 1200; // ms 無音が続いたら録音停止
const VAD_STORAGE_KEY = "voice-agent-vad-enabled";
const VAD_THRESHOLD_STORAGE_KEY = "voice-agent-vad-threshold";
const VAD_SILENCE_STORAGE_KEY = "voice-agent-vad-silence";

// UI 状態の保存キー（ログモード・話者・話速）と、会話を引き継ぐためのセッションID。
const LOGMODE_STORAGE_KEY = "voice-agent-logmode";
const SPEAKER_STORAGE_KEY = "voice-agent-speaker";
const SPEED_STORAGE_KEY = "voice-agent-speed";
const SESSION_STORAGE_KEY = "voice-agent-session-id";

// セッションIDを localStorage に保持する。これをサーバへ渡すと、リロード後も
// サーバ側の会話履歴（LLM の文脈）と表示ログを同じセッションから引き継げる。
function getSessionId() {
  let sid = localStorage.getItem(SESSION_STORAGE_KEY);
  if (!sid) {
    sid = (crypto.randomUUID && crypto.randomUUID()) ||
      String(Date.now()) + Math.random().toString(16).slice(2);
    localStorage.setItem(SESSION_STORAGE_KEY, sid);
  }
  return sid;
}

// ───────── 複数タブの調停（アクティブタブ選出） ─────────
// 同じ sid を複数タブで開くと、各タブが個別にマイクを監視し（VAD やリモート PTT で）
// 同じ発話を二重に拾って AI へ二重リクエストしてしまう。これを防ぐため BroadcastChannel で
// 「アクティブな 1 タブ」を選び、録音・送信はそのタブだけが担う。アクティブ権は
// 「最後に表示/操作したタブ」が持つ（見ているタブで操作する直感に合わせる）。
const TAB_ID = (crypto.randomUUID && crypto.randomUUID()) || (Date.now() + "." + Math.random());
const pttLabelEl = document.querySelector(".ptt-label");
let isActiveTab = true; // このタブが録音・送信を担当するか（単独タブ想定の既定は true）
let suppressSend = false; // 非アクティブ化で進行中の録音を破棄するためのフラグ
let leaderChan = null;
let leaderId = TAB_ID; // 既知のリーダーのタブID
let leaderTs = 0; // そのリーダーが主張した時刻
let lastLeaderSeen = 0;

function onActiveTabChange(active) {
  if (pttLabelEl) {
    pttLabelEl.textContent = active ? "押している間だけ話す" : "別タブが受け持ち中（押すと切替）";
  }
  pttEl.classList.toggle("passive", !active);
  if (active) {
    updateMicMonitoring(); // VAD が有効ならマイク監視を再開
  } else {
    if (vadInterval) { clearInterval(vadInterval); vadInterval = null; }
    if (recording) { suppressSend = true; stopRecording(); } // 録音中なら送らず破棄
    releaseMic();
  }
}

function applyActive(active) {
  if (isActiveTab === active) return;
  isActiveTab = active;
  onActiveTabChange(active);
}

// このタブをアクティブ（録音・送信担当）として主張する。
function claimLeadership() {
  leaderTs = Date.now();
  leaderId = TAB_ID;
  lastLeaderSeen = leaderTs;
  if (leaderChan) leaderChan.postMessage({ type: "claim", id: TAB_ID, ts: leaderTs });
  applyActive(true);
}

function setupTabLeadership() {
  if (!("BroadcastChannel" in window)) { applyActive(true); return; }
  leaderChan = new BroadcastChannel("voice-agent-leader-" + getSessionId());

  leaderChan.onmessage = (ev) => {
    const m = ev.data || {};
    if (m.type === "claim") {
      lastLeaderSeen = Date.now();
      // より新しい主張（時刻優先、同時刻は ID 比較）が来たらそのタブをリーダーにする。
      if (m.ts > leaderTs || (m.ts === leaderTs && m.id > leaderId)) {
        leaderTs = m.ts; leaderId = m.id;
        applyActive(leaderId === TAB_ID);
      } else if (leaderId === TAB_ID) {
        leaderChan.postMessage({ type: "claim", id: TAB_ID, ts: leaderTs }); // 自分が新しいので再主張
      }
    } else if (m.type === "resign") {
      if (m.id === leaderId) { // リーダーが離脱：表示中のタブが引き継ぐ
        leaderId = ""; leaderTs = 0;
        if (!document.hidden) claimLeadership();
      }
    } else if (m.type === "whois") {
      if (leaderId === TAB_ID) leaderChan.postMessage({ type: "claim", id: TAB_ID, ts: leaderTs });
    }
  };

  // 表示/フォーカスでアクティブ権を取りに行く（見ているタブが担当する）。
  window.addEventListener("focus", () => { if (!document.hidden) claimLeadership(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) claimLeadership(); });
  window.addEventListener("pagehide", () => {
    if (leaderChan) leaderChan.postMessage({ type: "resign", id: TAB_ID });
  });

  // リーダーは定期的に存在を主張。フォロワーは一定時間聞こえなければ引き継ぐ（クラッシュ対策）。
  setInterval(() => {
    if (leaderId === TAB_ID) {
      if (leaderChan) leaderChan.postMessage({ type: "claim", id: TAB_ID, ts: leaderTs });
    } else if (Date.now() - lastLeaderSeen > 5000 && !document.hidden) {
      claimLeadership();
    }
  }, 2000);

  // 起動時：表示中なら主張、非表示なら問い合わせ、しばらく返答が無ければ主張する。
  leaderChan.postMessage({ type: "whois", id: TAB_ID });
  if (!document.hidden) {
    claimLeadership();
  } else {
    leaderId = ""; lastLeaderSeen = Date.now();
    applyActive(false);
    setTimeout(() => { if (leaderId === "") claimLeadership(); }, 800);
  }
}

// ───────── WebSocket ─────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?sid=${encodeURIComponent(getSessionId())}`);
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
    case "history":
      // サーバが保持していた過去の会話を再生して見た目を復元する。
      // 再接続のたびに届くため、いったんクリアしてから描き直し重複を防ぐ。
      logEl.innerHTML = "";
      streamingEl = null;
      for (const e of (msg.events || [])) handleEvent(e);
      break;
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
      else if (msg.status === "progress") addBubble("sys", formatProgress(msg));
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
      // リモート PTT は全タブにブロードキャストされるため、アクティブなタブだけが反応する。
      if (!isActiveTab) break;
      if (msg.action === "start") {
        startRecording();
      } else if (msg.action === "stop") {
        stopRecording();
      }
      break;
    case "remote_logmode":
      if (msg.action === "on") setLogmode(true);
      else if (msg.action === "off") setLogmode(false);
      else setLogmode(!logmodeEl.checked); // toggle
      break;
    case "remote_vad":
      if (msg.action === "on") setVad(true);
      else if (msg.action === "off") setVad(false);
      else setVad(!vadEnabled); // toggle
      break;
    case "turn_end":
      setStatus("接続済み（マイク待機）", "ok");
      break;
  }
}

// 作業（[[TASK]]）中の opencode 進捗（ツール利用など）を1行に整形する。
function formatProgress(msg) {
  const icon = msg.state === "completed" ? "✅" : msg.state === "error" ? "⚠️" : "⚙️";
  const label = { running: "実行中", completed: "完了", error: "失敗" }[msg.state] || msg.state;
  const detail = (msg.title || "").trim();
  const tool = msg.tool || "tool";
  return `${icon} ${tool}（${label}）${detail ? ": " + detail : ""}`;
}

// ───────── Markdown レンダリング ─────────
// AI の発話は markdown で返ってくるため、marked で HTML 化し、DOMPurify で
// サニタイズしてから表示する（パーサ・サニタイザとも実績あるライブラリに委ねる）。
// 万一スクリプト読み込みに失敗した場合は、素のテキストへ安全にフォールバックする。
if (window.marked && window.marked.setOptions) {
  window.marked.setOptions({ gfm: true, breaks: true });
}
// リンクは別タブで開き、タブナビング対策として rel を付ける。
if (window.DOMPurify && window.DOMPurify.addHook) {
  window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
}
function renderMarkdown(src) {
  const text = src || "";
  if (!window.marked || !window.DOMPurify) {
    // ライブラリ未ロード時のフォールバック（HTML エスケープした素テキスト）。
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }
  const html = window.marked.parse(text);
  return window.DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
}

// ───────── 会話ログ表示 ─────────
let streamingEl = null;
let streamingRaw = ""; // ストリーミング中の生 markdown（毎 delta で再レンダリングするため保持）
function addBubble(kind, text) {
  const el = document.createElement("div");
  el.className = `bubble ${kind}`;
  // AI の発話だけ markdown として解釈する。ユーザー発話（STT）やシステム通知は素のテキスト。
  if (kind === "ai") {
    el.classList.add("md");
    el.innerHTML = renderMarkdown(text);
  } else {
    el.textContent = text;
  }
  logEl.appendChild(el);
  logEl.scrollTop = logEl.scrollHeight;
  return el;
}
function appendStreaming(delta) {
  if (!streamingEl) { streamingEl = addBubble("ai", ""); streamingRaw = ""; }
  streamingRaw += delta;
  streamingEl.innerHTML = renderMarkdown(streamingRaw);
  logEl.scrollTop = logEl.scrollHeight;
}
function finalizeStreaming(text) {
  if (streamingEl) streamingEl.innerHTML = renderMarkdown(text);
  else addBubble("ai", text);
  streamingEl = null;
  streamingRaw = "";
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

// マイクを解放する（全 track を stop して mediaStream を破棄）。
// スマホ（特に Android Chrome）はマイク取得中にエコーキャンセルが有効だと OS が
// 「通信モード」に入り、TTS 再生まで通話音声ストリームに乗る＝通話音量を参照してしまう。
// PTT では録音が終わったらマイクを手放すことで、再生時はマイク非アクティブとなり
// 通信モードに入らず、メディア音量で再生される。
// ただし VAD モードは常時マイク監視が必要なので、その間は解放しない。
function releaseMic() {
  if (vadEnabled && isActiveTab) return; // VAD 監視中（かつアクティブ）はマイクを握り続ける
  if (vadSource) {
    try { vadSource.disconnect(); } catch (e) {}
    vadSource = null;
  }
  if (mediaStream) {
    for (const track of mediaStream.getTracks()) {
      try { track.stop(); } catch (e) {}
    }
    mediaStream = null;
  }
}

async function startRecording() {
  if (recording) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!(await ensureMic())) return;
  suppressSend = false;

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
  // 非アクティブ化で中断された録音は送信せず破棄する（タブ間の二重送信防止）。
  if (suppressSend) {
    suppressSend = false;
    recChunks = [];
    releaseMic();
    setStatus("接続済み（マイク待機）", "ok");
    return;
  }
  const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
  // 録音データ確定後にマイクを手放す（再生をメディア音量にするため）。VAD 中は解放されない。
  releaseMic();
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
speakerEl.addEventListener("change", () => {
  localStorage.setItem(SPEAKER_STORAGE_KEY, speakerEl.value);
  sendConfig();
});
speedEl.addEventListener("change", () => {
  localStorage.setItem(SPEED_STORAGE_KEY, speedEl.value);
  sendConfig();
});

// 話速を localStorage から復元（話者は一覧取得後に loadSpeakers で復元する）。
const savedSpeed = parseFloat(localStorage.getItem(SPEED_STORAGE_KEY));
if (!Number.isNaN(savedSpeed)) speedEl.value = String(savedSpeed);

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
    // 前回選んだ話者があればそれを、無ければ既定ID（さらに無ければ先頭）を選ぶ。
    // 選択値はサーバへ同期しておく。
    const saved = localStorage.getItem(SPEAKER_STORAGE_KEY);
    const def = String(data.default);
    if (saved && list.some((s) => String(s.id) === saved)) {
      speakerEl.value = saved;
    } else {
      speakerEl.value = list.some((s) => String(s.id) === def) ? def : String(list[0].id);
    }
    sendConfig();
  } catch (e) {
    speakerEl.innerHTML = '<option value="">話者一覧を取得できません</option>';
  }
}
loadSpeakers();

// ───────── 履歴クリア ─────────
// サーバ側の会話履歴・表示ログ・opencode セッションを破棄し、画面も空にする。
// サーバは空の history を返すので、ブラウザの表示はそれでクリアされる。
clearEl.addEventListener("click", () => {
  if (!confirm("会話履歴を消去します。よろしいですか？")) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "clear" }));
  } else {
    logEl.innerHTML = "";
    streamingEl = null;
  }
});

// ───────── ログモード切り替え ─────────
// チェックボックスの状態を変え、グローバルホットキー等からの切り替えを画面にも反映する。
function setLogmode(on) {
  if (logmodeEl.checked === on) return;
  logmodeEl.checked = on;
  localStorage.setItem(LOGMODE_STORAGE_KEY, String(on));
  addBubble("sys", on ? "📝 ログモード ON（Discord へ直送）" : "💬 通常モード（会話）");
}

// ログモードの状態を localStorage から復元し、ユーザ操作も保存する。
logmodeEl.checked = localStorage.getItem(LOGMODE_STORAGE_KEY) === "true";
logmodeEl.addEventListener("change", () => {
  localStorage.setItem(LOGMODE_STORAGE_KEY, String(logmodeEl.checked));
});

// ───────── 自動音声検出 (VAD) 切り替え ─────────
// チェックボックスと内部状態を変え、グローバルホットキー等からの切り替えを画面にも反映する。
function setVad(on) {
  if (vadEnabled === on) return;
  vadEnabled = on;
  vadModeEl.checked = on;
  localStorage.setItem(VAD_STORAGE_KEY, vadEnabled);
  updateMicMonitoring();
  addBubble("sys", on ? "🎙️ 自動音声検出 (VAD) ON" : "🔇 自動音声検出 (VAD) OFF");
}

// ───────── PTT 入力（ポインタ / キーボード） ─────────
pttEl.disabled = false;
pttEl.addEventListener("pointerdown", (e) => { e.preventDefault(); claimLeadership(); startRecording(); });
pttEl.addEventListener("pointerup", (e) => { e.preventDefault(); stopRecording(); });
pttEl.addEventListener("pointerleave", () => stopRecording());
pttEl.addEventListener("pointercancel", () => stopRecording());

window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && e.target.tagName !== "INPUT") { e.preventDefault(); claimLeadership(); startRecording(); }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space" && e.target.tagName !== "INPUT") { e.preventDefault(); stopRecording(); }
});

// ───────── マイク監視制御 (VAD) ─────────
async function updateMicMonitoring() {
  if (!vadEnabled || !isActiveTab) {
    if (vadInterval) {
      clearInterval(vadInterval);
      vadInterval = null;
    }
    updateVolumeMeter(0);
    releaseMic(); // VAD を切ったらマイクを手放し、再生をメディア音量に戻す
    return;
  }

  // マイクの初期化
  if (!(await ensureMic())) {
    console.warn("マイクの初期化に失敗しました。");
    return;
  }

  // 音声自動検出 (VAD) の監視を開始
  if (vadInterval) {
    clearInterval(vadInterval);
    vadInterval = null;
  }
  setupVadLoop();
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
    if (!vadEnabled || !isActiveTab) return;
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
  // しきい値の縦線位置を現在のしきい値に合わせる（メーターは rms 0.1 で 100%）。
  const thresholdPos = Math.min(100, (VAD_THRESHOLD / 0.1) * 100);
  volumeMeterContainer.style.setProperty("--threshold-pos", `${thresholdPos}%`);
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
const savedThreshold = parseFloat(localStorage.getItem(VAD_THRESHOLD_STORAGE_KEY));
if (!Number.isNaN(savedThreshold)) VAD_THRESHOLD = savedThreshold;
const savedSilence = parseFloat(localStorage.getItem(VAD_SILENCE_STORAGE_KEY));
if (!Number.isNaN(savedSilence)) VAD_SILENCE_DURATION = savedSilence;

vadModeEl.checked = vadEnabled;
vadThresholdEl.value = String(VAD_THRESHOLD);
vadSilenceEl.value = String(VAD_SILENCE_DURATION / 1000);

vadModeEl.addEventListener("change", () => {
  setVad(vadModeEl.checked);
});

// しきい値・無音停止時間は録音中でも即時反映（VAD ループが毎ティック参照する）。
vadThresholdEl.addEventListener("change", () => {
  const v = parseFloat(vadThresholdEl.value);
  if (!Number.isNaN(v)) {
    VAD_THRESHOLD = v;
    localStorage.setItem(VAD_THRESHOLD_STORAGE_KEY, String(v));
  }
});
vadSilenceEl.addEventListener("change", () => {
  const sec = parseFloat(vadSilenceEl.value);
  if (!Number.isNaN(sec)) {
    VAD_SILENCE_DURATION = Math.round(sec * 1000);
    localStorage.setItem(VAD_SILENCE_STORAGE_KEY, String(VAD_SILENCE_DURATION));
  }
});

// 背面タブでも VAD を動かすため、可視状態が戻ったら AudioContext を起こし直す。
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && vadEnabled && audioCtx && audioCtx.state === "suspended") {
    audioCtx.resume();
  }
});

// タブ間調停を開始（これで isActiveTab が確定）→ 初期監視開始。
setupTabLeadership();
updateMicMonitoring();

connect();

// ───────── PWA: Service Worker 登録 ─────────
// シェル（HTML/CSS/JS/アイコン）をキャッシュして起動を速くし、インストール可能にする。
// セキュアコンテキスト（https / localhost）でしか登録できない点に注意。
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((e) => {
      console.warn("Service Worker 登録に失敗:", e);
    });
  });
}
