"use strict";

const TIER_NAMES = ["Direct media", "yt-dlp", "HTML parser", "Browser sniffer"];
const STATUS_LABELS = {
  idle: "idle",
  analyzing: "analyzing",
  ready: "ready",
  queued: "queued",
  downloading: "downloading",
  merging: "merging",
  done: "done",
  failed: "failed",
};

const els = {
  form: document.getElementById("analyze-form"),
  url: document.getElementById("url"),
  analyzeBtn: document.getElementById("analyze-btn"),
  analyzeLabel: document.querySelector("#analyze-btn .btn-label"),
  cookiesFile: document.getElementById("cookies-file"),
  cookiePicker: document.getElementById("cookie-picker"),
  cookieStatus: document.getElementById("cookie-status"),
  limitNote: document.getElementById("limit-note"),
  capabilities: document.getElementById("capabilities"),
  jobState: document.getElementById("job-state"),
  stageLine: document.getElementById("stage-line"),
  jobId: document.getElementById("job-id"),
  tierTrack: document.getElementById("tier-track"),
  activeTierLabel: document.getElementById("active-tier-label"),
  sourceList: document.getElementById("source-list"),
  selectionSummary: document.getElementById("selection-summary"),
  downloadBtn: document.getElementById("download-btn"),
  errorBox: document.getElementById("error-box"),
  errorCode: document.getElementById("error-code"),
  errorMessage: document.getElementById("error-message"),
  errorHint: document.getElementById("error-hint"),
  errorDismiss: document.getElementById("error-dismiss"),
  log: document.getElementById("log"),
  logCount: document.getElementById("log-count"),
  progressBlock: document.getElementById("progress-block"),
  progressStage: document.getElementById("progress-stage"),
  progressPercent: document.getElementById("progress-percent"),
  progressTrack: document.getElementById("progress-track"),
  progressFill: document.getElementById("progress-fill"),
  progressBytes: document.getElementById("progress-bytes"),
  progressSpeed: document.getElementById("progress-speed"),
  progressEta: document.getElementById("progress-eta"),
  resultPanel: document.getElementById("result-panel"),
  resultMeta: document.getElementById("result-meta"),
  resultLink: document.getElementById("result-link"),
};

const state = {
  jobId: null,
  es: null,
  pollTimer: null,
  selectedSourceId: null,
  cookiesId: null,
  sourceSignature: "",
  logsRendered: 0,
  logsJobId: null,
  lastStatus: null,
  lastJob: null,
  localError: false,
};

function init() {
  els.form.addEventListener("submit", startAnalysis);
  els.downloadBtn.addEventListener("click", requestDownload);
  els.errorDismiss.addEventListener("click", () => {
    hideError();
    state.localError = false;
  });
  els.cookiesFile.addEventListener("change", uploadCookies);
  loadHealth();
  setStatus("idle");
}

async function api(path, options = {}) {
  const requestOptions = { ...options, headers: { ...(options.headers || {}) } };
  if (typeof requestOptions.body === "string") {
    requestOptions.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, requestOptions);
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const message =
      data && typeof data.detail === "string"
        ? data.detail
        : `Request failed with HTTP ${response.status}.`;
    const error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

async function startAnalysis(event) {
  event.preventDefault();
  const url = els.url.value.trim();
  if (!url) {
    els.url.focus();
    return;
  }

  resetJobView();
  setStatus("analyzing");
  setAnalyzeBusy(true);
  els.stageLine.textContent = "Sending the URL to the local engine…";
  els.jobId.textContent = "job starting";
  try {
    const job = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({ url, cookies_id: state.cookiesId }),
    });
    state.jobId = job.id;
    els.jobId.textContent = `job ${job.id.slice(0, 8)}`;
    renderJob(job);
    connectStream(job.id);
  } catch (error) {
    setStatus("failed");
    els.stageLine.textContent = "Analysis could not start.";
    setAnalyzeBusy(false);
    showRequestError(error);
  }
}

async function requestDownload() {
  if (!state.jobId || !state.selectedSourceId) return;
  els.downloadBtn.disabled = true;
  els.downloadBtn.textContent = "Starting…";
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/download`, {
      method: "POST",
      body: JSON.stringify({ source_id: state.selectedSourceId }),
    });
    renderJob(job);
    if (!state.es && !state.pollTimer) {
      connectStream(state.jobId);
    }
  } catch (error) {
    showRequestError(error);
    if (state.lastJob) syncDownloadButton(state.lastJob);
  }
}

function connectStream(jobId) {
  closeStream();
  if (!("EventSource" in window)) {
    startPolling(jobId);
    return;
  }
  const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
  state.es = source;
  source.onmessage = (event) => {
    let job;
    try {
      job = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!job || job.id !== state.jobId) return;
    renderJob(job);
    if (job.status === "done" || job.status === "failed") {
      closeStream();
    }
  };
  source.onerror = () => {
    if (!state.es) return;
    if (state.lastStatus === "done" || state.lastStatus === "failed") {
      closeStream();
      return;
    }
    closeStream();
    if (state.jobId) startPolling(state.jobId);
  };
}

function closeStream() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
}

function startPolling(jobId) {
  stopPolling();
  const poll = async () => {
    if (!state.jobId) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      renderJob(job);
      if (!["done", "failed"].includes(job.status)) {
        state.pollTimer = setTimeout(poll, 900);
      } else {
        state.pollTimer = null;
      }
    } catch (error) {
      if (error.status === 404) {
        showRequestError(error);
        state.pollTimer = null;
        return;
      }
      state.pollTimer = setTimeout(poll, 1500);
    }
  };
  poll();
}

function stopPolling() {
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function renderJob(job) {
  if (!job || (state.jobId && job.id !== state.jobId)) return;
  state.lastJob = job;
  state.lastStatus = job.status;
  if (!state.jobId) state.jobId = job.id;

  document.title = job.title ? `${job.title} · UVD` : "Universal Video Downloader";
  els.stageLine.textContent = job.stage || STATUS_LABELS[job.status] || job.status;
  els.jobId.textContent = `job ${job.id.slice(0, 8)}`;
  setStatus(job.status);

  if (job.active_tier !== null && job.active_tier !== undefined) {
    els.activeTierLabel.textContent = `tier ${job.active_tier} · ${TIER_NAMES[job.active_tier] || ""}`;
  } else if (job.winning_tier !== null && job.winning_tier !== undefined) {
    els.activeTierLabel.textContent = `tier ${job.winning_tier} found it`;
  } else {
    els.activeTierLabel.textContent = "tier —";
  }

  updateTierTrack(job);
  renderLogs(job);
  renderSources(job);
  renderProgress(job);
  renderError(job);
  renderResult(job);
  setAnalyzeBusy(job.status === "analyzing");

  if (job.status === "done" || job.status === "failed") {
    closeStream();
    stopPolling();
  }
}

function setStatus(status) {
  els.jobState.dataset.state = status;
  els.jobState.textContent = STATUS_LABELS[status] || status;
}

function updateTierTrack(job) {
  const items = els.tierTrack.querySelectorAll("li");
  items.forEach((item, index) => {
    item.classList.toggle("is-active", Boolean(job) && job.active_tier === index);
    item.classList.toggle("is-won", Boolean(job) && job.winning_tier === index);
    item.classList.toggle(
      "is-failed",
      Boolean(job) && job.status === "failed" && job.active_tier === index
    );
  });
}

function renderLogs(job) {
  const logs = job.logs || [];
  if (state.logsJobId !== job.id) {
    state.logsJobId = job.id;
    state.logsRendered = 0;
    els.log.replaceChildren();
  }
  if (logs.length < state.logsRendered) {
    state.logsRendered = 0;
    els.log.replaceChildren();
  }
  if (state.logsRendered === 0) {
    els.log.replaceChildren();
  }

  const nearBottom =
    els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 70;
  const fragment = document.createDocumentFragment();
  for (let index = state.logsRendered; index < logs.length; index += 1) {
    fragment.appendChild(makeLogLine(logs[index]));
  }
  const addedCount = fragment.childNodes.length;
  if (addedCount) {
    els.log.appendChild(fragment);
    state.logsRendered = logs.length;
    if (nearBottom || state.logsRendered === addedCount) {
      els.log.scrollTop = els.log.scrollHeight;
    }
  }
  els.logCount.textContent = `${logs.length} ${logs.length === 1 ? "event" : "events"}`;
}

function makeLogLine(entry) {
  const line = document.createElement("div");
  const level = ["info", "success", "warning", "error"].includes(entry.level)
    ? entry.level
    : "info";
  line.className = `log-line level-${level}`;

  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = formatTime(entry.ts);

  const tier = document.createElement("span");
  tier.className = "log-tier";
  if (typeof entry.tier === "number") {
    tier.textContent = `T${entry.tier}`;
    tier.classList.add(`t${entry.tier}`);
    tier.title = entry.tier_name || TIER_NAMES[entry.tier] || "";
  } else {
    tier.textContent = "··";
  }

  const message = document.createElement("span");
  message.className = "log-msg";
  message.textContent = entry.message || "";

  line.append(time, tier, message);
  return line;
}

function renderSources(job) {
  const sources = job.sources || [];
  const signature = sources.map((source) => source.id).join(",");

  if (signature && signature !== state.sourceSignature) {
    state.sourceSignature = signature;
    const validSelection = sources.some((source) => source.id === state.selectedSourceId);
    if (!validSelection) {
      state.selectedSourceId =
        job.selected_source_id && sources.some((source) => source.id === job.selected_source_id)
          ? job.selected_source_id
          : sources[0].id;
    }
    const fragment = document.createDocumentFragment();
    sources.forEach((source) => fragment.appendChild(makeSourceCard(source)));
    els.sourceList.replaceChildren(fragment);
  }

  if (!sources.length && signature !== state.sourceSignature) {
    state.sourceSignature = "";
    els.sourceList.replaceChildren(
      makeEmptyState(
        job.status === "failed" ? "No source survived." : "No candidates yet",
        job.status === "failed"
          ? "The reason is shown in the alert above. The log has the full trace."
          : "The log reports every tier it tries, including which one found the video."
      )
    );
  }

  updateSelectionVisuals();
  updateSourceInteractivity(job);
  syncSelectionSummary(job);
  syncDownloadButton(job);
}

function updateSourceInteractivity(job) {
  const locked = job.status !== "ready";
  const cards = els.sourceList.querySelectorAll(".source-card");
  cards.forEach((card) => {
    card.classList.toggle("is-locked", locked);
    const radio = card.querySelector(".source-radio");
    if (radio) radio.disabled = locked;
  });
}

function makeSourceCard(source) {
  const card = document.createElement("label");
  card.className = "source-card";
  card.dataset.sourceId = source.id;

  const radio = document.createElement("input");
  radio.type = "radio";
  radio.className = "source-radio";
  radio.name = "source";
  radio.value = source.id;
  radio.checked = source.id === state.selectedSourceId;
  radio.addEventListener("change", () => {
    if (!radio.checked) return;
    state.selectedSourceId = source.id;
    updateSelectionVisuals();
    if (state.lastJob) {
      syncSelectionSummary(state.lastJob);
      syncDownloadButton(state.lastJob);
    }
  });

  const radioUi = document.createElement("span");
  radioUi.className = "radio-ui";
  radioUi.setAttribute("aria-hidden", "true");

  const content = document.createElement("span");
  content.className = "source-content";

  const top = document.createElement("span");
  top.className = "source-top";
  const name = document.createElement("span");
  name.className = "source-label";
  name.textContent = source.label || "Video source";
  top.appendChild(name);

  const tierChip = document.createElement("span");
  tierChip.className = `chip tier-${source.tier}`;
  tierChip.textContent = `T${source.tier} · ${source.tier_name || TIER_NAMES[source.tier] || ""}`;
  top.appendChild(tierChip);

  const kindChip = document.createElement("span");
  kindChip.className = "chip";
  kindChip.textContent = source.kind_name || source.kind || "media";
  top.appendChild(kindChip);

  if (source.quality) {
    const qualityChip = document.createElement("span");
    qualityChip.className = "chip";
    qualityChip.textContent = source.quality;
    top.appendChild(qualityChip);
  }

  if (source.drm) {
    const drmChip = document.createElement("span");
    drmChip.className = "chip drm";
    drmChip.textContent = "DRM";
    top.appendChild(drmChip);
  }

  const meta = document.createElement("span");
  meta.className = "source-meta";
  const size = document.createElement("span");
  size.textContent = formatBytes(source.filesize);
  meta.appendChild(size);
  const format = document.createElement("span");
  format.textContent = (source.ext || "mp4").replace(".", "").toUpperCase();
  meta.appendChild(format);
  if (source.duration) {
    const duration = document.createElement("span");
    duration.textContent = formatDuration(source.duration);
    meta.appendChild(duration);
  }
  if (source.kind === "hls" || source.kind === "dash") {
    const adaptive = document.createElement("span");
    adaptive.textContent = "adaptive stream";
    meta.appendChild(adaptive);
  }

  const url = document.createElement("span");
  url.className = "source-url";
  url.textContent = source.url;
  url.title = source.url;

  content.append(top, meta, url);
  card.append(radio, radioUi, content);
  card.classList.toggle("is-selected", source.id === state.selectedSourceId);
  return card;
}

function updateSelectionVisuals() {
  const cards = els.sourceList.querySelectorAll(".source-card");
  cards.forEach((card) => {
    const selected = card.dataset.sourceId === state.selectedSourceId;
    card.classList.toggle("is-selected", selected);
    const radio = card.querySelector(".source-radio");
    if (radio) radio.checked = selected;
  });
}

function syncSelectionSummary(job) {
  const sources = job.sources || [];
  const selected = sources.find((source) => source.id === state.selectedSourceId);
  if (job.status === "done") {
    els.selectionSummary.textContent = "Download complete. The file is ready below.";
    return;
  }
  if (job.status === "failed") {
    els.selectionSummary.textContent = "No file was produced for this job.";
    return;
  }
  if (["queued", "downloading", "merging"].includes(job.status)) {
    els.selectionSummary.textContent = selected
      ? `Working on: ${selected.label}${selected.filesize ? ` · ${formatBytes(selected.filesize)}` : ""}`
      : "Preparing the download…";
    return;
  }
  if (job.status === "ready" && selected) {
    els.selectionSummary.textContent = `${sources.length} candidate${
      sources.length === 1 ? "" : "s"
    } · selected ${selected.label} · ${formatBytes(selected.filesize)}`;
    return;
  }
  if (job.status === "ready") {
    els.selectionSummary.textContent = "Select a source to download.";
    return;
  }
  if (job.status === "analyzing") {
    els.selectionSummary.textContent = "Working through Tiers 0–3 in order.";
    return;
  }
  els.selectionSummary.textContent = "Waiting for a URL.";
}

function syncDownloadButton(job) {
  let label = "Download MP4";
  if (job.status === "queued") label = "Queued…";
  else if (job.status === "downloading" || job.status === "merging") label = "Downloading…";
  else if (job.status === "done") label = "Downloaded";
  else if (job.status === "failed") label = "Download failed";
  els.downloadBtn.textContent = label;
  els.downloadBtn.disabled = job.status !== "ready" || !state.selectedSourceId;
}

function renderProgress(job) {
  const visible =
    ["queued", "downloading", "merging", "done"].includes(job.status) ||
    (job.status === "failed" && job.progress > 0);
  els.progressBlock.hidden = !visible;
  if (!visible) return;

  const total = job.total_bytes;
  const known = Boolean(total && total > 0);
  const percent = job.status === "done" ? 100 : Math.min(99, Number(job.progress) || 0);
  const indeterminate =
    !known && (job.status === "downloading" || job.status === "merging");

  els.progressFill.classList.toggle("is-indeterminate", indeterminate);
  els.progressFill.style.width = indeterminate ? "" : `${percent}%`;
  els.progressPercent.textContent = indeterminate ? "live" : `${Math.round(percent)}%`;
  els.progressStage.textContent = job.stage || "Working…";
  els.progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
  els.progressBytes.textContent = known
    ? `${formatBytes(job.downloaded_bytes)} / ${formatBytes(total)}`
    : `${formatBytes(job.downloaded_bytes)} downloaded`;
  els.progressSpeed.textContent = job.speed_bps ? `${formatBytes(job.speed_bps)}/s` : "—";

  if (job.status === "done") {
    els.progressEta.textContent = "saved";
  } else if (known && job.speed_bps > 0 && job.downloaded_bytes < total) {
    const seconds = (total - job.downloaded_bytes) / job.speed_bps;
    els.progressEta.textContent = `${formatDuration(seconds)} left`;
  } else if (job.status === "merging") {
    els.progressEta.textContent = "muxing";
  } else {
    els.progressEta.textContent = "—";
  }
}

function renderError(job) {
  if (job.status === "failed" && job.error) {
    showErrorBox(job.error.code, job.error.message, job.error.hint);
  } else if (!state.localError) {
    hideError();
  }
}

function renderResult(job) {
  if (job.status === "done" && job.download_url) {
    els.resultPanel.hidden = false;
    els.resultMeta.textContent = `${job.filename || "video.mp4"} · ${formatBytes(job.file_size)}`;
    els.resultLink.href = job.download_url;
    els.resultLink.setAttribute("download", job.filename || "video.mp4");
  } else {
    els.resultPanel.hidden = true;
  }
}

function showRequestError(error) {
  const data = error.data || {};
  state.localError = true;
  showErrorBox(
    data.code || "request_error",
    typeof data.detail === "string" ? data.detail : error.message,
    data.hint
  );
}

function showErrorBox(code, message, hint) {
  els.errorCode.textContent = String(code || "error").toUpperCase();
  els.errorMessage.textContent = message || "Something went wrong.";
  els.errorHint.textContent = hint || "";
  els.errorHint.hidden = !hint;
  els.errorBox.hidden = false;
}

function hideError() {
  els.errorBox.hidden = true;
}

function resetJobView() {
  closeStream();
  stopPolling();
  state.jobId = null;
  state.selectedSourceId = null;
  state.sourceSignature = "";
  state.logsRendered = 0;
  state.logsJobId = null;
  state.lastStatus = null;
  state.lastJob = null;
  state.localError = false;

  els.log.replaceChildren();
  const waiting = document.createElement("div");
  waiting.className = "log-waiting";
  const caret = document.createElement("span");
  caret.className = "log-caret";
  caret.textContent = "▌";
  waiting.append(caret, document.createTextNode(" Connecting to the extractor…"));
  els.log.appendChild(waiting);
  els.logCount.textContent = "0 events";

  els.sourceList.replaceChildren(makeEmptyState());
  els.selectionSummary.textContent = "Waiting for the engine…";
  els.downloadBtn.disabled = true;
  els.downloadBtn.textContent = "Download MP4";
  els.progressBlock.hidden = true;
  els.resultPanel.hidden = true;
  els.errorBox.hidden = true;
  els.activeTierLabel.textContent = "tier —";
  updateTierTrack(null);
  document.title = "Universal Video Downloader";
}

function makeEmptyState(
  title = "No candidates yet",
  copy = "The log reports every tier it tries, including which one found the video."
) {
  const wrapper = document.createElement("div");
  wrapper.className = "empty";
  const graphic = document.createElement("div");
  graphic.className = "empty-graphic";
  graphic.setAttribute("aria-hidden", "true");
  graphic.innerHTML =
    '<span>[</span><svg viewBox="0 0 42 42" fill="none"><path d="M12 11.5v19l16-9.5-16-9.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><span>]</span>';
  const heading = document.createElement("h3");
  heading.textContent = title;
  const text = document.createElement("p");
  text.textContent = copy;
  wrapper.append(graphic, heading, text);
  return wrapper;
}

function setAnalyzeBusy(busy) {
  els.analyzeBtn.disabled = busy;
  els.analyzeLabel.textContent = busy ? "Analyzing…" : "Analyze";
}

async function uploadCookies() {
  const file = els.cookiesFile.files && els.cookiesFile.files[0];
  if (!file) {
    state.cookiesId = null;
    els.cookieStatus.textContent = "no cookies attached";
    els.cookieStatus.classList.remove("is-attached");
    els.cookiePicker.classList.remove("has-file");
    return;
  }
  els.cookieStatus.textContent = "uploading…";
  els.cookieStatus.classList.remove("is-attached");
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/api/cookies", { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(
        typeof data.detail === "string" ? data.detail : "The cookies file was rejected."
      );
      error.data = data;
      throw error;
    }
    state.cookiesId = data.cookies_id;
    els.cookieStatus.textContent = `${data.cookie_count} cookies attached`;
    els.cookieStatus.classList.add("is-attached");
    els.cookiePicker.classList.add("has-file");
    state.localError = false;
    hideError();
  } catch (error) {
    state.cookiesId = null;
    els.cookieStatus.textContent = "cookies rejected";
    els.cookiePicker.classList.remove("has-file");
    showRequestError(error);
  }
}

async function loadHealth() {
  try {
    const data = await api("/api/health");
    const engine = data.engine || {};
    setCapability("ytdlp", Boolean(engine.yt_dlp), engine.yt_dlp ? "ready" : "missing", engine.yt_dlp || "");
    setCapability("ffmpeg", Boolean(engine.ffmpeg), engine.ffmpeg ? "ready" : "missing", engine.ffmpeg_path || "");
    setCapability(
      "browser",
      Boolean(engine.playwright_package && engine.playwright_enabled),
      engine.playwright_package && engine.playwright_enabled ? "ready" : "missing",
      engine.playwright_package ? "Chromium is downloaded separately" : "playwright package not installed"
    );
    const limits = data.limits || {};
    if (limits.max_file_size_mb) {
      const hours = limits.max_duration_seconds
        ? Math.round((limits.max_duration_seconds / 3600) * 10) / 10
        : "—";
      els.limitNote.textContent = `LIMITS ${limits.max_file_size_mb} MB / ${hours} h`;
    }
  } catch {
    /* Health is decorative; failures do not block the page. */
  }
}

function setCapability(name, ok, label, title) {
  const element = els.capabilities.querySelector(`[data-cap="${name}"]`);
  if (!element) return;
  element.classList.toggle("is-ok", ok);
  element.classList.toggle("is-warn", !ok);
  const value = element.querySelector("b");
  if (value) value.textContent = label;
  element.title = title || "";
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes <= 0) return "unknown size";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const digits = index === 0 ? 0 : size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  }
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatTime(ts) {
  if (!ts) return "--:--:--";
  try {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "--:--:--";
  }
}

init();
