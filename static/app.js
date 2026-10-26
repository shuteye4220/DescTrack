"use strict";

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
const OFFSET_MIN = -600000;
const OFFSET_MAX = 600000;

const state = {
  pairs: [],            // [{id, videoPath, audioPath, offsetMs, offsetUserModified, autoSyncPromise, previewPositionValue, previewPositionUserModified}]
  activePairId: null,   // which pair's files/offset the (single, shared) mixer dialog is currently editing
  audioCtx: null,
  activeSources: [],
  loggedMilestones: new Set(),
  mixerClosedViaLock: false,
  batchRunning: false,
  lastOutputPath: null,
};

let _pairIdSeq = 0;
const pairEls = new Map(); // pair id -> DOM refs for that row

// ---------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------
const el = {
  statusBanner: document.getElementById("status-banner"),
  pairsList: document.getElementById("pairs-list"),
  pairRowTemplate: document.getElementById("pair-row-template"),
  btnAddPair: document.getElementById("btn-add-pair"),

  mixerDialog: document.getElementById("mixer-dialog"),
  mixerDialogSubtitle: document.getElementById("mixer-dialog-subtitle"),
  previewPosition: document.getElementById("preview-position"),
  previewDuration: document.getElementById("preview-duration"),
  offsetSpin: document.getElementById("offset-spin"),
  offsetTyped: document.getElementById("offset-typed"),
  btnAutoSync: document.getElementById("btn-auto-sync"),
  btnPlayPreview: document.getElementById("btn-play-preview"),
  btnStopPreview: document.getElementById("btn-stop-preview"),
  btnLockOffset: document.getElementById("btn-lock-offset"),
  btnCloseMixer: document.getElementById("btn-close-mixer"),

  chromecast: document.getElementById("chromecast"),
  btnStart: document.getElementById("btn-start"),
  btnOpenFolder: document.getElementById("btn-open-folder"),
  batchStatus: document.getElementById("batch-status"),
  progressBar: document.getElementById("progress-bar"),
  progressText: document.getElementById("progress-text"),
  progressDetail: document.getElementById("progress-detail"),
  activityLog: document.getElementById("activity-log"),
  completeSound: document.getElementById("complete-sound"),
};

// ---------------------------------------------------------------------
// Logging (the one live-announcement channel: role="log" + aria-live="polite")
// ---------------------------------------------------------------------
function logMessage(text) {
  const p = document.createElement("p");
  const time = new Date().toLocaleTimeString();
  p.textContent = `${time}  ${text}`;
  el.activityLog.appendChild(p);
  el.activityLog.scrollTop = el.activityLog.scrollHeight;
}

// ---------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------
async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `Request to ${url} failed (${res.status}).`);
  }
  return data;
}

async function apiGet(url) {
  const res = await fetch(url);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `Request to ${url} failed (${res.status}).`);
  }
  return data;
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function basename(path) {
  if (!path) return "";
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function hasNativeFileDialogs() {
  return typeof window.pywebview !== "undefined" && window.pywebview.api;
}

// ---------------------------------------------------------------------
// Pair rows: each pair is its own video + AD audio + offset, so a batch of
// videos with different sync needs can all be queued at once. The dialog
// itself stays a single shared element (see "Sync mixer dialog" below) -
// opening it for a given row points it at that row's pair via
// state.activePairId.
// ---------------------------------------------------------------------
function hasCompletePair() {
  return state.pairs.some((p) => p.videoPath && p.audioPath);
}

function updateStartButtonState() {
  el.btnStart.disabled = state.batchRunning || !hasCompletePair();
}

function updatePairNumbering() {
  state.pairs.forEach((pair, idx) => {
    const n = idx + 1;
    const refs = pairEls.get(pair.id);
    if (!refs) return;
    refs.heading.textContent = `Pair ${n}`;
    refs.videoBtn.setAttribute("aria-label", `Choose video file — Pair ${n}`);
    refs.audioBtn.setAttribute("aria-label", `Choose audio description file — Pair ${n}`);
    refs.mixerBtn.setAttribute("aria-label", `Open sync mixer — Pair ${n}`);
    refs.removeBtn.setAttribute("aria-label", `Remove Pair ${n}`);
    refs.removeBtn.hidden = state.pairs.length <= 1;
  });
}

function updatePairReadyState(id) {
  const pair = state.pairs.find((p) => p.id === id);
  const refs = pairEls.get(id);
  if (!pair || !refs) return;
  const ready = Boolean(pair.videoPath && pair.audioPath);
  refs.mixerBtn.disabled = state.batchRunning || !ready;
  updateStartButtonState();
}

function addPair(opts = {}) {
  const { announce = true } = opts;
  const id = ++_pairIdSeq;
  const pair = {
    id,
    videoPath: null,
    audioPath: null,
    offsetMs: 0,
    offsetUserModified: false,
    autoSyncPromise: null,
    previewPositionValue: 0,
    previewPositionUserModified: false,
  };
  state.pairs.push(pair);

  const frag = el.pairRowTemplate.content.cloneNode(true);
  const row = frag.querySelector("[data-pair-row]");
  const heading = row.querySelector("[data-pair-heading]");
  const videoBtn = row.querySelector("[data-pick-video]");
  const videoDisplay = row.querySelector("[data-video-display]");
  const audioBtn = row.querySelector("[data-pick-audio]");
  const audioDisplay = row.querySelector("[data-audio-display]");
  const mixerBtn = row.querySelector("[data-open-mixer]");
  const offsetSummary = row.querySelector("[data-offset-summary]");
  const removeBtn = row.querySelector("[data-remove-pair]");
  const videoFileInput = row.querySelector("[data-video-file-input]");
  const audioFileInput = row.querySelector("[data-audio-file-input]");

  heading.id = `pair-heading-${id}`;
  videoDisplay.id = `pair-${id}-video-display`;
  audioDisplay.id = `pair-${id}-audio-display`;
  videoBtn.setAttribute("aria-describedby", videoDisplay.id);
  audioBtn.setAttribute("aria-describedby", audioDisplay.id);

  videoBtn.addEventListener("click", () => pickVideoForPair(id));
  audioBtn.addEventListener("click", () => pickAudioForPair(id));
  mixerBtn.addEventListener("click", () => openMixerForPair(id));
  removeBtn.addEventListener("click", () => removePair(id));
  videoFileInput.addEventListener("change", () => {
    const file = videoFileInput.files[0];
    if (file) setPairVideoPath(id, file.name);
  });
  audioFileInput.addEventListener("change", () => {
    const file = audioFileInput.files[0];
    if (file) setPairAudioPath(id, file.name);
  });

  pairEls.set(id, {
    row, heading, videoBtn, videoDisplay, audioBtn, audioDisplay,
    mixerBtn, offsetSummary, removeBtn, videoFileInput, audioFileInput,
  });

  el.pairsList.appendChild(row);
  updatePairNumbering();
  updateStartButtonState();

  if (announce) {
    logMessage(`Pair ${state.pairs.length} added.`);
    videoBtn.focus();
  }
  return id;
}

function removePair(id) {
  const idx = state.pairs.findIndex((p) => p.id === id);
  if (idx === -1) return;
  const removedNumber = idx + 1;
  const refs = pairEls.get(id);

  state.pairs.splice(idx, 1);
  pairEls.delete(id);
  if (refs) refs.row.remove();

  updatePairNumbering();
  updateStartButtonState();
  logMessage(`Pair ${removedNumber} removed.`);
  el.btnAddPair.focus();
}

async function pickVideoForPair(id) {
  if (hasNativeFileDialogs()) {
    try {
      const path = await window.pywebview.api.pick_video();
      if (path) setPairVideoPath(id, path);
    } catch (err) {
      logMessage("ERROR choosing video file: " + err.message);
    }
  } else {
    const refs = pairEls.get(id);
    if (refs) refs.videoFileInput.click();
  }
}

async function pickAudioForPair(id) {
  if (hasNativeFileDialogs()) {
    try {
      const path = await window.pywebview.api.pick_audio();
      if (path) setPairAudioPath(id, path);
    } catch (err) {
      logMessage("ERROR choosing audio file: " + err.message);
    }
  } else {
    const refs = pairEls.get(id);
    if (refs) refs.audioFileInput.click();
  }
}

function setPairVideoPath(id, path) {
  const pair = state.pairs.find((p) => p.id === id);
  const refs = pairEls.get(id);
  if (!pair || !refs) return;
  pair.videoPath = path;
  refs.videoDisplay.textContent = path || "No file selected";
  if (path) logMessage(`Video selected (Pair ${state.pairs.indexOf(pair) + 1}): ${path}`);
  pair.previewPositionUserModified = false;
  pair.offsetUserModified = false;
  updatePairReadyState(id);
  maybeDetectStart(id);
  pair.autoSyncPromise = maybeAutoSync(id);
}

function setPairAudioPath(id, path) {
  const pair = state.pairs.find((p) => p.id === id);
  const refs = pairEls.get(id);
  if (!pair || !refs) return;
  pair.audioPath = path;
  refs.audioDisplay.textContent = path || "No file selected";
  if (path) logMessage(`AD audio selected (Pair ${state.pairs.indexOf(pair) + 1}): ${path}`);
  pair.previewPositionUserModified = false;
  pair.offsetUserModified = false;
  updatePairReadyState(id);
  maybeDetectStart(id);
  pair.autoSyncPromise = maybeAutoSync(id);
}

// Once both files in a pair are known, look for leading silence in each
// (via ffmpeg's silencedetect) and default that pair's preview position past
// it, so the mixer doesn't start you off listening to dead air. Runs in the
// background; never blocks anything, and backs off silently on error. Only
// applies its result if the user hasn't already typed their own value for
// this pair.
async function maybeDetectStart(id) {
  const pair = state.pairs.find((p) => p.id === id);
  if (!pair || !pair.videoPath || !pair.audioPath) return;
  try {
    const result = await apiPost("/api/detect-start", {
      video_path: pair.videoPath,
      audio_path: pair.audioPath,
    });
    const stillThere = state.pairs.find((p) => p.id === id);
    if (!stillThere || pair.previewPositionUserModified) return;
    const suggested = Math.round(result.suggested_position_s);
    pair.previewPositionValue = suggested;
    if (state.activePairId === id) {
      el.previewPosition.value = suggested;
    }
    if (suggested > 0) {
      const n = state.pairs.indexOf(pair) + 1;
      logMessage(
        `Preview position auto-set to ${suggested}s for Pair ${n} to skip leading silence ` +
        `(video starts at ${result.video_start_s.toFixed(1)}s, AD audio at ${result.ad_start_s.toFixed(1)}s).`
      );
    }
  } catch (err) {
    logMessage("Could not auto-detect leading silence: " + err.message);
  }
}

// Once both files in a pair are known, also run auto-sync automatically in
// the background and apply its estimate as that pair's starting offset -
// the sync mixer becomes something you open only to fine-tune by ear (or if
// auto-sync guessed wrong), not something required for every pair. Never
// overrides an offset the user has already set by hand (nudged, typed, or
// manually re-run via the dialog's own Auto-sync button).
async function maybeAutoSync(id) {
  const pair = state.pairs.find((p) => p.id === id);
  if (!pair || !pair.videoPath || !pair.audioPath) return;
  try {
    const result = await apiPost("/api/auto-sync", {
      video_path: pair.videoPath,
      audio_path: pair.audioPath,
    });
    const stillThere = state.pairs.find((p) => p.id === id);
    if (!stillThere || pair.offsetUserModified) return;
    const clamped = clampOffset(result.offset_ms);
    applyOffsetToPair(id, clamped, { userModified: false });
    const confidencePct = Math.round(result.confidence * 100);
    const n = state.pairs.indexOf(pair) + 1;
    logMessage(
      `Auto-sync estimate for Pair ${n}: offset = ${clamped} ms (confidence ${confidencePct}%). ` +
      `Applied automatically - open the sync mixer only if it needs fine-tuning.`
    );
  } catch (err) {
    const n = state.pairs.indexOf(pair) + 1;
    logMessage(`Could not auto-sync Pair ${n}: ${err.message}`);
  }
}

// ---------------------------------------------------------------------
// Sync mixer dialog: a single shared <dialog>, pointed at whichever pair's
// "Open sync mixer" button was clicked (state.activePairId). Left/Right (or
// Up/Down) arrow keys nudge the offset spinbutton; holding a key accelerates
// through wider increments the longer it's held.
// ---------------------------------------------------------------------
function clampOffset(value) {
  return Math.max(OFFSET_MIN, Math.min(OFFSET_MAX, Math.round(value)));
}

// Applies an offset to a specific pair (not necessarily the one the dialog
// currently has open - background auto-sync needs to write into a pair
// that may not be "active"). Updates that row's on-page summary always, and
// additionally keeps the live dialog controls in sync if the dialog happens
// to be open for this exact pair right now.
function applyOffsetToPair(id, clampedValue, { userModified } = { userModified: true }) {
  const pair = state.pairs.find((p) => p.id === id);
  if (!pair) return;
  pair.offsetMs = clampedValue;
  if (userModified) pair.offsetUserModified = true;

  const refs = pairEls.get(id);
  if (refs) refs.offsetSummary.textContent = `Offset: ${clampedValue} ms`;

  if (state.activePairId === id) {
    const text = `${clampedValue} ms`;
    el.offsetSpin.textContent = text;
    el.offsetSpin.setAttribute("aria-valuenow", String(clampedValue));
    el.offsetSpin.setAttribute("aria-valuetext", `${clampedValue} milliseconds`);
    // Don't stomp on the typed field while the user is actively editing it.
    if (document.activeElement !== el.offsetTyped) {
      el.offsetTyped.value = String(clampedValue);
    }
  }
}

// Used by direct, in-dialog user interaction (spinbutton nudge, typed
// field, the dialog's own Auto-sync button) - always applies to whichever
// pair the dialog is currently open for, and always counts as a user edit.
function setOffsetMs(value) {
  if (state.activePairId == null) return;
  applyOffsetToPair(state.activePairId, clampOffset(value), { userModified: true });
}

// Syncs the dialog's controls to reflect a pair's stored offset without
// marking it as user-modified - used only when the dialog is first opened
// for a pair, so simply looking at the auto-sync estimate doesn't lock it
// in as if it had been hand-edited.
function syncDialogDisplayFromPair(pair) {
  const text = `${pair.offsetMs} ms`;
  el.offsetSpin.textContent = text;
  el.offsetSpin.setAttribute("aria-valuenow", String(pair.offsetMs));
  el.offsetSpin.setAttribute("aria-valuetext", `${pair.offsetMs} milliseconds`);
  el.offsetTyped.value = String(pair.offsetMs);
}

el.offsetTyped.addEventListener("input", () => {
  const value = Number(el.offsetTyped.value);
  if (!Number.isNaN(value)) setOffsetMs(value);
});

el.previewPosition.addEventListener("input", () => {
  const pair = state.pairs.find((p) => p.id === state.activePairId);
  if (!pair) return;
  pair.previewPositionUserModified = true;
  pair.previewPositionValue = Number(el.previewPosition.value) || 0;
});

function nudgeStepForHoldDuration(elapsedMs) {
  if (elapsedMs < 500) return 10;
  if (elapsedMs < 1500) return 50;
  if (elapsedMs < 3000) return 200;
  return 1000;
}
window.__nudgeStepForHoldDuration = nudgeStepForHoldDuration; // test hook

let holdStartTime = null;

el.offsetSpin.addEventListener("keydown", (event) => {
  const decrementKeys = ["ArrowLeft", "ArrowDown"];
  const incrementKeys = ["ArrowRight", "ArrowUp"];

  if (decrementKeys.includes(event.key) || incrementKeys.includes(event.key)) {
    event.preventDefault();
    if (!event.repeat || holdStartTime === null) {
      holdStartTime = performance.now();
    }
    const elapsed = performance.now() - holdStartTime;
    const step = nudgeStepForHoldDuration(elapsed);
    const direction = incrementKeys.includes(event.key) ? 1 : -1;
    const pair = state.pairs.find((p) => p.id === state.activePairId);
    const current = pair ? pair.offsetMs : 0;
    setOffsetMs(current + direction * step);
    return;
  }

  if (event.key === "Enter") {
    event.preventDefault();
    lockOffsetAndClose();
    return;
  }
  // Escape is handled natively by <dialog> (closes it); no special-casing needed here.
});

el.offsetSpin.addEventListener("keyup", () => {
  holdStartTime = null;
});

function openMixerForPair(id) {
  const pair = state.pairs.find((p) => p.id === id);
  if (!pair) return;
  state.activePairId = id;
  const n = state.pairs.indexOf(pair) + 1;
  el.mixerDialogSubtitle.textContent =
    `Editing sync for Pair ${n}: ${basename(pair.videoPath)} + ${basename(pair.audioPath)}`;
  el.previewPosition.value = pair.previewPositionValue || 0;
  syncDialogDisplayFromPair(pair);
  el.mixerDialog.showModal();
  el.offsetSpin.focus();
}

function lockOffsetAndClose() {
  const pair = state.pairs.find((p) => p.id === state.activePairId);
  const n = pair ? state.pairs.indexOf(pair) + 1 : "?";
  logMessage(`Sync offset locked at ${pair ? pair.offsetMs : 0} ms (Pair ${n}).`);
  state.mixerClosedViaLock = true;
  el.mixerDialog.close();
}

el.mixerDialog.addEventListener("close", () => {
  const pairId = state.activePairId;
  const pair = state.pairs.find((p) => p.id === pairId);

  if (!state.mixerClosedViaLock) {
    const n = pair ? state.pairs.indexOf(pair) + 1 : "?";
    logMessage(`Sync mixer closed (Pair ${n}).`);
  }
  state.mixerClosedViaLock = false;

  const refs = pairId != null ? pairEls.get(pairId) : null;
  if (refs) {
    refs.mixerBtn.focus();
  } else {
    el.btnAddPair.focus();
  }
  state.activePairId = null;
});

el.btnLockOffset.addEventListener("click", lockOffsetAndClose);
el.btnCloseMixer.addEventListener("click", () => el.mixerDialog.close());

// ---------------------------------------------------------------------
// Auto-sync (operates on whichever pair the dialog is currently open for)
// ---------------------------------------------------------------------
function setDialogBusy(busy) {
  el.btnAutoSync.disabled = busy;
  el.btnPlayPreview.disabled = busy;
}

async function runAutoSync() {
  const pairId = state.activePairId;
  const pair = state.pairs.find((p) => p.id === pairId);
  if (!pair) return;
  setDialogBusy(true);
  logMessage("Starting auto-sync analysis...");
  try {
    const result = await apiPost("/api/auto-sync", {
      video_path: pair.videoPath,
      audio_path: pair.audioPath,
    });
    const clamped = clampOffset(result.offset_ms);
    applyOffsetToPair(pairId, clamped, { userModified: true });
    const confidencePct = Math.round(result.confidence * 100);
    logMessage(
      `Auto-sync estimate: offset = ${clamped} ms (confidence ${confidencePct}%). ` +
      `Always confirm with the preview before rendering.`
    );
  } catch (err) {
    logMessage("ERROR during auto-sync: " + err.message);
  } finally {
    setDialogBusy(false);
  }
}

// ---------------------------------------------------------------------
// Preview playback (Web Audio API - sample-accurate scheduled start)
// ---------------------------------------------------------------------
function stopPreview() {
  for (const source of state.activeSources) {
    try {
      source.stop();
    } catch (err) {
      /* already stopped */
    }
  }
  state.activeSources = [];
  logMessage("Preview stopped.");
}

async function fetchAudioBuffer(ctx, url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`Could not fetch preview clip (${res.status}).`);
  const arrayBuffer = await res.arrayBuffer();
  return ctx.decodeAudioData(arrayBuffer);
}

async function playPreview() {
  const pair = state.pairs.find((p) => p.id === state.activePairId);
  if (!pair) return;
  setDialogBusy(true);
  try {
    stopPreview();

    const positionS = Number(el.previewPosition.value) || 0;
    const durationS = Number(el.previewDuration.value) || 15;
    const offsetMs = pair.offsetMs;

    logMessage(`Preparing preview at ${positionS}s with offset ${offsetMs} ms...`);
    const prep = await apiPost("/api/preview/prepare", {
      video_path: pair.videoPath,
      audio_path: pair.audioPath,
      position_s: positionS,
      duration_s: durationS,
      offset_ms: offsetMs,
    });

    if (!state.audioCtx) {
      state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    const ctx = state.audioCtx;
    if (ctx.state === "suspended") await ctx.resume();

    const [videoBuffer, adBuffer] = await Promise.all([
      fetchAudioBuffer(ctx, prep.video_url),
      fetchAudioBuffer(ctx, prep.ad_url),
    ]);

    const videoSource = ctx.createBufferSource();
    videoSource.buffer = videoBuffer;
    videoSource.connect(ctx.destination);

    const adSource = ctx.createBufferSource();
    adSource.buffer = adBuffer;
    adSource.connect(ctx.destination);

    // Schedule both to start at the exact same AudioContext time, so
    // alignment is sample-accurate rather than depending on JS call timing.
    const startAt = ctx.currentTime + 0.12;
    videoSource.start(startAt);
    adSource.start(startAt);
    state.activeSources = [videoSource, adSource];

    logMessage("Playing preview: video's original audio and the AD track together.");
  } catch (err) {
    logMessage("ERROR during preview: " + err.message);
  } finally {
    setDialogBusy(false);
  }
}

el.btnAutoSync.addEventListener("click", runAutoSync);
el.btnPlayPreview.addEventListener("click", playPreview);
el.btnStopPreview.addEventListener("click", stopPreview);

// ---------------------------------------------------------------------
// Batch processing: pairs are rendered one at a time (reusing the exact
// same single-job backend endpoints as before), so ffmpeg never runs more
// than one job at once and the progress section stays meaningful.
// ---------------------------------------------------------------------

// Formats a seconds count as "1:05" (or "45s" under a minute). Returns null
// for values that aren't a usable finite number, so callers can decide
// whether to show anything at all.
function formatDuration(totalSeconds) {
  if (totalSeconds === null || totalSeconds === undefined || !Number.isFinite(totalSeconds)) {
    return null;
  }
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}:${String(sec).padStart(2, "0")}` : `${sec}s`;
}

function setProgress(snapshot, label) {
  const pct = snapshot.percent;
  el.progressBar.value = pct;
  el.progressText.textContent = `${pct}%`;

  const elapsedStr = formatDuration(snapshot.elapsed_seconds);
  const etaStr = formatDuration(snapshot.eta_seconds);
  const parts = [];
  if (elapsedStr) parts.push(`Elapsed ${elapsedStr}`);
  if (etaStr) parts.push(`about ${etaStr} remaining`);
  if (snapshot.speed) parts.push(`encoding at ${snapshot.speed.toFixed(2)}x speed`);
  el.progressDetail.textContent = parts.length ? parts.join(" — ") : "";

  const milestones = [25, 50, 75];
  for (const m of milestones) {
    if (pct >= m && !state.loggedMilestones.has(m)) {
      state.loggedMilestones.add(m);
      const prefix = label ? `Rendering pair ${label}... ` : "Rendering... ";
      logMessage(`${prefix}${m}% complete${etaStr ? ` (about ${etaStr} remaining)` : ""}.`);
    }
  }
}

async function pollJobToCompletion(jobId, label) {
  for (;;) {
    let snapshot;
    try {
      snapshot = await apiGet(`/api/job/${jobId}`);
    } catch (err) {
      logMessage(`ERROR while checking render progress (pair ${label}): ${err.message}`);
      return { status: "error", error: err.message };
    }
    setProgress(snapshot, label);
    if (snapshot.status !== "running") return snapshot;
    await sleep(700);
  }
}

window.__completionSoundPlayCount = 0;
function playCompletionSound() {
  window.__completionSoundPlayCount++;
  try {
    el.completeSound.currentTime = 0;
    const playResult = el.completeSound.play();
    if (playResult && typeof playResult.catch === "function") {
      playResult.catch(() => { /* autoplay restrictions, etc. - non-fatal */ });
    }
  } catch (err) {
    /* non-fatal - the log message and on-screen status already convey completion */
  }
}

function setBatchBusy(busy) {
  state.batchRunning = busy;
  el.btnAddPair.disabled = busy;
  el.chromecast.disabled = busy;
  el.btnStart.disabled = busy || !hasCompletePair();
  for (const pair of state.pairs) {
    const refs = pairEls.get(pair.id);
    if (!refs) continue;
    refs.videoBtn.disabled = busy;
    refs.audioBtn.disabled = busy;
    refs.removeBtn.disabled = busy;
    refs.mixerBtn.disabled = busy || !(pair.videoPath && pair.audioPath);
  }
}

async function startBatch() {
  if (state.batchRunning) return;

  const readyPairs = state.pairs.filter((p) => p.videoPath && p.audioPath);
  if (readyPairs.length === 0) {
    logMessage("Cannot start: at least one pair needs both a video and an AD audio file chosen.");
    return;
  }
  const skipped = state.pairs.length - readyPairs.length;
  if (skipped > 0) {
    logMessage(`${skipped} incomplete pair(s) will be skipped (missing a video or AD audio file).`);
  }

  // Make sure each pair's background auto-sync (run automatically as soon as
  // both its files were chosen) has actually resolved before we lock in the
  // offsets that will be used for rendering - otherwise a pair added and
  // started in quick succession could render at the default 0 ms.
  await Promise.all(readyPairs.map((pair) => pair.autoSyncPromise || Promise.resolve()));

  const chromecast = el.chromecast.checked;

  // Pre-flight: resolve every output path and check for existing files up
  // front, so the whole batch can run unattended afterward instead of
  // blocking partway through on a confirm() dialog nobody's there to answer.
  let outputInfos;
  try {
    outputInfos = await Promise.all(
      readyPairs.map((pair) => apiPost("/api/output-path", { video_path: pair.videoPath, chromecast }))
    );
  } catch (err) {
    logMessage("ERROR checking output paths: " + err.message);
    return;
  }
  // Two kinds of conflict to disclose up front, in one pass, so nothing gets
  // silently clobbered partway through an unattended batch: an output that
  // already exists on disk, and an output path used by more than one pair
  // in *this* batch (e.g. the same source video picked twice) - in that
  // second case each pair still renders, but only the last one to finish
  // survives, so it needs the same explicit go-ahead.
  const conflicts = [];
  outputInfos.forEach((info) => {
    if (info.exists) conflicts.push(`${info.output_path} (already exists)`);
  });
  const pathCounts = new Map();
  outputInfos.forEach((info) => pathCounts.set(info.output_path, (pathCounts.get(info.output_path) || 0) + 1));
  for (const [path, count] of pathCounts) {
    if (count > 1) conflicts.push(`${path} (used by ${count} pairs in this batch - only the last will be kept)`);
  }

  if (conflicts.length > 0) {
    const shown = conflicts.slice(0, 10).join("\n");
    const more = conflicts.length > 10 ? `\n...and ${conflicts.length - 10} more` : "";
    const proceed = window.confirm(
      `${conflicts.length} output conflict(s) found:\n\n${shown}${more}\n\nContinue anyway?`
    );
    if (!proceed) {
      logMessage("Batch cancelled: output conflicts were found and overwrite was declined.");
      return;
    }
  }

  setBatchBusy(true);
  let succeeded = 0;
  let failed = 0;

  for (let i = 0; i < readyPairs.length; i++) {
    const pair = readyPairs[i];
    const pairNumber = state.pairs.indexOf(pair) + 1;
    const label = `${i + 1}/${readyPairs.length}`;

    el.batchStatus.textContent = `Processing pair ${label} (Pair ${pairNumber}): ${basename(pair.videoPath)}`;
    logMessage(
      `Starting pair ${label} (Pair ${pairNumber}): ${pair.videoPath} + ${pair.audioPath} ` +
      `(${chromecast ? "H.264/MP4 for Chromecast" : "original format preserved"})...`
    );

    el.progressBar.value = 0;
    el.progressText.textContent = "0%";
    el.progressDetail.textContent = "";
    state.loggedMilestones.clear();

    let jobId;
    try {
      const created = await apiPost("/api/create", {
        video_path: pair.videoPath,
        audio_path: pair.audioPath,
        offset_ms: pair.offsetMs,
        chromecast,
      });
      jobId = created.job_id;
    } catch (err) {
      failed++;
      logMessage(`ERROR starting pair ${label} (Pair ${pairNumber}): ${err.message}`);
      continue;
    }

    const snapshot = await pollJobToCompletion(jobId, label);

    if (snapshot.status === "done") {
      succeeded++;
      state.lastOutputPath = snapshot.output_path;
      el.btnOpenFolder.disabled = false;
      logMessage(`Done (Pair ${pairNumber}). Saved to: ${snapshot.output_path}`);
      playCompletionSound();
    } else if (snapshot.status === "cancelled") {
      failed++;
      logMessage(`Cancelled (Pair ${pairNumber}).`);
    } else {
      failed++;
      logMessage(`ERROR (Pair ${pairNumber}): ${snapshot.error}`);
    }
  }

  const summary = `Batch complete: ${succeeded} of ${readyPairs.length} succeeded${failed ? `, ${failed} failed` : ""}.`;
  el.batchStatus.textContent = summary;
  logMessage(summary);

  setBatchBusy(false);
}

async function openOutputFolder() {
  if (!state.lastOutputPath) return;
  try {
    await apiPost("/api/reveal", { path: state.lastOutputPath });
  } catch (err) {
    logMessage("Could not open output folder: " + err.message);
  }
}

el.btnAddPair.addEventListener("click", () => addPair());
el.btnStart.addEventListener("click", startBatch);
el.btnOpenFolder.addEventListener("click", openOutputFolder);

// ---------------------------------------------------------------------
// Startup: ffmpeg/ffprobe availability check
// ---------------------------------------------------------------------
async function checkStatus() {
  try {
    const status = await apiGet("/api/status");
    if (!status.ffmpeg_found || !status.ffprobe_found) {
      el.statusBanner.hidden = false;
      el.statusBanner.textContent =
        "ffmpeg and/or ffprobe could not be found. Place a portable copy of both next to the app, " +
        "or install ffmpeg so it is on your PATH. See README.md for details.";
    } else if (status.ffmpeg_outdated) {
      el.statusBanner.hidden = false;
      el.statusBanner.textContent =
        `Your ffmpeg is version ${status.ffmpeg_version}. Versions older than ` +
        `${status.ffmpeg_recommended_version} have known security bugs that a booby-trapped video ` +
        "file could trigger. Please update ffmpeg (see README.md). DescTrack will still work.";
    }
  } catch (err) {
    el.statusBanner.hidden = false;
    el.statusBanner.textContent = "Could not reach the DescTrack backend: " + err.message;
  }
}

// ---------------------------------------------------------------------
// Test-only hook: lets automated tests exercise the real app logic with
// real filesystem paths without driving an actual OS file-picker dialog
// (which a plain browser cannot do, by design - see README's testing
// notes). Fills the existing pair rows first (the page always starts with
// one empty pair), adding more rows as needed for extra entries.
// ---------------------------------------------------------------------
window.__setPairsForTesting = (pairsArray) => {
  pairsArray.forEach((entry, i) => {
    const id = i < state.pairs.length ? state.pairs[i].id : addPair({ announce: false });
    if (entry.video) setPairVideoPath(id, entry.video);
    if (entry.audio) setPairAudioPath(id, entry.audio);
  });
};

// ---------------------------------------------------------------------
// Init: seed the page with one empty pair (mirrors the always-visible
// video/audio pickers from earlier versions), then check backend status.
// ---------------------------------------------------------------------
addPair({ announce: false });
checkStatus();
