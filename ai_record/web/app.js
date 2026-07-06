/* ai-record — front-end controller (vanilla JS, no deps).
 *
 * Responsibilities:
 *   - Read the per-launch token from the URL and attach it to every REST + WS call.
 *   - Drive four surfaces: consent modal, preflight, compact bar, expanded view.
 *   - Maintain a live transcript from the WebSocket (STT-first, patched in place).
 *   - Bind a settings drawer to GET/PUT /api/settings and the secrets endpoints.
 *
 * The whole UI is one window; "expand"/"collapse" swaps views + asks pywebview
 * (if present) to resize. It never opens a second window.
 */
(() => {
  "use strict";

  /* ============================ AUTH / TOKEN ============================ */
  const TOKEN = new URLSearchParams(window.location.search).get("token");
  const HEADER = "X-AI-Record-Token";

  /* ============================ APP STATE ============================ */
  const state = {
    settings: null,          // last redacted settings object
    consentOk: false,
    recording: false,
    ephemeral: false,        // "Không lưu" mode: nothing is written to disk
    sessionId: null,
    lastSeq: 0,              // highest durable seq seen (for WS catch-up)
    utterances: new Map(),   // seq -> {record, el}
    autoScroll: true,
    searchTerm: "",
    ws: null,
    wsBackoff: 500,
    wsTimer: null,
    everConnected: false,
    languages: [],
    rediarizeTimer: null,
    modelCatalog: null,      // {default, models, current, installed, ollama_available}
    // --- three-view switcher (Transcript | Summary | Analyze) ---
    tab: "transcript",       // 'transcript' | 'summary' | 'analyze'
    transcriptView: "dialogue", // 'dialogue' (speaker rows) | 'plain' (selectable text block)
    summaryResult: null,     // cached summarize payload (scenario reformat)
    analyzeResult: null,     // cached summarize payload (scenario analyze)
    // --- new: expanded-view content mode + saved-session browsing ---
    uiMode: "browser",       // 'browser' | 'transcript'
    openedSessionId: null,   // a saved session opened for viewing (vs. the live one)
    devices: null,           // {inputs, outputs, available} from /api/audio-devices
    inputSel: null,          // selected input option value ("off" | "id:<id>")
    outputSel: null,         // selected output option value
    sessionsCache: [],       // last /api/sessions payload (for client-side filtering)
    browserSearch: "",
  };

  const MAX_ROWS = 500;      // cap DOM nodes for very long transcripts
  const UNKNOWN_SPEAKER = "Speaker ?";
  // The Summary/Analyze tabs each map to a fixed summarize scenario; provider comes
  // from settings. reformat = verbatim grouping, analyze = general analysis + critique.
  const TAB_SCENARIO = { summary: "reformat", analyze: "analyze" };
  const SUMMARY_PROVIDERS = [
    { value: "claude_cli", label: "Claude CLI" },
    { value: "codex_cli", label: "Codex CLI" },
    { value: "gemini", label: "Gemini" },
    { value: "ollama", label: "Ollama" },
  ];

  // Default instruction prompts for the two editable AI actions. These MUST stay in
  // sync with DEFAULT_SUMMARY_SCENARIOS["reformat"|"analyze"] in ai_record/config.py —
  // they back the "Khôi phục mặc định" (reset) buttons in Settings and are only used
  // when the server hasn't given us a value.
  const DEFAULT_SUMMARY_PROMPTS = {
    reformat:
      "You are a meticulous transcript editor. You are given a raw MEETING / VOICE " +
      "transcript that may mix Vietnamese, English, and Japanese. Your ONLY job is to " +
      "reorganize it so it is easier to read — never to rewrite it.\n\n" +
      "Hard rules (a downstream integrity check rejects any violation):\n" +
      "- Preserve every utterance's wording VERBATIM. Do NOT translate, paraphrase, " +
      "correct, summarize, add, remove, merge, split, or reorder the words a speaker said.\n" +
      "- Keep each original speaker label and its [timestamp] exactly as given, attached " +
      "to the same words.\n" +
      "- Every original utterance MUST still appear, in full, in your output.\n\n" +
      "What you MAY do:\n" +
      "- Group consecutive, related utterances into thematic sections.\n" +
      "- Add Markdown structure ONLY: `##` headings that name each topic, and bullet " +
      "points that lay out the utterances beneath them.\n\n" +
      "Output Markdown only — no preamble, no closing commentary.",
    analyze:
      "You are a sharp, careful meeting analyst. You are given a MEETING / VOICE " +
      "transcript that may mix Vietnamese, English, and Japanese. Read and understand " +
      "the whole thing before you write.\n\n" +
      "Produce a GENERAL ANALYSIS (not a plain restatement), written in Vietnamese, as " +
      "skimmable Markdown with EXACTLY these four sections in this order:\n" +
      "## Tổng quan — 2–3 câu: đây là cuộc trao đổi gì, ai tham gia (nếu rõ), bối cảnh " +
      "và mục đích.\n" +
      "## Các điểm/chủ đề chính — gạch đầu dòng những luận điểm, chủ đề và quyết định " +
      "quan trọng nhất, nhóm theo chủ đề.\n" +
      "## Tóm tắt cô đọng — vài câu văn xuôi cô đọng lại nội dung cốt lõi.\n" +
      "## Câu hỏi phản biện / Gợi ý / Rủi ro — 3 đến 6 gạch đầu dòng nêu câu hỏi phản " +
      "biện, điểm còn mơ hồ, rủi ro cần lưu ý, hoặc gợi ý cho bước tiếp theo.\n\n" +
      "Constraints: base EVERYTHING strictly on what the transcript actually says. Do " +
      "NOT invent facts, names, numbers, decisions, or claims that are not present. If " +
      "something is unclear or missing, say so instead of guessing. Output Markdown only.",
  };

  /* ============================ DOM SHORTCUTS ============================ */
  const $ = (id) => document.getElementById(id);
  const el = {
    app: $("app"),
    // compact
    cToggle: $("c-toggle"), cEphemeral: $("c-ephemeral"),
    cDot: $("c-dot"), cStatusText: $("c-status-text"),
    cStatus: $("c-status"), cRecent: $("c-recent"), cExpand: $("c-expand"),
    cInput: $("c-input"), cOutputDev: $("c-output-dev"), cScreen: $("c-screen"),
    cTranslate: $("c-translate"), cFolder: $("c-folder"), cExit: $("c-exit"), cSettings: $("c-settings"),
    // expanded
    expanded: $("expanded"),
    xCollapse: $("x-collapse"), xTitle: $("x-title"), xToggle: $("x-toggle"),
    xEphemeral: $("x-ephemeral"),
    xDot: $("x-dot"), xStatusText: $("x-status-text"), xStatus: $("x-status"),
    xChips: $("x-chips"), xSearch: $("x-search"), xSettings: $("x-settings"),
    xExit: $("x-exit"),
    xTranscript: $("x-transcript"), xTranscriptPlain: $("x-transcript-plain"),
    xViewmode: $("x-viewmode"), xJump: $("x-jump"),
    xInput: $("x-input"), xOutputDev: $("x-output-dev"), xScreen: $("x-screen"),
    xTranslate: $("x-translate"),
    // browser mode
    browserSearch: $("browser-search"), sessionBrowser: $("session-browser"),
    backSessions: $("back-sessions"),
    // transcript-mode actions + three-view tabs
    transcriptPanel: $("transcript-panel"),
    tabTranscript: $("tab-transcript"), tabSummary: $("tab-summary"), tabAnalyze: $("tab-analyze"),
    copyTranscript: $("copy-transcript"), cCopy: $("c-copy"),
    openFolder: $("open-folder"),
    toolStatus: $("tool-status"),
    // overlays
    consent: $("consent"), consentAgree: $("consent-agree"),
    preflight: $("preflight"), pfRows: $("pf-rows"), pfPreset: $("pf-preset"),
    pfContinue: $("pf-continue"), pfRefresh: $("pf-refresh"),
    settings: $("settings"), setClose: $("set-close"), setBody: $("set-body"),
    confirm: $("confirm"), confirmMsg: $("confirm-msg"),
    confirmOk: $("confirm-ok"), confirmCancel: $("confirm-cancel"),
    noToken: $("no-token"),
    notices: $("notices"),
  };

  /* ============================ REST HELPERS ============================ */
  async function api(path, { method = "GET", body } = {}) {
    const opts = { method, headers: { [HEADER]: TOKEN || "" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (!res.ok) {
      const err = new Error(`${method} ${path} -> ${res.status}`);
      err.status = res.status;
      try { err.data = await res.json(); } catch (_) { /* ignore */ }
      throw err;
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  }

  /* ============================ NOTICES ============================ */
  function notice(message, kind = "info") {
    const node = document.createElement("div");
    node.className = "notice" + (kind === "error" ? " error" : kind === "warn" ? " warn" : "");
    const msg = document.createElement("div");
    msg.className = "n-msg";
    msg.textContent = message;
    const close = document.createElement("button");
    close.className = "n-close";
    close.textContent = "✕";
    close.setAttribute("aria-label", "Dismiss");
    close.onclick = () => node.remove();
    node.append(msg, close);
    el.notices.appendChild(node);
    if (kind !== "error") setTimeout(() => node.remove(), 6000);
  }

  /* ============================ VIEW SWITCHING ============================ */
  // pywebview exposes an optional resize hook; ignore if not present.
  function requestResize(w, h) {
    try {
      const pw = window.pywebview;
      if (pw && pw.api && typeof pw.api.resize === "function") pw.api.resize(w, h);
    } catch (_) { /* not hosted by pywebview, or no api */ }
  }

  function setView(view) {
    el.app.dataset.view = view;
    const expanded = view === "expanded";
    $("expanded").hidden = !expanded;
    $("compact").hidden = expanded;
    if (expanded) {
      requestResize(980, 680);
      // Idle with nothing to show -> session browser; otherwise the transcript.
      const hasTranscript = state.recording || state.openedSessionId || state.utterances.size > 0;
      setUiMode(hasTranscript ? "transcript" : "browser");
    } else {
      requestResize(700, 250);
    }
  }

  /* ============================ LOGO → WEBSITE ============================ */
  // The logo (compact + expanded) opens the product site externally. Prefer the
  // pywebview bridge; fall back to a normal new-tab open when not hosted.
  const PRODUCT_URL = "https://ducnguyen.vn/ai-record/";
  function openWebsite() {
    try {
      if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.open_external === "function") {
        window.pywebview.api.open_external(PRODUCT_URL);
        return;
      }
    } catch (_) { /* fall through to browser open */ }
    try { window.open(PRODUCT_URL, "_blank"); } catch (_) { /* popup blocked */ }
  }
  for (const logo of document.querySelectorAll(".app-logo")) {
    logo.addEventListener("click", openWebsite);
    logo.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openWebsite(); }
    });
  }

  // Swap the area under the expanded header between the saved-session browser
  // (Mode A) and the transcript + actions (Mode B). The toolbar is identical.
  function setUiMode(mode) {
    state.uiMode = mode === "transcript" ? "transcript" : "browser";
    if (el.expanded) el.expanded.dataset.mode = state.uiMode;
    if (state.uiMode === "browser") { loadSessionBrowser(); return; }
    updateTabAvail();
    if (el.transcriptPanel) el.transcriptPanel.dataset.tab = state.tab;
    if (state.tab === "transcript") scrollToLatest();
  }

  /* ============================ TIME / TEXT UTILS ============================ */
  function fmtTs(sec) {
    if (sec == null || isNaN(sec)) return "--:--";
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${pad(m)}:${pad(ss)}`;
  }
  function fmtDur(sec) {
    if (!sec && sec !== 0) return "";
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return `${m}m ${String(s).padStart(2, "0")}s`;
  }
  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  /* ============================ STATUS / DEGRADED STATE ============================ */
  // Map degraded_state codes to human labels + tooltip detail.
  const DEGRADED = {
    stt_catching_up:     { label: "STT catching up",             detail: "Speech-to-text is behind live audio and catching up." },
    audio_only:          { label: "Audio-only",                  detail: "Transcription is unavailable; audio is still being captured." },
    translation_paused:  { label: "Translation paused",          detail: "Translation is temporarily paused." },
    diarization_offline: { label: "Speaker labels offline-only", detail: "Speaker labels will be assigned after the session, not live." },
    mic_only:            { label: "Mic only",                    detail: "Only your microphone is captured; system audio is unavailable." },
    them_only:           { label: "System-audio only",           detail: "Only system audio is captured; your microphone is unavailable." },
  };

  function computeStatus(status) {
    // Returns { text, dotState, tooltip }
    if (!state.recording) return { text: "Idle", dot: "idle", tip: "" };
    const codes = (status && status.degraded_states) || [];
    if (status && status.error) return { text: "Error", dot: "error", tip: status.note || "" };
    // Priority: pick the first known degraded state; else plain "Recording".
    for (const code of codes) {
      const d = DEGRADED[code];
      if (d) return { text: d.label, dot: "warn", tip: d.detail };
    }
    if (codes.length) return { text: "Recording (degraded)", dot: "warn", tip: codes.join(", ") };
    return { text: "Recording", dot: "recording", tip: "" };
  }

  function renderStatus(status) {
    const s = computeStatus(status || {});
    for (const [dot, text, wrap] of [
      [el.cDot, el.cStatusText, el.cStatus],
      [el.xDot, el.xStatusText, el.xStatus],
    ]) {
      dot.dataset.state = s.dot;
      text.textContent = s.text;
      wrap.title = s.tip || s.text;
    }
    // Degraded-state chips (expanded only).
    el.xChips.textContent = "";
    // Draft indicator: ephemeral ("Không lưu") mode writes nothing to disk.
    if (isEphemeralActive()) {
      const draft = document.createElement("span");
      draft.className = "chip draft";
      draft.textContent = "Listening — không lưu";
      draft.title = "Phiên nháp: không tạo thư mục, không lưu transcript/audio/summary.";
      el.xChips.appendChild(draft);
    }
    const codes = (status && status.degraded_states) || [];
    for (const code of codes) {
      const d = DEGRADED[code];
      const chip = document.createElement("span");
      chip.className = "chip warn";
      chip.textContent = d ? d.label : code;
      chip.title = d ? d.detail : code;
      el.xChips.appendChild(chip);
    }
    if (status && status.effective_model) {
      const chip = document.createElement("span");
      chip.className = "chip info";
      chip.textContent = status.effective_model;
      chip.title = "Effective STT model" + (status.ladder_step != null ? ` · ladder step ${status.ladder_step}` : "");
      el.xChips.appendChild(chip);
    }
  }

  function setRecording(on) {
    state.recording = on;
    for (const btn of [el.cToggle, el.xToggle]) {
      btn.classList.toggle("recording", on);
      btn.textContent = on ? "● Stop" : "Record";
    }
    // Lock the device picker buttons while recording.
    for (const dd of deviceDropdowns) dd.btn.disabled = on;
    // Lock the ephemeral toggle while recording (mode is fixed at Start).
    for (const btn of [el.cEphemeral, el.xEphemeral]) if (btn) btn.disabled = on;
  }

  function refreshToggleEnabled() {
    const enabled = state.consentOk;
    for (const btn of [el.cToggle, el.xToggle]) {
      btn.disabled = !enabled;
      btn.title = enabled ? "" : "Acknowledge the consent notice before recording.";
    }
  }

  /* ============================ EPHEMERAL ("KHÔNG LƯU") ============================ */
  // A draft, no-save recording mode: transcribe/translate/summarize live, but NOTHING
  // is written to disk (no session folder, transcript, WAV, or summary). The two
  // toggles (compact + expanded) share state.ephemeral. In ephemeral mode the "Mở
  // thư mục lưu" button + the output-format selector are hidden (no files exist), and
  // Summary/Analyze route to /api/summarize-text with the client's own transcript.
  function isEphemeralActive() {
    // Only the live session can be ephemeral; a saved session opened for browsing is not.
    return state.ephemeral && !state.openedSessionId;
  }

  function setEphemeral(on) {
    // Cannot change mode mid-recording.
    if (state.recording) return;
    state.ephemeral = !!on;
    updateEphemeralUi();
  }

  function updateEphemeralUi() {
    const on = !!state.ephemeral;
    for (const btn of [el.cEphemeral, el.xEphemeral]) {
      if (!btn) continue;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.disabled = state.recording;
    }
    const active = isEphemeralActive();
    // Hide/disable the folder buttons — there is nothing to open in ephemeral mode.
    for (const btn of [el.openFolder, el.cFolder]) {
      if (!btn) continue;
      btn.hidden = active;
      btn.disabled = active;
    }
    // Hide the output-format selectors — no artefacts are produced.
    for (const d of outputDropdowns) {
      if (d.btn) d.btn.hidden = active;
    }
    if (active) closeAllPops();
    // Save (persist-to-disk) is meaningless in ephemeral mode; refresh result panels.
    updateResultSaveButtons();
  }

  // Keep the Summary/Analyze "Lưu" buttons hidden while ephemeral (no session dir).
  function updateResultSaveButtons() {
    if (!isEphemeralActive()) return;
    for (const kind of ["summary", "analyze"]) {
      const r = resultEls(kind);
      if (r.save) r.save.hidden = true;
    }
  }

  /* ============================ AUDIO DEVICES ============================ */
  // Input (mic) + Output (speaker/system) are ICON BUTTONS that open a popover
  // dropdown of devices. Compact + expanded share one selection each via
  // state.inputSel / state.outputSel (values "off" | "id:<id>"). A green on-dot
  // on the button proves the source is active; "Tắt" disables it.
  const deviceDropdowns = [];  // { btn, pop, kind, dot }

  async function loadAudioDevices() {
    try {
      state.devices = await api("/api/audio-devices");
    } catch (_) {
      state.devices = { inputs: [], outputs: [], available: false };
    }
    fillDeviceDropdowns();
  }

  function currentSel(kind) { return kind === "input" ? state.inputSel : state.outputSel; }
  function setSel(kind, val) { if (kind === "input") state.inputSel = val; else state.outputSel = val; }

  // The system default (default:true) is the initial selection; fall back to the
  // first device so a source is on by default. "off" only via explicit "Tắt".
  function defaultDeviceVal(devices) {
    const def = (devices || []).find((d) => d.default);
    if (def) return "id:" + String(def.id);
    return devices && devices.length ? "id:" + String(devices[0].id) : "off";
  }

  function mkCheckIcon() {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "15");
    svg.setAttribute("height", "15");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.6");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(NS, "polyline");
    p.setAttribute("points", "20 6 9 17 4 12");
    svg.appendChild(p);
    return svg;
  }

  function mkDeviceOpt(dd, val, name, isDefault) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "device-opt" + (currentSel(dd.kind) === val ? " selected" : "");
    const check = document.createElement("span");
    check.className = "device-check";
    check.appendChild(mkCheckIcon());
    const label = document.createElement("span");
    label.className = "device-name";
    label.textContent = isDefault ? `${name} (mặc định hệ thống)` : name;
    row.append(check, label);
    row.addEventListener("click", () => {
      setSel(dd.kind, val);
      fillDeviceDropdowns();   // re-render selection across both toolbars
      closeAllPops();
    });
    return row;
  }

  function fillOneDevicePop(dd, devices, available) {
    const pop = dd.pop;
    pop.textContent = "";
    if (!available || !devices || !devices.length) {
      const row = document.createElement("div");
      row.className = "device-opt disabled";
      const label = document.createElement("span");
      label.className = "device-name";
      label.textContent = "Không tìm thấy thiết bị";
      row.appendChild(label);
      pop.appendChild(row);
      return;
    }
    // Ensure a valid selection (default the first time we see devices).
    const values = new Set(["off", ...devices.map((d) => "id:" + String(d.id))]);
    if (!currentSel(dd.kind) || !values.has(currentSel(dd.kind))) {
      setSel(dd.kind, defaultDeviceVal(devices));
    }
    // System default listed first.
    const ordered = devices.slice().sort((a, b) => (b.default ? 1 : 0) - (a.default ? 1 : 0));
    for (const dev of ordered) {
      pop.appendChild(mkDeviceOpt(dd, "id:" + String(dev.id), dev.name || String(dev.id), !!dev.default));
    }
    // "Tắt" disables this source.
    pop.appendChild(mkDeviceOpt(dd, "off", "Tắt", false));
  }

  function fillDeviceDropdowns() {
    const d = state.devices || { inputs: [], outputs: [], available: false };
    for (const dd of deviceDropdowns) {
      const list = dd.kind === "input" ? d.inputs : d.outputs;
      fillOneDevicePop(dd, list, d.available);
    }
    updateDeviceButtons();
    // Re-clamp any open device popover whose contents just changed size.
    for (const dd of deviceDropdowns) if (!dd.pop.hidden) clampPopover(dd.pop);
  }

  function updateDeviceButtons() {
    for (const dd of deviceDropdowns) {
      const sel = currentSel(dd.kind);
      const on = !!sel && sel !== "off";
      if (dd.dot) dd.dot.hidden = !on;
      dd.btn.classList.toggle("source-off", !on);
      const label = dd.kind === "input" ? "Micro" : "Loa / hệ thống";
      dd.btn.title = on ? `${label}: bật` : `${label}: tắt`;
    }
  }

  function registerDeviceDropdown(btnId, kind) {
    const btn = $(btnId);
    if (!btn) return;
    const wrap = btn.parentNode;  // .device-dd (relative)
    const pop = document.createElement("div");
    pop.className = "device-pop";
    pop.hidden = true;
    wrap.appendChild(pop);
    const dd = { btn, pop, kind, dot: btn.querySelector(".on-dot") };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = pop.hidden;
      closeAllPops();
      if (willOpen) {
        fillDeviceDropdowns();      // render from current cache immediately
        pop.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        clampPopover(pop);
        loadAudioDevices();         // then refresh the list from the backend
      }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    deviceDropdowns.push(dd);
  }
  registerDeviceDropdown("c-input", "input");
  registerDeviceDropdown("x-input", "input");
  registerDeviceDropdown("c-output-dev", "output");
  registerDeviceDropdown("x-output-dev", "output");

  // Resolve a selection value ("off" | "id:<id>") back to the backend device id.
  function resolveDeviceId(val, list) {
    if (!val || val === "off" || val === "__none__") return null;
    const raw = val.startsWith("id:") ? val.slice(3) : val;
    const dev = (list || []).find((d) => String(d.id) === raw);
    return dev ? dev.id : raw;
  }

  function deviceRequest() {
    const d = state.devices || {};
    return {
      input_device: resolveDeviceId(state.inputSel, d.inputs),
      output_device: resolveDeviceId(state.outputSel, d.outputs),
    };
  }

  function updateTranslateButtons() {
    const on = !!(state.settings && state.settings.translate_enabled);
    for (const btn of [el.cTranslate, el.xTranslate]) {
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.title = on ? "Dịch: bật" : "Dịch: tắt";
      const dot = btn.querySelector(".on-dot");
      if (dot) dot.hidden = !on;
    }
    refreshTranslatePops();
  }

  function refreshTranslationRows() {
    for (const { record, el: row } of state.utterances.values()) {
      const tr = row.querySelector(".tr");
      if (tr) applyTranslation(tr, record);
    }
    renderRecent();
  }

  async function setTranslateEnabled(enabled) {
    try {
      const updated = await api("/api/settings", { method: "PUT", body: { translate_enabled: enabled } });
      state.settings = updated || Object.assign(state.settings || {}, { translate_enabled: enabled });
      updateTranslateButtons();
      refreshTranslationRows();
    } catch (e) {
      notice("Couldn't update translation: " + (e.message || e), "error");
    }
  }
  /* ============================ TRANSLATE POPOVER ============================ */
  // The translate icon opens a small popover: on/off toggle + From/To selects.
  // From includes an "Auto" option. Applying writes translate_enabled / target_lang
  // / source_languages via PUT /api/settings. The icon shows an active state when on.
  const translateDropdowns = [];  // { btn, pop, onInput, fromSel, toSel }

  function langOptions() {
    const list = (state.languages && state.languages.length)
      ? state.languages.slice()
      : [{ code: "en", label: "English (en)" }, { code: "vi", label: "Vietnamese (vi)" }];
    // Guarantee the two defaults are always selectable.
    for (const need of [{ code: "en", label: "English (en)" }, { code: "vi", label: "Vietnamese (vi)" }]) {
      if (!list.some((l) => l.code === need.code)) list.push(need);
    }
    return list;
  }

  function fillLangSelect(sel, includeAuto) {
    sel.textContent = "";
    if (includeAuto) {
      const o = document.createElement("option");
      o.value = "auto";
      o.textContent = "Auto (mọi ngôn ngữ)";
      sel.appendChild(o);
    }
    for (const l of langOptions()) {
      const o = document.createElement("option");
      o.value = l.code;
      o.textContent = l.label || l.code;
      sel.appendChild(o);
    }
  }

  function refreshTranslatePops() {
    if (!translateDropdowns.length) return;
    const s = state.settings || {};
    const on = !!s.translate_enabled;
    const src = Array.isArray(s.source_languages) ? s.source_languages : [];
    // Reflect settings; default From=en, To=vi. A specific source language wins;
    // otherwise fall back to the en default (Auto stays a selectable option).
    const fromVal = src.length ? src[0] : "en";
    const toVal = s.target_lang || "vi";
    for (const dd of translateDropdowns) {
      dd.onInput.checked = on;
      fillLangSelect(dd.fromSel, true);
      fillLangSelect(dd.toSel, false);
      ensureSelectValue(dd.fromSel, fromVal, fromVal);
      ensureSelectValue(dd.toSel, toVal, toVal);
    }
  }

  function registerTranslateDropdown(btnId) {
    const btn = $(btnId);
    if (!btn) return;
    const wrap = btn.parentNode;  // .translate-dd (relative)
    const pop = document.createElement("div");
    pop.className = "translate-pop";
    pop.hidden = true;

    const toggle = document.createElement("label");
    toggle.className = "tp-toggle";
    const onInput = document.createElement("input");
    onInput.type = "checkbox";
    const onText = document.createElement("span");
    onText.textContent = "Bật dịch";
    toggle.append(onInput, onText);
    onInput.addEventListener("change", () => setTranslateEnabled(onInput.checked));

    const langs = document.createElement("div");
    langs.className = "tp-langs";
    const fromField = document.createElement("div");
    fromField.className = "tp-field";
    const fromCap = document.createElement("small");
    fromCap.textContent = "Dịch từ";
    const fromSel = document.createElement("select");
    fromSel.className = "tp-select";
    fromField.append(fromCap, fromSel);
    const arrow = document.createElement("span");
    arrow.className = "tp-arrow";
    arrow.textContent = "→";
    const toField = document.createElement("div");
    toField.className = "tp-field";
    const toCap = document.createElement("small");
    toCap.textContent = "Sang";
    const toSel = document.createElement("select");
    toSel.className = "tp-select";
    toField.append(toCap, toSel);
    langs.append(fromField, arrow, toField);

    fromSel.addEventListener("change", () => {
      putSetting({ source_languages: fromSel.value === "auto" ? [] : [fromSel.value] });
    });
    toSel.addEventListener("change", () => putSetting({ target_lang: toSel.value }));

    pop.append(toggle, langs);
    wrap.appendChild(pop);

    const dd = { btn, pop, onInput, fromSel, toSel };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = pop.hidden;
      closeAllPops();
      if (willOpen) {
        refreshTranslatePops();
        pop.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        clampPopover(pop);
      }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    translateDropdowns.push(dd);
  }
  registerTranslateDropdown("c-translate");
  registerTranslateDropdown("x-translate");

  /* ============================ OUTPUT FORMATS DROPDOWN ============================ */
  // A pre-Start multi-select choosing which artefacts a session saves. Two identical
  // dropdowns (compact + expanded) share ONE source of truth: settings.output_formats.
  // ".md" is always on (checked + disabled). Editing anytime; applies at next finalize.
  const OUTPUT_OPTIONS = [
    { key: "md", label: ".md — bản ghi (luôn lưu)", fixed: true },
    { key: "txt", label: ".txt — văn bản thuần" },
    { key: "mp3", label: ".mp3 — kèm audio" },
    { key: "summary", label: "AI summary — tóm tắt tự động" },
  ];
  const OUTPUT_ORDER = OUTPUT_OPTIONS.map((o) => o.key);
  const outputDropdowns = [];  // { btn, pop, checks: Map<key, input>, badge }

  // Current formats as a Set: sanitize to known keys and always include "md".
  function currentOutputFormats() {
    const raw = (state.settings && state.settings.output_formats) || ["md"];
    const set = new Set((Array.isArray(raw) ? raw : []).filter((f) => OUTPUT_ORDER.includes(f)));
    set.add("md");
    return set;
  }

  async function setOutputFormat(key, on) {
    const set = currentOutputFormats();
    if (on) set.add(key); else set.delete(key);
    set.add("md");  // always produced
    const list = OUTPUT_ORDER.filter((k) => set.has(k));
    await putSetting({ output_formats: list });
    refreshOutputDropdowns();
  }

  // Close every popover in the toolbar (device, output formats, translate, copy).
  function closeAllPops() {
    for (const d of deviceDropdowns) {
      d.pop.hidden = true;
      d.btn.setAttribute("aria-expanded", "false");
    }
    for (const d of outputDropdowns) {
      d.pop.hidden = true;
      d.btn.setAttribute("aria-expanded", "false");
    }
    for (const d of translateDropdowns) {
      d.pop.hidden = true;
      d.btn.setAttribute("aria-expanded", "false");
    }
    for (const d of copyDropdowns) {
      d.pop.hidden = true;
      d.btn.setAttribute("aria-expanded", "false");
    }
  }

  // Keep a just-opened toolbar popover fully inside the OS window: content cannot
  // overflow the native window edge, so shift it left if its right edge would
  // exceed the viewport, and cap its height if it would run past the bottom.
  const POP_MARGIN = 8;
  function clampPopover(pop) {
    if (!pop || pop.hidden) return;
    pop.style.transform = "none";
    pop.style.maxHeight = "";
    pop.style.overflowY = "";
    // Horizontal: shift left to fit, but never past the left margin.
    const rect = pop.getBoundingClientRect();
    let shift = 0;
    const rightOver = rect.right - (window.innerWidth - POP_MARGIN);
    if (rightOver > 0) shift = rightOver;
    if (rect.left - shift < POP_MARGIN) shift = Math.max(0, rect.left - POP_MARGIN);
    if (shift > 0) pop.style.transform = `translateX(${-shift}px)`;
    // Vertical: if it would spill past the bottom, cap height + scroll inside.
    const rect2 = pop.getBoundingClientRect();
    const bottomOver = rect2.bottom - (window.innerHeight - POP_MARGIN);
    if (bottomOver > 0) {
      const avail = window.innerHeight - rect2.top - POP_MARGIN;
      pop.style.maxHeight = Math.max(120, avail) + "px";
      pop.style.overflowY = "auto";
    }
  }

  function refreshOutputDropdowns() {
    const set = currentOutputFormats();
    const on = OUTPUT_ORDER.filter((k) => set.has(k));
    for (const d of outputDropdowns) {
      for (const [key, cb] of d.checks) cb.checked = set.has(key);
      if (d.badge) d.badge.hidden = on.length <= 1;   // dot when >1 format selected
      d.btn.title = "Định dạng lưu: " + on.join(", ");
    }
  }

  function registerOutputDropdown(btnId) {
    const btn = $(btnId);
    if (!btn) return;
    const wrap = btn.parentNode;  // .output-dd (relative-positioned)
    const pop = document.createElement("div");
    pop.className = "output-pop";
    pop.hidden = true;
    const checks = new Map();
    for (const opt of OUTPUT_OPTIONS) {
      const row = document.createElement("label");
      row.className = "output-opt";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = opt.key === "md";
      if (opt.fixed) cb.disabled = true;
      const span = document.createElement("span");
      span.textContent = opt.label;
      row.append(cb, span);
      if (!opt.fixed) cb.addEventListener("change", () => setOutputFormat(opt.key, cb.checked));
      checks.set(opt.key, cb);
      pop.appendChild(row);
    }
    wrap.appendChild(pop);
    const badge = btn.querySelector(".fmt-badge");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = pop.hidden;
      closeAllPops();
      if (willOpen) { refreshOutputDropdowns(); pop.hidden = false; btn.setAttribute("aria-expanded", "true"); clampPopover(pop); }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    outputDropdowns.push({ btn, pop, checks, badge });
  }
  registerOutputDropdown("c-output");
  registerOutputDropdown("x-output");
  document.addEventListener("click", closeAllPops);

  /* ============================ TRANSCRIPT RENDERING ============================ */
  function speakerText(rec) {
    // Unknown / low-confidence -> "Speaker ?"
    if (rec.speaker && rec.speaker.trim()) {
      const low = rec.diarization_confidence != null && rec.diarization_confidence < 0.5;
      const label = rec.speaker.trim();
      const unknown = low || label === "?" || label === UNKNOWN_SPEAKER;
      return { text: label === "?" ? UNKNOWN_SPEAKER : label, cls: unknown ? "unknown" : "" };
    }
    // No speaker yet: diarization pending placeholder.
    return { text: UNKNOWN_SPEAKER, cls: "pending unknown" };
  }

  function buildRow(rec) {
    const row = document.createElement("div");
    row.className = "utt";
    row.dataset.seq = rec.seq;
    row.dataset.source = rec.source === "you" ? "you" : "them";

    const ts = document.createElement("div");
    ts.className = "ts";
    ts.textContent = fmtTs(rec.start);

    const spk = document.createElement("div");
    spk.className = "spk";
    const sp = speakerText(rec);
    spk.textContent = sp.text;
    if (sp.cls) spk.classList.add(...sp.cls.split(" "));
    if (rec.diarization_confidence != null)
      spk.title = `confidence ${(rec.diarization_confidence * 100).toFixed(0)}% · click to rename`;
    else spk.title = "click to rename";
    spk.addEventListener("click", () => beginRename(spk, rec));

    const body = document.createElement("div");
    body.className = "body";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = rec.text || "";
    const tr = document.createElement("div");
    tr.className = "tr";
    applyTranslation(tr, rec);
    body.append(text, tr);

    row.append(ts, spk, body);
    return row;
  }

  function applyTranslation(trEl, rec) {
    trEl.classList.remove("pending", "error");
    if (rec.translation) {
      trEl.textContent = rec.translation;
    } else if (rec.translation_error) {
      // Generic message (the flag is a bool) and NOT a stuck "translating…" state.
      trEl.textContent = "Translation failed";
      trEl.classList.add("error");
    } else if (rec.stale_skipped) {
      trEl.textContent = "";
    } else if (state.settings && state.settings.translate_enabled) {
      // Translation is expected but hasn't arrived yet.
      trEl.textContent = "translating…";
      trEl.classList.add("pending");
    } else {
      trEl.textContent = "";
    }
  }

  function addUtterance(rec) {
    if (rec.seq == null) return;
    if (rec.session_id && !state.sessionId) state.sessionId = rec.session_id;
    if (state.utterances.has(rec.seq)) { patchUtterance(rec.seq, rec); return; }
    if (rec.seq > state.lastSeq) state.lastSeq = rec.seq;

    const row = buildRow(rec);
    state.utterances.set(rec.seq, { record: rec, el: row });

    // Insert in seq order (usually just append; handle out-of-order catch-up).
    const cont = el.xTranscript;
    const lastChild = cont.lastElementChild;
    if (!lastChild || Number(lastChild.dataset.seq) < rec.seq) {
      cont.appendChild(row);
    } else {
      let ref = null;
      for (const child of cont.children) {
        if (Number(child.dataset.seq) > rec.seq) { ref = child; break; }
      }
      cont.insertBefore(row, ref);
    }

    trimDom();
    applySearchToRow(row);
    renderRecent();
    refreshPlainIfActive();
    if (state.autoScroll) scrollToLatest();
  }

  function patchUtterance(seq, fields) {
    const entry = state.utterances.get(seq);
    if (!entry) return;
    Object.assign(entry.record, fields);
    const rec = entry.record;
    const row = entry.el;
    // Translation line (in place, no reflow of siblings).
    const tr = row.querySelector(".tr");
    if (tr) applyTranslation(tr, rec);
    // Speaker label.
    if ("speaker" in fields || "diarization_confidence" in fields) {
      const spk = row.querySelector(".spk");
      const sp = speakerText(rec);
      spk.textContent = sp.text;
      spk.classList.remove("pending", "unknown");
      if (sp.cls) spk.classList.add(...sp.cls.split(" "));
      if (rec.diarization_confidence != null)
        spk.title = `confidence ${(rec.diarization_confidence * 100).toFixed(0)}% · click to rename`;
    }
    applySearchToRow(row);
    if (isRecentSeq(seq)) renderRecent();
    refreshPlainIfActive();
  }

  function trimDom() {
    const cont = el.xTranscript;
    while (cont.children.length > MAX_ROWS) {
      const first = cont.firstElementChild;
      const seq = Number(first.dataset.seq);
      state.utterances.delete(seq);
      first.remove();
    }
  }

  /* Rename speaker inline; broadcasts nothing itself — server may echo a rename. */
  function beginRename(spkEl, rec) {
    if (spkEl.querySelector("input")) return;
    const current = speakerText(rec).text;
    const input = document.createElement("input");
    input.className = "spk-edit";
    input.value = current === UNKNOWN_SPEAKER ? "" : current;
    input.placeholder = "Speaker name";
    spkEl.textContent = "";
    spkEl.appendChild(input);
    input.focus();
    input.select();

    let finished = false;
    const finish = (commit) => {
      if (finished) return;
      finished = true;
      const val = input.value.trim();
      spkEl.textContent = "";
      if (commit && val && val !== current) {
        const sp = speakerText(rec);
        spkEl.textContent = sp.text;
        spkEl.className = "spk" + (sp.cls ? " " + sp.cls : "");
        renameSpeaker(current, val);
      } else {
        const sp = speakerText(rec);
        spkEl.textContent = sp.text;
        spkEl.className = "spk" + (sp.cls ? " " + sp.cls : "");
      }
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  // Relabel all rows whose displayed speaker matches `oldName` -> `newName`.
  function applySpeakerRename(oldName, newName) {
    if (!newName) return;
    for (const { record, el: row } of state.utterances.values()) {
      if (speakerText(record).text === (oldName || "")) {
        record.speaker = newName;
        const spk = row.querySelector(".spk");
        spk.textContent = newName;
        spk.classList.remove("pending", "unknown");
      }
    }
    renderRecent();
  }

  async function renameSpeaker(oldName, newName) {
    const sid = activeSessionId();
    if (!sid) { notice("Start or select a session before renaming speakers.", "warn"); return; }
    try {
      await api(`/api/sessions/${encodeURIComponent(sid)}/speakers/rename`, {
        method: "POST",
        body: { old: oldName, new: newName },
      });
      applySpeakerRename(oldName, newName);
    } catch (e) {
      notice("Couldn't rename speaker: " + (e.message || e), "error");
      applySearchAll();
    }
  }

  /* ============================ COMPACT RECENT LINES ============================ */
  // Render a generous window of the newest utterances; the .recent container is a
  // bottom-anchored flex column with overflow hidden, so it shows exactly as many
  // lines as fit and clips the oldest at the top. Dragging the window taller shows
  // MORE lines with no extra work here.
  const RECENT_MAX = 60;
  function recentEntries() {
    const seqs = [...state.utterances.keys()].sort((a, b) => a - b);
    return seqs.slice(-RECENT_MAX).map((s) => state.utterances.get(s).record);
  }
  function isRecentSeq(seq) {
    const seqs = [...state.utterances.keys()].sort((a, b) => a - b).slice(-RECENT_MAX);
    return seqs.includes(seq);
  }
  function renderRecent() {
    const recs = recentEntries();
    el.cRecent.textContent = "";
    if (!recs.length) {
      const empty = document.createElement("div");
      empty.className = "recent-empty";
      empty.textContent = state.recording ? "Listening…" : "No transcript yet.";
      el.cRecent.appendChild(empty);
      return;
    }
    for (const rec of recs) {
      const line = document.createElement("div");
      line.className = "r-line";
      const t = document.createElement("span");
      t.className = "r-text";
      t.textContent = rec.text || "";
      line.appendChild(t);
      el.cRecent.appendChild(line);
      if (rec.translation) {
        const tl = document.createElement("div");
        tl.className = "r-line r-tr";
        tl.textContent = rec.translation;
        el.cRecent.appendChild(tl);
      }
    }
  }

  /* ============================ AUTO-SCROLL ============================ */
  function scrollToLatest() {
    const c = el.xTranscript;
    c.scrollTop = c.scrollHeight;
    state.autoScroll = true;
    el.xJump.hidden = true;
  }
  el.xTranscript.addEventListener("scroll", () => {
    const c = el.xTranscript;
    const nearBottom = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
    state.autoScroll = nearBottom;
    el.xJump.hidden = nearBottom;
  });
  el.xJump.addEventListener("click", scrollToLatest);

  /* ============================ SEARCH ============================ */
  function highlight(node, plain, term) {
    node.textContent = "";
    if (!term) { node.textContent = plain; return; }
    const low = plain.toLowerCase();
    let i = 0, idx;
    const t = term.toLowerCase();
    while ((idx = low.indexOf(t, i)) !== -1) {
      if (idx > i) node.appendChild(document.createTextNode(plain.slice(i, idx)));
      const mark = document.createElement("mark");
      mark.textContent = plain.slice(idx, idx + term.length);
      node.appendChild(mark);
      i = idx + term.length;
    }
    if (i < plain.length) node.appendChild(document.createTextNode(plain.slice(i)));
  }

  function applySearchToRow(row) {
    const term = state.searchTerm;
    const entry = state.utterances.get(Number(row.dataset.seq));
    if (!entry) return;
    const rec = entry.record;
    const haystack = [rec.text, rec.translation, rec.speaker].filter(Boolean).join(" ").toLowerCase();
    const match = !term || haystack.includes(term.toLowerCase());
    row.classList.toggle("hidden", !match);
    if (match && term) {
      highlight(row.querySelector(".text"), rec.text || "", term);
      const tr = row.querySelector(".tr");
      if (rec.translation) highlight(tr, rec.translation, term);
    } else {
      // Reset highlight when no term.
      const text = row.querySelector(".text");
      if (text.querySelector("mark")) text.textContent = rec.text || "";
      const tr = row.querySelector(".tr");
      if (tr.querySelector("mark") && rec.translation) tr.textContent = rec.translation;
    }
  }

  function applySearchAll() {
    for (const { el: row } of state.utterances.values()) applySearchToRow(row);
  }

  el.xSearch.addEventListener("input", () => {
    state.searchTerm = el.xSearch.value.trim();
    applySearchAll();
  });

  /* ============================ RECORDING CONTROLS ============================ */
  function activeSessionId() {
    return state.openedSessionId || state.sessionId;
  }

  async function toggleRecording() {
    if (state.recording) {
      try {
        const r = await api("/api/capture/stop", { method: "POST" });
        setRecording(false);
        updateEphemeralUi();
        renderStatus({ recording: false });
        notice("Recording stopped." + (r && r.finalized ? " Session finalized." : ""), "info");
        loadSessionsInto();  // refresh sessions list if drawer open
      } catch (e) {
        notice("Couldn't stop recording: " + (e.message || e), "error");
      }
    } else {
      if (!state.consentOk) { openConsent(); return; }
      const req = deviceRequest();
      if (req.input_device == null && req.output_device == null) {
        notice("Chọn ít nhất một nguồn: micro hoặc loa/hệ thống.", "warn");
        return;
      }
      try {
        const title = el.xTitle.value.trim() || undefined;
        // A new live session replaces any opened saved session + its transcript.
        state.openedSessionId = null;
        clearTranscript();
        const ephemeral = !!state.ephemeral;
        const r = await api("/api/capture/start", {
          method: "POST",
          body: { title, input_device: req.input_device, output_device: req.output_device, ephemeral },
        });
        state.sessionId = r && r.session_id;
        setRecording(true);
        updateEphemeralUi();
        renderStatus({ recording: true, ephemeral });
        setUiMode("transcript");
        notice(ephemeral ? "Đang ghi (nháp — không lưu)." : "Recording started.", "info");
      } catch (e) {
        if (e.status === 403) { openConsent(); notice("Please acknowledge consent before recording.", "warn"); }
        else if (e.status === 422) notice("Chọn ít nhất một nguồn để ghi (micro hoặc loa).", "warn");
        else notice("Couldn't start recording: " + (e.message || e), "error");
      }
    }
  }
  el.cToggle.addEventListener("click", toggleRecording);
  el.xToggle.addEventListener("click", toggleRecording);
  for (const btn of [el.cEphemeral, el.xEphemeral]) {
    if (btn) btn.addEventListener("click", () => setEphemeral(!state.ephemeral));
  }

  /* ============================ SUMMARY / EXPORT / REDIARIZE ============================ */
  function setToolStatus(text, kind = "info") {
    el.toolStatus.textContent = text || "";
    el.toolStatus.className = "tool-status" + (kind === "error" ? " error" : kind === "warn" ? " warn" : "");
  }

  async function openFolder() {
    // A saved session opens its own folder; otherwise the default sessions root.
    const sid = state.openedSessionId;
    try {
      const r = sid
        ? await api(`/api/sessions/${encodeURIComponent(sid)}/open-folder`, { method: "POST" })
        : await api("/api/open-folder", { method: "POST" });
      setToolStatus(`Đã mở thư mục lưu${r && r.path ? ": " + r.path : ""}`);
    } catch (_e) {
      setToolStatus("Không mở được thư mục lưu.", "error");
    }
  }

  /* ============================ COPY TO CLIPBOARD ============================ */
  // navigator.clipboard may be unavailable/restricted under pywebview; fall back
  // to a hidden <textarea> + execCommand("copy").
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (_) { return false; }
  }

  // Build the full transcript as plain text, matching the .txt export shape:
  //   [hh:mm:ss] Speaker: text
  //       translation
  function buildTranscriptText() {
    const seqs = [...state.utterances.keys()].sort((a, b) => a - b);
    const lines = [];
    for (const s of seqs) {
      const rec = state.utterances.get(s).record;
      const spk = speakerText(rec).text;
      lines.push(`[${fmtTs(rec.start)}] ${spk}: ${rec.text || ""}`);
      if (rec.translation) lines.push(`    ${rec.translation}`);
    }
    return lines.join("\n");
  }

  // "Text only" variant: just the concatenated spoken text, no speaker/timestamp.
  function buildTranscriptPlainText() {
    const seqs = [...state.utterances.keys()].sort((a, b) => a - b);
    return seqs
      .map((s) => (state.utterances.get(s).record.text || "").trim())
      .filter(Boolean)
      .join("\n");
  }

  // --- Dialogue ⇄ Văn bản (plain-text) view for the Transcript tab ---
  function renderPlainTranscript() {
    if (el.xTranscriptPlain) el.xTranscriptPlain.textContent = buildTranscriptText() || "";
  }
  function refreshPlainIfActive() {
    if (state.transcriptView === "plain") renderPlainTranscript();
  }
  function setTranscriptView(mode) {
    const plain = mode === "plain";
    state.transcriptView = plain ? "plain" : "dialogue";
    if (plain) renderPlainTranscript();
    if (el.xTranscript) el.xTranscript.hidden = plain;
    if (el.xTranscriptPlain) el.xTranscriptPlain.hidden = !plain;
    if (el.xViewmode) {
      el.xViewmode.setAttribute("aria-pressed", plain ? "true" : "false");
      el.xViewmode.textContent = plain ? "💬 Hội thoại" : "📄 Văn bản";
      el.xViewmode.title = plain
        ? "Về giao diện hội thoại" : "Chuyển sang văn bản thuần (dễ bôi chọn & copy)";
    }
    if (!plain && state.autoScroll) scrollToLatest();
  }

  async function copyTranscript(withSpeakers) {
    const text = withSpeakers ? buildTranscriptText() : buildTranscriptPlainText();
    if (!text.trim()) { setToolStatus("Chưa có nội dung để copy.", "warn"); return; }
    const ok = await copyText(text);
    setToolStatus(ok ? "Đã copy" : "Không copy được.", ok ? "info" : "error");
  }

  /* ---- Copy dropdown: "Chỉ văn bản" | "Kèm người nói" ---- */
  // One shared copy behavior attached to EACH copy button that exists (the compact
  // toolbar icon `#c-copy` and the expanded header `#copy-transcript`). Both open the
  // same "Chỉ văn bản / Kèm người nói" menu and copy the current transcript, so the
  // copy logic (buildTranscriptText / buildTranscriptPlainText) lives in one place.
  const copyDropdowns = [];
  function registerCopyDropdown(btn) {
    if (!btn) return;
    const wrap = btn.parentNode;  // .copy-dd
    const pop = document.createElement("div");
    pop.className = "copy-pop";
    pop.hidden = true;
    const opts = [
      { label: "Chỉ văn bản", withSpeakers: false },
      { label: "Kèm người nói", withSpeakers: true },
    ];
    for (const o of opts) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "copy-opt";
      b.textContent = o.label;
      b.addEventListener("click", () => { closeAllPops(); copyTranscript(o.withSpeakers); });
      pop.appendChild(b);
    }
    wrap.appendChild(pop);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = pop.hidden;
      closeAllPops();
      if (willOpen) { pop.hidden = false; btn.setAttribute("aria-expanded", "true"); clampPopover(pop); }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    copyDropdowns.push({ btn, pop });
  }

  /* ============================ THREE-VIEW TABS ============================ */
  // Tabs: Transcript (live, always mounted) | Summary (reformat) | Analyze (analyze).
  // Summary/Analyze fetch once against activeSessionId(), cache the payload in
  // state.summaryResult / state.analyzeResult, and offer refresh + save + copy.
  function resultEls(kind) {
    return {
      tab: kind === "summary" ? el.tabSummary : el.tabAnalyze,
      meta: $(`${kind}-meta`),
      fallback: $(`${kind}-fallback`),
      output: $(`${kind}-output`),
      save: $(`${kind}-save`),
      copy: $(`${kind}-copy`),
      refresh: $(`${kind}-refresh`),
    };
  }
  function getCache(kind) { return kind === "summary" ? state.summaryResult : state.analyzeResult; }
  function setCache(kind, v) { if (kind === "summary") state.summaryResult = v; else state.analyzeResult = v; }

  function updateTabAvail() {
    const has = !!activeSessionId();
    for (const btn of [el.tabSummary, el.tabAnalyze]) {
      if (!btn) continue;
      btn.disabled = !has;
      btn.title = has ? "" : "Bắt đầu ghi hoặc mở một phiên để dùng.";
    }
    // If a result tab is active but the session went away, fall back to transcript.
    if (!has && state.tab !== "transcript") setTab("transcript");
  }

  function setTab(tab) {
    if (!["transcript", "summary", "analyze"].includes(tab)) tab = "transcript";
    if (tab !== "transcript" && !activeSessionId()) return;  // disabled tab
    state.tab = tab;
    for (const [name, btn] of [
      ["transcript", el.tabTranscript], ["summary", el.tabSummary], ["analyze", el.tabAnalyze],
    ]) {
      if (btn) btn.setAttribute("aria-selected", name === tab ? "true" : "false");
    }
    if (el.transcriptPanel) el.transcriptPanel.dataset.tab = tab;
    // The Dialogue⇄Text toggle only applies to the live Transcript view.
    if (el.xViewmode) el.xViewmode.hidden = tab !== "transcript";
    if (tab === "transcript") { scrollToLatest(); return; }
    // Result tab: show the cache, or run it the first time it is opened.
    const cache = getCache(tab);
    if (cache) renderResult(tab, cache);
    else runResult(tab);
  }

  function renderLoading(kind) {
    const r = resultEls(kind);
    if (r.meta) r.meta.textContent = "";
    if (r.fallback) r.fallback.hidden = true;
    if (r.save) r.save.hidden = true;
    if (r.copy) r.copy.hidden = true;
    if (r.output) {
      r.output.classList.add("result-loading");
      r.output.textContent = kind === "analyze" ? "Đang phân tích…" : "Đang tóm tắt…";
    }
  }

  function renderResult(kind, payload) {
    const r = resultEls(kind);
    if (!r.output) return;
    r.output.classList.remove("result-loading");
    if (payload && payload.error && !payload.markdown) {
      if (r.meta) r.meta.textContent = "Không xử lý được";
      if (r.fallback) r.fallback.hidden = true;
      if (r.save) r.save.hidden = true;
      if (r.copy) r.copy.hidden = true;
      renderMarkdown("**Lỗi:** " + String(payload.error), r.output);
      return;
    }
    const markdown = payload && payload.markdown ? String(payload.markdown) : "";
    const provider = payload && payload.provider ? providerLabel(payload.provider) : "";
    if (r.meta) r.meta.textContent = [provider, payload && payload.saved ? "đã lưu" : ""].filter(Boolean).join(" · ");
    if (r.fallback) r.fallback.hidden = !(payload && payload.reformat_fallback);
    if (r.save) {
      // Ephemeral mode has no session dir to persist into — hide "Lưu" entirely
      // (the user copies the result instead).
      r.save.hidden = !markdown || isEphemeralActive();
      r.save.disabled = false;
      r.save.textContent = payload && payload.saved ? "Đã lưu" : "Lưu";
    }
    if (r.copy) r.copy.hidden = !markdown;
    renderMarkdown(markdown || "_Không có nội dung._", r.output);
  }

  // Run the summarize scenario for a tab. Cache the payload so switching tabs never
  // re-runs; the "Chạy lại" button forces a re-run.
  async function runResult(kind) {
    const ephem = isEphemeralActive();
    const sid = activeSessionId();
    if (!sid && !ephem) { notice("Bắt đầu hoặc chọn một phiên trước.", "warn"); return; }
    const provider = (state.settings && (state.settings.summarizer_provider || state.settings.summary_provider)) || "claude_cli";
    const scenario = TAB_SCENARIO[kind];
    const r = resultEls(kind);
    if (r.refresh) r.refresh.disabled = true;
    renderLoading(kind);
    try {
      let payload;
      if (ephem) {
        // No persisted session: summarize the client's own transcript text directly.
        const text = buildTranscriptText();
        if (!text.trim()) {
          renderResult(kind, { error: "Chưa có nội dung để xử lý." });
          if (r.refresh) r.refresh.disabled = false;
          return;
        }
        payload = await api("/api/summarize-text", {
          method: "POST",
          body: { text, scenario, provider },
        });
      } else {
        payload = await api(`/api/sessions/${encodeURIComponent(sid)}/summarize`, {
          method: "POST",
          body: { scenario, provider },
        });
      }
      setCache(kind, payload || {});
      renderResult(kind, payload || {});
    } catch (e) {
      const errText = (e.data && e.data.error) || e.message || String(e);
      renderResult(kind, { error: errText });
      notice("Không xử lý được phiên: " + errText, "error");
    } finally {
      if (r.refresh) r.refresh.disabled = false;
    }
  }

  // Persist the cached result on explicit user approval.
  async function saveResult(kind) {
    if (isEphemeralActive()) {
      notice("Chế độ Không lưu: hãy dùng Copy để giữ kết quả.", "warn");
      return;
    }
    const sid = activeSessionId();
    const cur = getCache(kind);
    const r = resultEls(kind);
    if (!sid) { notice("Bắt đầu hoặc chọn một phiên trước khi lưu.", "warn"); return; }
    if (!cur || !cur.markdown) { notice("Chạy trước khi lưu.", "warn"); return; }
    if (r.save) r.save.disabled = true;
    setToolStatus("Đang lưu...");
    try {
      await api(`/api/sessions/${encodeURIComponent(sid)}/summary/save`, {
        method: "POST",
        body: { markdown: cur.markdown, scenario: cur.scenario || TAB_SCENARIO[kind], provider: cur.provider },
      });
      cur.saved = true;
      if (r.save) r.save.textContent = "Đã lưu";
      if (r.meta) r.meta.textContent = [cur.provider ? providerLabel(cur.provider) : "", "đã lưu"].filter(Boolean).join(" · ");
      setToolStatus("Đã lưu.");
    } catch (e) {
      if (r.save) r.save.disabled = false;
      setToolStatus("Không lưu được.", "error");
      notice("Không lưu được: " + (e.message || e), "error");
    }
  }

  async function copyResult(kind) {
    const cur = getCache(kind);
    const md = cur && cur.markdown;
    if (!md) { setToolStatus("Chưa có nội dung để copy.", "warn"); return; }
    const ok = await copyText(String(md));
    setToolStatus(ok ? "Đã copy" : "Không copy được.", ok ? "info" : "error");
  }

  // Clear both result panels' DOM (used when the transcript surface is reset).
  function resetResultPanels() {
    for (const kind of ["summary", "analyze"]) {
      const r = resultEls(kind);
      if (r.output) { r.output.textContent = ""; r.output.classList.remove("result-loading"); }
      if (r.meta) r.meta.textContent = "";
      if (r.fallback) r.fallback.hidden = true;
      if (r.save) r.save.hidden = true;
      if (r.copy) r.copy.hidden = true;
    }
  }

  function providerLabel(value) {
    const found = SUMMARY_PROVIDERS.find((p) => p.value === value);
    return found ? found.label : value;
  }

  function appendInline(parent, text) {
    const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0, match;
    while ((match = re.exec(text)) !== null) {
      if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (token.startsWith("**")) {
        const strong = document.createElement("strong");
        strong.textContent = token.slice(2, -2);
        parent.appendChild(strong);
      } else {
        const code = document.createElement("code");
        code.textContent = token.slice(1, -1);
        parent.appendChild(code);
      }
      last = match.index + token.length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function renderMarkdown(markdown, target) {
    target.textContent = "";
    const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
    let paragraph = [];
    let list = null;
    const flushParagraph = () => {
      if (!paragraph.length) return;
      const p = document.createElement("p");
      appendInline(p, paragraph.join(" "));
      target.appendChild(p);
      paragraph = [];
    };
    const flushList = () => { list = null; };

    for (const raw of lines) {
      const line = raw.trimEnd();
      if (!line.trim()) { flushParagraph(); flushList(); continue; }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushParagraph(); flushList();
        const h = document.createElement(`h${heading[1].length}`);
        appendInline(h, heading[2].trim());
        target.appendChild(h);
        continue;
      }
      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushParagraph(); flushList();
        const q = document.createElement("blockquote");
        appendInline(q, quote[1]);
        target.appendChild(q);
        continue;
      }
      const bullet = line.match(/^[-*]\s+(.+)$/);
      const numbered = line.match(/^\d+\.\s+(.+)$/);
      if (bullet || numbered) {
        flushParagraph();
        const ordered = !!numbered;
        if (!list || (ordered && list.tagName !== "OL") || (!ordered && list.tagName !== "UL")) {
          list = document.createElement(ordered ? "ol" : "ul");
          target.appendChild(list);
        }
        const li = document.createElement("li");
        appendInline(li, (bullet || numbered)[1]);
        list.appendChild(li);
        continue;
      }
      flushList();
      paragraph.push(line.trim());
    }
    flushParagraph();
  }

  /* ============================ RE-DIARIZE (in Settings drawer) ============================ */
  // The "Tách người nói (chính xác)" action lives in the Settings drawer and is
  // rebuilt each time the drawer opens, so we track its current button element here.
  let rediarizeBtn = null;

  function renderRediarizeProgress(st) {
    const raw = st && st.progress;
    const pct = raw == null ? null : (raw <= 1 ? Math.round(raw * 100) : Math.round(raw));
    const stateText = (st && st.state) || "running";
    setToolStatus(`Re-diarize ${stateText}${pct == null ? "" : ` · ${pct}%`}`);
  }

  async function pollRediarizeStatus() {
    const sid = activeSessionId();
    if (!sid) return;
    try {
      const st = await api(`/api/sessions/${encodeURIComponent(sid)}/rediarize/status`);
      renderRediarizeProgress(st);
      const done = ["done", "complete", "completed", "success", "succeeded"].includes(String(st.state || "").toLowerCase());
      const failed = ["error", "failed", "cancelled", "canceled"].includes(String(st.state || "").toLowerCase());
      if (done) {
        clearInterval(state.rediarizeTimer);
        state.rediarizeTimer = null;
        if (rediarizeBtn) rediarizeBtn.disabled = false;
        setToolStatus("Re-diarize complete. Refreshing speaker labels.");
        await refreshTranscriptFromServer();
      } else if (failed) {
        clearInterval(state.rediarizeTimer);
        state.rediarizeTimer = null;
        if (rediarizeBtn) rediarizeBtn.disabled = false;
        setToolStatus("Re-diarize failed.", "error");
      }
    } catch (e) {
      clearInterval(state.rediarizeTimer);
      state.rediarizeTimer = null;
      if (rediarizeBtn) rediarizeBtn.disabled = false;
      setToolStatus("Could not read re-diarize status.", "error");
    }
  }

  async function runRediarize() {
    const sid = activeSessionId();
    if (!sid) { notice("Start or select a session before re-diarizing.", "warn"); return; }
    if (rediarizeBtn) rediarizeBtn.disabled = true;
    clearInterval(state.rediarizeTimer);
    setToolStatus("Starting accurate re-diarize...");
    try {
      await api(`/api/sessions/${encodeURIComponent(sid)}/rediarize`, { method: "POST" });
      state.rediarizeTimer = setInterval(pollRediarizeStatus, 1500);
      await pollRediarizeStatus();
    } catch (e) {
      setToolStatus("Re-diarize could not start.", "error");
      notice("Couldn't start re-diarize: " + (e.message || e), "error");
      if (rediarizeBtn) rediarizeBtn.disabled = false;
    }
  }

  function coerceUtteranceRows(payload) {
    if (Array.isArray(payload)) return payload;
    if (payload && Array.isArray(payload.records)) return payload.records;
    if (payload && Array.isArray(payload.utterances)) return payload.utterances;
    return [];
  }

  async function refreshTranscriptFromServer() {
    const sid = activeSessionId();
    if (!sid) return;
    try {
      const rows = coerceUtteranceRows(await api(`/api/sessions/${encodeURIComponent(sid)}/utterances?since_seq=0`));
      for (const rec of rows) addUtterance(rec);
      renderRecent();
      setToolStatus("Speaker labels refreshed.");
    } catch (e) {
      setToolStatus("Speaker labels could not be refreshed.", "warn");
    }
  }

  // Tab switcher + per-tab controls (refresh / save / copy).
  if (el.tabTranscript) el.tabTranscript.addEventListener("click", () => setTab("transcript"));
  if (el.tabSummary) el.tabSummary.addEventListener("click", () => setTab("summary"));
  if (el.tabAnalyze) el.tabAnalyze.addEventListener("click", () => setTab("analyze"));
  for (const kind of ["summary", "analyze"]) {
    const r = resultEls(kind);
    if (r.refresh) r.refresh.addEventListener("click", () => runResult(kind));
    if (r.save) r.save.addEventListener("click", () => saveResult(kind));
    if (r.copy) r.copy.addEventListener("click", () => copyResult(kind));
  }
  if (el.xViewmode) el.xViewmode.addEventListener("click", () =>
    setTranscriptView(state.transcriptView === "plain" ? "dialogue" : "plain"));
  registerCopyDropdown(el.copyTranscript);
  registerCopyDropdown(el.cCopy);
  el.openFolder.addEventListener("click", openFolder);
  if (el.cFolder) el.cFolder.addEventListener("click", openFolder);
  // (Header dragging is handled purely by the .header-drag underlay + z-index in CSS;
  // no JS needed — the underlay is the click target on every non-button pixel.)

  // Session title -> PUT settings? Title belongs to the session; persist on blur if recording.
  el.xTitle.addEventListener("change", () => {
    // Title is captured at start; here we just keep it local. Server owns session title.
  });

  /* ============================ EXPAND / COLLAPSE ============================ */
  el.cExpand.addEventListener("click", () => setView("expanded"));
  el.xCollapse.addEventListener("click", () => setView("compact"));

  /* ============================ EXIT (Feature 1) ============================ */
  // Primary path: pywebview api.exit() destroys the native window so the Python
  // side finalizes the active session. Browser fallback: POST /api/quit (best-effort
  // stop+finalize) then try to close the tab.
  function quitApp() {
    const rec = !!state.recording;
    const msg = rec
      ? "Đang ghi. Thoát sẽ DỪNG ghi, lưu phiên theo cài đặt rồi thoát AI Record?"
      : "Thoát AI Record?";
    confirmDialog(msg, "Thoát", async () => {
      const pw = window.pywebview;
      if (pw && pw.api && typeof pw.api.exit === "function") {
        try { pw.api.exit(); return; } catch (_) { /* fall through to browser path */ }
      }
      try { await api("/api/quit", { method: "POST" }); } catch (_) { /* best-effort */ }
      try { window.close(); } catch (_) { /* browsers may block */ }
    });
  }
  if (el.xExit) el.xExit.addEventListener("click", quitApp);
  if (el.cExit) el.cExit.addEventListener("click", quitApp);

  /* ============================ CONSENT ============================ */
  function openConsent() { el.consent.hidden = false; }
  el.consentAgree.addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "PUT", body: { consent_acknowledged: true } });
      state.consentOk = true;
      refreshToggleEnabled();
      el.consent.hidden = true;
    } catch (e) {
      notice("Couldn't save consent: " + (e.message || e), "error");
    }
  });

  /* ============================ PREFLIGHT ============================ */
  // Interpret a preflight field into a badge class + label + detail.
  function pfBadge(ok) {
    if (ok === true) return "ok";
    if (ok === false) return "fail";
    return "unknown";
  }
  function pfSymbol(cls) {
    return cls === "ok" ? "✓" : cls === "fail" ? "!" : cls === "warn" ? "!" : "?";
  }
  function pfRow(label, cls, detail) {
    const row = document.createElement("div");
    row.className = "pf-row";
    const badge = document.createElement("span");
    badge.className = "pf-badge " + cls;
    badge.textContent = pfSymbol(cls);
    const lbl = document.createElement("span");
    lbl.className = "pf-label";
    lbl.textContent = label;
    const det = document.createElement("span");
    det.className = "pf-detail";
    det.textContent = detail || "";
    row.append(badge, lbl, det);
    return row;
  }

  async function runPreflight(opts) {
    el.pfRows.textContent = "Checking…";
    let pf;
    try { pf = await api("/api/preflight"); }
    catch (e) { el.preflight.hidden = false; el.pfRows.textContent = ""; el.pfRows.appendChild(pfRow("Preflight request failed", "fail", e.message || "")); return; }

    state.preflight = pf;   // stash so Settings can show the effective preset model
    el.pfRows.textContent = "";
    // CUDA (+ version)
    el.pfRows.appendChild(pfRow(
      "CUDA GPU acceleration", pfBadge(pf.cuda),
      pf.cuda ? (pf.cuda_version ? `CUDA ${pf.cuda_version}` : "available")
              : (pf.cuda === false ? "not detected — CPU fallback" : "unknown")));
    // Whisper loadable
    el.pfRows.appendChild(pfRow("Whisper model loadable", pfBadge(pf.whisper_loadable),
      pf.whisper_loadable === false ? "model failed to load" : ""));
    // Model cache + disk
    {
      const cls = pf.model_cache ? "ok" : "warn";
      const disk = pf.disk_free_gb != null ? `${pf.disk_free_gb} GB free` : "";
      const cacheTxt = pf.model_cache
        ? (typeof pf.model_cache === "string" ? pf.model_cache : "cached")
        : "model not yet cached (will download on first use)";
      el.pfRows.appendChild(pfRow("Model cache & disk space", cls, [cacheTxt, disk].filter(Boolean).join(" · ")));
    }
    // HF terms
    el.pfRows.appendChild(pfRow("Hugging Face model terms", pf.hf_terms_ok ? "ok" : "warn",
      pf.hf_terms_ok ? "accepted" : "not accepted — some models may be unavailable"));
    // CLI availability
    el.pfRows.appendChild(pfRow("Command-line tools", pfBadge(pf.cli_available),
      pf.cli_available === false ? "not on PATH" : ""));

    // Preset explanation
    el.pfPreset.className = "pf-preset";
    el.pfPreset.textContent = "";
    const preset = pf.preset || (state.settings && state.settings.hardware_preset) || "unknown";
    const name = document.createElement("div");
    name.innerHTML = `Auto-selected hardware preset: <span class="p-name"></span>`;
    name.querySelector(".p-name").textContent = preset;
    el.pfPreset.appendChild(name);
    const explain = document.createElement("div");
    explain.className = "muted";
    explain.style.marginTop = "4px";
    explain.textContent = presetExplain(preset);
    el.pfPreset.appendChild(explain);
    if (String(preset).toLowerCase() === "cpu") {
      el.pfPreset.classList.add("cpu-warn");
      const w = document.createElement("div");
      w.className = "warn-text";
      w.textContent = "No GPU detected. Real-time transcription, live translation, and speaker labels are limited on CPU; transcripts may lag or run after the session.";
      el.pfPreset.appendChild(w);
    }

    // Auto-skip the preflight gate when nothing is BLOCKING (user request).
    // hf_terms (HF token) and ollama are optional warnings, not blockers.
    const blocking = pf.whisper_loadable === false
      || (typeof pf.disk_free_gb === "number" && pf.disk_free_gb < 2);
    if (opts && opts.auto && !blocking) {
      el.preflight.hidden = true;
      setView("compact");
    } else {
      el.preflight.hidden = false;
    }
  }
  function presetExplain(p) {
    switch (String(p).toLowerCase()) {
      case "cpu": return "Chosen because no supported GPU was found.";
      case "cuda": case "gpu": return "A CUDA GPU was found and will be used for real-time transcription.";
      default: return "Selected automatically based on the detected hardware.";
    }
  }
  el.pfRefresh.addEventListener("click", () => runPreflight());
  el.pfContinue.addEventListener("click", () => { el.preflight.hidden = true; setView("compact"); });

  /* ============================ SETTINGS DRAWER ============================ */
  async function openSettings() {
    el.settings.hidden = false;
    buildSettings();
    await loadLanguages();
    buildSettings();
    // Populate the Ollama model picker from the catalog (best-effort; async).
    loadModelCatalogInto();
  }

  async function loadModelCatalogInto() {
    try {
      state.modelCatalog = await api("/api/models/catalog");
    } catch (_) {
      state.modelCatalog = null;
    }
    fillModelPicker();
  }
  el.xSettings.addEventListener("click", openSettings);
  if (el.cSettings) el.cSettings.addEventListener("click", openSettings);
  el.setClose.addEventListener("click", () => { el.settings.hidden = true; });
  el.settings.addEventListener("click", (e) => { if (e.target === el.settings) el.settings.hidden = true; });

  // Small builders for settings controls.
  function rowSelect(label, sub, value, options, onChange) {
    const row = mkRow(label, sub);
    const sel = document.createElement("select");
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = typeof opt === "string" ? opt : opt.value;
      o.textContent = typeof opt === "string" ? opt : opt.label;
      if (o.value === String(value)) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => onChange(sel.value));
    row.querySelector(".ctl").appendChild(sel);
    return row;
  }
  // Build options for a "preset-driven" setting: an empty value means "Auto —
  // follow the detected preset". The Auto option shows the effective value from
  // preflight (e.g. "large-v3") so the empty override never looks like "tiny".
  function optsWithAuto(cur, standard, effective) {
    const auto = { value: "", label: "Auto — theo preset" + (effective ? ` (${effective})` : "") };
    const list = [auto, ...standard];
    if (cur && !standard.includes(cur)) list.push(cur);  // preserve a custom override
    return list;
  }
  function rowToggle(label, sub, checked, onChange) {
    const row = mkRow(label, sub);
    const wrap = document.createElement("label");
    wrap.className = "switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!checked;
    const track = document.createElement("span");
    track.className = "track";
    wrap.append(input, track);
    input.addEventListener("change", () => onChange(input.checked));
    row.querySelector(".ctl").appendChild(wrap);
    return row;
  }
  function rowLanguages(label, sub, selected, languages) {
    const row = mkRow(label, sub);
    const picker = document.createElement("div");
    picker.className = "lang-picker";
    const current = Array.isArray(selected) ? selected.slice() : [];
    const options = languages.length ? languages : current.map((code) => ({ code, label: code }));

    const any = document.createElement("button");
    any.type = "button";
    any.className = "lang-chip";
    any.textContent = "Any non-target";
    any.setAttribute("aria-pressed", current.length ? "false" : "true");
    any.addEventListener("click", async () => {
      await putSetting({ source_languages: [] });
      buildSettings();
    });
    picker.appendChild(any);

    if (!options.length) {
      const empty = document.createElement("span");
      empty.className = "lang-empty";
      empty.textContent = "Language list unavailable";
      picker.appendChild(empty);
    }

    for (const lang of options) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "lang-chip";
      chip.textContent = lang.label || lang.code;
      chip.title = lang.code;
      chip.setAttribute("aria-pressed", current.includes(lang.code) ? "true" : "false");
      chip.addEventListener("click", async () => {
        const next = current.includes(lang.code)
          ? current.filter((code) => code !== lang.code)
          : current.concat(lang.code);
        await putSetting({ source_languages: next });
        buildSettings();
      });
      picker.appendChild(chip);
    }
    row.querySelector(".ctl").appendChild(picker);
    return row;
  }
  function rowText(label, sub, value, onCommit, opts = {}) {
    const row = mkRow(label, sub);
    const input = document.createElement("input");
    input.type = opts.number ? "number" : "text";
    input.value = value != null ? value : "";
    if (opts.min != null) input.min = opts.min;
    input.addEventListener("change", () => onCommit(opts.number ? Number(input.value) : input.value));
    row.querySelector(".ctl").appendChild(input);
    return row;
  }
  // Stacked row with a full-width, resizable textarea + reset/save buttons.
  // ``key`` is the summary scenario ("reformat" | "analyze"); ``value`` is the
  // current prompt (falls back to the embedded default when the server omits it).
  function rowPrompt(label, sub, key, value) {
    const row = document.createElement("div");
    row.className = "set-row prompt-row";
    const l = document.createElement("div");
    l.className = "lbl";
    l.textContent = label;
    if (sub) { const sm = document.createElement("small"); sm.textContent = sub; l.appendChild(sm); }
    row.appendChild(l);

    const ta = document.createElement("textarea");
    ta.className = "prompt-box";
    ta.rows = 7;
    ta.spellcheck = false;
    ta.value = value != null && value !== "" ? value : (DEFAULT_SUMMARY_PROMPTS[key] || "");
    // Save on blur when the text actually changed.
    ta.addEventListener("change", () => putSummaryScenario(key, ta.value));
    row.appendChild(ta);

    const actions = document.createElement("div");
    actions.className = "prompt-actions";
    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "btn subtle";
    resetBtn.textContent = "Khôi phục mặc định";
    resetBtn.addEventListener("click", async () => {
      ta.value = DEFAULT_SUMMARY_PROMPTS[key] || "";
      await putSummaryScenario(key, ta.value);
      notice("Đã khôi phục prompt mặc định.", "info");
    });
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn primary";
    saveBtn.textContent = "Lưu prompt";
    saveBtn.addEventListener("click", async () => {
      await putSummaryScenario(key, ta.value);
      notice("Đã lưu prompt.", "info");
    });
    actions.append(resetBtn, saveBtn);
    row.appendChild(actions);
    return row;
  }

  // Merge one scenario into the existing summary_scenarios map and PUT the whole map
  // (so the other scenarios — minutes/study_notes/action_tracker/article — are kept).
  async function putSummaryScenario(key, text) {
    const s = state.settings || {};
    const merged = Object.assign({}, s.summary_scenarios || {});
    merged[key] = text;
    await putSetting({ summary_scenarios: merged });
  }

  function rowReadonly(label, sub, value) {
    const row = mkRow(label, sub);
    row.classList.add("readonly");
    const v = document.createElement("span");
    v.className = "val";
    v.textContent = value != null && value !== "" ? String(value) : "—";
    row.querySelector(".ctl").appendChild(v);
    return row;
  }
  function mkRow(label, sub) {
    const row = document.createElement("div");
    row.className = "set-row";
    const l = document.createElement("div");
    l.className = "lbl";
    l.textContent = label;
    if (sub) { const s = document.createElement("small"); s.textContent = sub; l.appendChild(s); }
    const c = document.createElement("div");
    c.className = "ctl";
    row.append(l, c);
    return row;
  }
  function group(title) {
    const g = document.createElement("div");
    g.className = "set-group";
    const h = document.createElement("h3");
    h.textContent = title;
    g.appendChild(h);
    return g;
  }

  function normalizeLanguageEntry(entry) {
    if (typeof entry === "string") return { code: entry, label: entry };
    if (!entry || typeof entry !== "object") return null;
    const code = entry.code || entry.lang || entry.id || entry.value;
    if (!code) return null;
    const name = entry.name || entry.label || entry.native_name || entry.display || code;
    return { code: String(code), label: `${name} (${code})` };
  }

  function normalizeLanguagesPayload(payload) {
    let list = payload;
    if (payload && !Array.isArray(payload) && typeof payload === "object") {
      list = payload.languages || payload.items || Object.entries(payload).map(([code, name]) => ({ code, name }));
    }
    if (!Array.isArray(list)) return [];
    const seen = new Set();
    const out = [];
    for (const item of list) {
      const lang = normalizeLanguageEntry(item);
      if (lang && !seen.has(lang.code)) {
        seen.add(lang.code);
        out.push(lang);
      }
    }
    return out;
  }

  async function loadLanguages() {
    try {
      state.languages = normalizeLanguagesPayload(await api("/api/languages"));
    } catch (_) {
      state.languages = [];
    }
  }

  function ensureSelectValue(select, value, label) {
    if (!value) return;
    if (![...select.options].some((opt) => opt.value === value)) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label || value;
      select.appendChild(opt);
    }
    select.value = value;
  }

  async function putSetting(patch) {
    try {
      const updated = await api("/api/settings", { method: "PUT", body: patch });
      state.settings = updated || Object.assign(state.settings || {}, patch);
      applyTheme();
      updateTranslateButtons();
      if ("translate_enabled" in patch) refreshTranslationRows();
    } catch (e) {
      notice("Couldn't save setting: " + (e.message || e), "error");
    }
  }

  function buildSettings() {
    const s = state.settings || {};
    el.setBody.textContent = "";

    /* --- Hardware --- */
    const gHw = group("Hardware");
    gHw.appendChild(rowReadonly("Detected preset", "auto-selected at launch", s.hardware_preset));
    const pf = state.preflight || {};
    gHw.appendChild(rowSelect("Whisper model", "Trống = tự chọn theo preset máy", s.whisper_model,
      optsWithAuto(s.whisper_model, ["tiny", "base", "small", "medium", "large-v3"], pf.whisper_model),
      (v) => putSetting({ whisper_model: v })));
    gHw.appendChild(rowSelect("Compute type", null, s.whisper_compute_type,
      optsWithAuto(s.whisper_compute_type, ["int8", "int8_float16", "float16", "float32"], pf.compute_type),
      (v) => putSetting({ whisper_compute_type: v })));
    gHw.appendChild(rowSelect("Latency mode", null, s.latency_mode,
      ["low", "balanced", "accurate", s.latency_mode].filter(uniq),
      (v) => putSetting({ latency_mode: v })));
    el.setBody.appendChild(gHw);

    /* --- Translation: two dropdowns (From / To); on/off lives in the toolbar popover --- */
    const gTr = group("Dịch");
    const src = Array.isArray(s.source_languages) ? s.source_languages : [];
    const fromVal = src.length ? src[0] : "en";
    const langOpts = langOptions().map((l) => ({ value: l.code, label: l.label || l.code }));
    gTr.appendChild(rowSelect("Dịch từ", "ngôn ngữ nguồn (Auto = mọi ngôn ngữ)", fromVal,
      [{ value: "auto", label: "Auto (mọi ngôn ngữ)" }].concat(langOpts),
      (v) => putSetting({ source_languages: v === "auto" ? [] : [v] })));
    gTr.appendChild(rowSelect("Dịch sang", "ngôn ngữ đích", s.target_lang || "vi",
      langOpts,
      (v) => putSetting({ target_lang: v })));
    gTr.appendChild(rowSelect("Provider", null, s.translation_provider,
      ["nllb", "gemini", s.translation_provider].filter(uniq),
      (v) => putSetting({ translation_provider: v })));
    gTr.appendChild(rowSelect("Device", null, s.translation_device,
      ["auto", "cuda", "cpu", s.translation_device].filter(uniq),
      (v) => putSetting({ translation_device: v })));
    el.setBody.appendChild(gTr);

    /* --- Speakers --- */
    const gSp = group("Speaker labels");
    gSp.appendChild(rowToggle("Diarization", "identify who is speaking",
      s.diarization_enabled, (v) => putSetting({ diarization_enabled: v })));
    gSp.appendChild(rowToggle("Real-time labels", "label speakers live (needs GPU)",
      s.diarization_realtime, (v) => putSetting({ diarization_realtime: v })));
    // Accurate offline re-diarization for the open/recording session (moved here
    // from the transcript action row to keep that row clean).
    const rdRow = mkRow("Tách người nói (chính xác)", "chạy lại diarization offline cho phiên đang mở");
    const rdBtn = document.createElement("button");
    rdBtn.type = "button";
    rdBtn.id = "rediarize-run";
    rdBtn.className = "btn subtle";
    rdBtn.textContent = "Tách người nói";
    rdBtn.disabled = !activeSessionId();
    if (!activeSessionId()) rdBtn.title = "Mở một phiên hoặc đang ghi để dùng.";
    rdBtn.addEventListener("click", runRediarize);
    rdRow.querySelector(".ctl").appendChild(rdBtn);
    gSp.appendChild(rdRow);
    rediarizeBtn = rdBtn;
    el.setBody.appendChild(gSp);

    /* --- Storage --- */
    const gStore = group("Storage");
    gStore.appendChild(rowText("Sessions folder", null, s.sessions_root,
      (v) => putSetting({ sessions_root: v })));
    gStore.appendChild(rowText("Retention (days)", "0 keeps sessions forever", s.retention_days,
      (v) => putSetting({ retention_days: v }), { number: true, min: 0 }));
    el.setBody.appendChild(gStore);

    /* --- Output files (bound to the SAME output_formats as the toolbar dropdown) --- */
    const gOut = group("Lưu kết quả");
    const outSet = currentOutputFormats();
    gOut.appendChild(rowReadonly("Bản ghi (.md)", "transcript.md — luôn lưu", "luôn lưu"));
    gOut.appendChild(rowToggle("Kèm .txt", "cũng lưu transcript.txt dạng văn bản thuần",
      outSet.has("txt"), (v) => setOutputFormat("txt", v)));
    gOut.appendChild(rowToggle("Kèm audio (.mp3)", "giữ audio, chuyển sang mp3 khi kết thúc",
      outSet.has("mp3"), (v) => setOutputFormat("mp3", v)));
    gOut.appendChild(rowToggle("AI summary", "tự tạo + lưu tóm tắt khi kết thúc phiên",
      outSet.has("summary"), (v) => setOutputFormat("summary", v)));
    el.setBody.appendChild(gOut);

    /* --- Appearance --- */
    const gAppr = group("Appearance");
    gAppr.appendChild(rowSelect("Theme", null, s.theme || "auto",
      [{ value: "auto", label: "Auto (system)" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }],
      (v) => putSetting({ theme: v })));
    el.setBody.appendChild(gAppr);

    /* --- Secrets --- */
    const gSec = group("Secrets");
    gSec.appendChild(secretRow("Hugging Face token", "hf_token", s.hf_token_is_set));
    gSec.appendChild(secretRow("Gemini API key", "gemini_api_key", s.gemini_api_key_is_set));
    el.setBody.appendChild(gSec);

    /* --- Sessions --- */
    const gSess = group("Sessions");
    const list = document.createElement("div");
    list.className = "sess-list";
    list.id = "sess-list";
    list.textContent = "Loading…";
    gSess.appendChild(list);
    el.setBody.appendChild(gSess);
    loadSessionsInto();

    /* --- Tóm tắt (Summary/Analyze use this provider + fixed scenarios) --- */
    const gSum = group("Tóm tắt");
    const selectedSummaryProvider = s.summarizer_provider || s.summary_provider || "claude_cli";
    const summaryProviderOptions = SUMMARY_PROVIDERS.slice();
    if (!summaryProviderOptions.some((p) => p.value === selectedSummaryProvider)) {
      summaryProviderOptions.push({ value: selectedSummaryProvider, label: selectedSummaryProvider });
    }
    gSum.appendChild(rowSelect("Provider", "dùng cho nút Summary (tóm tắt) và Analyze (phân tích)",
      selectedSummaryProvider,
      summaryProviderOptions,
      (v) => putSetting({ summarizer_provider: v })));
    gSum.appendChild(rowToggle("Use translations", "feed Vietnamese text to summaries when available",
      s.summary_use_translation, (v) => putSetting({ summary_use_translation: v })));
    gSum.appendChild(buildModelPickerRow());
    el.setBody.appendChild(gSum);
    fillModelPicker();

    /* --- Kết nối AI (per-machine sign-in / status / test) --- */
    const gConn = group("Kết nối AI");
    const connNote = document.createElement("p");
    connNote.className = "set-note";
    connNote.textContent =
      "Đăng nhập theo tài khoản của CHÍNH máy này. App không lưu, không đọc, " +
      "không chia sẻ thông tin đăng nhập — mỗi máy kết nối độc lập.";
    gConn.appendChild(connNote);
    const connList = document.createElement("div");
    connList.className = "conn-list";
    connList.id = "conn-list";
    connList.textContent = "Đang kiểm tra kết nối…";
    gConn.appendChild(connList);
    el.setBody.appendChild(gConn);
    loadProviderStatusInto();

    /* --- Prompt AI (Summary / Analyze): editable instruction prompts --- */
    const gPrompt = group("Prompt AI (Summary / Analyze)");
    const pNote = document.createElement("p");
    pNote.className = "set-note";
    pNote.textContent =
      "Chỉnh cách AI tóm tắt/phân tích. Nội dung transcript được tự động thêm vào sau prompt.";
    gPrompt.appendChild(pNote);
    const scen = (s.summary_scenarios && typeof s.summary_scenarios === "object")
      ? s.summary_scenarios : {};
    gPrompt.appendChild(rowPrompt(
      "Prompt Summary",
      "tab Summary — định dạng lại, giữ nguyên câu chữ (reformat)",
      "reformat",
      scen.reformat));
    gPrompt.appendChild(rowPrompt(
      "Prompt Analyze",
      "tab Analyze — phân tích tổng quát + phản biện (analyze)",
      "analyze",
      scen.analyze));
    el.setBody.appendChild(gPrompt);

    /* --- Legal --- */
    const gLegal = group("Legal");
    const legal = mkRow("Consent notice", "review the recording terms");
    const lBtn = document.createElement("button");
    lBtn.className = "link-btn"; lBtn.textContent = "Legal & Consent";
    lBtn.addEventListener("click", () => { el.consent.hidden = false; });
    legal.querySelector(".ctl").appendChild(lBtn);
    gLegal.appendChild(legal);
    if (s.app_version) gLegal.appendChild(rowReadonly("App version", null, s.app_version));
    el.setBody.appendChild(gLegal);
  }

  /* --- Ollama model picker (catalog-driven; applies to the Ollama provider) --- */
  function buildModelPickerRow() {
    const row = mkRow("Ollama model", "local model for the Ollama summarizer provider");
    const ctl = row.querySelector(".ctl");
    ctl.classList.add("model-picker");

    const sel = document.createElement("select");
    sel.id = "ollama-model-select";
    ctl.appendChild(sel);

    const custom = document.createElement("input");
    custom.type = "text";
    custom.id = "ollama-model-custom";
    custom.placeholder = "e.g. qwen2.5:32b";
    custom.hidden = true;
    ctl.appendChild(custom);

    sel.addEventListener("change", () => {
      if (sel.value === "__custom__") {
        custom.hidden = false;
        custom.focus();
        return;
      }
      custom.hidden = true;
      if (sel.value) putSetting({ ollama_model: sel.value });
    });
    custom.addEventListener("change", () => {
      const v = custom.value.trim();
      if (v) putSetting({ ollama_model: v });
    });

    return row;
  }

  function fillModelPicker() {
    const sel = document.getElementById("ollama-model-select");
    const custom = document.getElementById("ollama-model-custom");
    if (!sel) return; // drawer not open / row not built
    const cat = state.modelCatalog;
    const s = state.settings || {};
    const current = (cat && cat.current) || s.ollama_model || "";
    const installed = new Set((cat && cat.installed) || []);
    const defaultTag = (cat && cat.default) || "";

    sel.textContent = "";
    const models = (cat && cat.models) || [];
    if (!models.length) {
      // Catalog unavailable — still let the user set/keep a custom tag.
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = current || "(catalog unavailable)";
      opt.selected = true;
      sel.appendChild(opt);
    } else {
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.tag;
        const vram = m.vram_gb != null ? `~${m.vram_gb}GB` : "";
        const parts = [m.params, vram, m.langs].filter(Boolean).join(" · ");
        const marks = [];
        if (installed.has(m.tag)) marks.push("✓");
        if (m.tag === defaultTag) marks.push("default");
        else if (m.recommended) marks.push("recommended");
        const suffix = marks.length ? `  [${marks.join(", ")}]` : "";
        opt.textContent = `${m.tag} — ${parts}${suffix}`;
        sel.appendChild(opt);
      }
    }

    // "Custom…" free-text escape hatch for any tag not in the catalog.
    const customOpt = document.createElement("option");
    customOpt.value = "__custom__";
    customOpt.textContent = "Custom…";
    sel.appendChild(customOpt);

    // Reflect the current setting: pick it if listed, else show it as custom.
    const known = models.some((m) => m.tag === current);
    if (current && known) {
      sel.value = current;
      if (custom) custom.hidden = true;
    } else if (current) {
      sel.value = "__custom__";
      if (custom) { custom.hidden = false; custom.value = current; }
    }
  }

  /* --- Kết nối AI: fetch + render provider connection status --- */
  async function loadProviderStatusInto() {
    const list = document.getElementById("conn-list");
    if (!list) return; // drawer not open / panel not built
    let data;
    try {
      data = await api("/api/providers/status");
    } catch (_) {
      list.textContent = "Không tải được trạng thái kết nối.";
      return;
    }
    renderProviderStatus(list, data);
  }

  // Map one provider status entry → a status chip {sym, text, cls}.
  function providerChip(p) {
    if (p.kind === "local") {
      return p.ready
        ? { sym: "●", text: "Offline OK", cls: "ok" }
        : { sym: "✗", text: "Chưa chạy", cls: "fail" };
    }
    if (p.kind === "api") {
      return p.ready
        ? { sym: "✓", text: "Sẵn sàng", cls: "ok" }
        : { sym: "⚠", text: "Chưa có key", cls: "warn" };
    }
    // CLI
    if (!p.installed) return { sym: "✗", text: "Chưa cài", cls: "fail" };
    if (p.signed_in === true) return { sym: "✓", text: "Sẵn sàng", cls: "ok" };
    if (p.signed_in === false) return { sym: "⚠", text: "Chưa đăng nhập", cls: "warn" };
    return { sym: "⚠", text: "Chưa rõ đăng nhập", cls: "warn" };
  }

  function setChip(chip, cls, text) {
    chip.className = "conn-chip " + cls;
    chip.textContent = text;
  }

  function renderProviderStatus(list, data) {
    list.textContent = "";
    const providers = (data && data.providers) || [];
    if (!providers.length) {
      list.textContent = "Không có provider nào.";
      return;
    }
    for (const p of providers) {
      const row = document.createElement("div");
      row.className = "conn-row";
      row.dataset.name = p.name;

      const info = document.createElement("div");
      info.className = "conn-info";
      const head = document.createElement("div");
      head.className = "conn-head";
      const label = document.createElement("span");
      label.className = "conn-label";
      label.textContent = p.label || p.name;
      const chip = document.createElement("span");
      const c = providerChip(p);
      setChip(chip, c.cls, c.sym + " " + c.text);
      head.append(label, chip);
      info.appendChild(head);
      if (p.detail) {
        const d = document.createElement("small");
        d.className = "conn-detail";
        d.textContent = p.detail;
        info.appendChild(d);
      }

      const actions = document.createElement("div");
      actions.className = "conn-actions";
      if (p.kind === "cli") {
        const loginBtn = document.createElement("button");
        loginBtn.type = "button";
        loginBtn.className = "btn subtle";
        loginBtn.textContent = "Đăng nhập";
        loginBtn.disabled = !p.installed;
        if (!p.installed) loginBtn.title = "Cài CLI này trên máy trước khi đăng nhập.";
        loginBtn.addEventListener("click", () => providerLogin(p.name));
        actions.appendChild(loginBtn);
      } else if (p.kind === "api") {
        const keyBtn = document.createElement("button");
        keyBtn.type = "button";
        keyBtn.className = "btn subtle";
        keyBtn.textContent = p.ready ? "Đổi API key" : "Đặt API key";
        keyBtn.addEventListener("click", async () => {
          await setSecret("gemini_api_key", "Gemini API key");
        });
        actions.appendChild(keyBtn);
      }
      const testBtn = document.createElement("button");
      testBtn.type = "button";
      testBtn.className = "btn";
      testBtn.textContent = "Kiểm tra";
      testBtn.addEventListener("click", () => providerTest(p.name, chip, testBtn));
      actions.appendChild(testBtn);

      row.append(info, actions);
      list.appendChild(row);
    }
  }

  async function providerLogin(name) {
    try {
      const r = await api(`/api/providers/${name}/login`, { method: "POST" });
      notice((r && r.hint) || "Đã mở cửa sổ đăng nhập.", "info");
    } catch (e) {
      const msg = (e.data && (e.data.error || e.data.detail)) || e.message;
      notice("Không mở được đăng nhập: " + msg, "error");
    }
  }

  async function providerTest(name, chip, btn) {
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Đang kiểm tra…";
    try {
      const r = await api(`/api/providers/${name}/test`, { method: "POST" });
      if (r && r.ok) {
        setChip(chip, "ok", "✓ Đã kết nối");
        notice("Kết nối OK.", "info");
      } else {
        setChip(chip, "fail", "✗ Lỗi");
        notice("Kết nối lỗi: " + ((r && r.error) || "unknown"), "error");
      }
    } catch (e) {
      setChip(chip, "fail", "✗ Lỗi");
      const msg = (e.data && (e.data.error || e.data.detail)) || e.message;
      notice("Kiểm tra lỗi: " + msg, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  }

  function uniq(v, i, arr) { return v != null && v !== "" && arr.indexOf(v) === i; }

  function secretRow(label, name, isSet) {
    const row = mkRow(label, isSet ? null : "not configured");
    const state_span = document.createElement("span");
    state_span.className = "secret-state " + (isSet ? "set" : "unset");
    state_span.textContent = isSet ? "is set" : "not set";
    const setBtn = document.createElement("button");
    setBtn.className = "link-btn";
    setBtn.textContent = isSet ? "Replace" : "Set";
    setBtn.addEventListener("click", () => setSecret(name, label));
    const ctl = row.querySelector(".ctl");
    ctl.appendChild(state_span);
    ctl.appendChild(setBtn);
    if (isSet) {
      const clrBtn = document.createElement("button");
      clrBtn.className = "link-btn danger-link";
      clrBtn.style.color = "var(--fail)";
      clrBtn.textContent = "Clear";
      clrBtn.addEventListener("click", () => clearSecret(name, label));
      ctl.appendChild(clrBtn);
    }
    return row;
  }

  async function setSecret(name, label) {
    const value = window.prompt(`Enter ${label}. It is stored write-only and never shown again.`);
    if (value == null) return;
    const v = value.trim();
    if (!v) return;
    try {
      await api(`/api/secrets/${name}`, { method: "POST", body: { value: v } });
      notice(`${label} saved.`, "info");
      await refreshSettings();
      buildSettings();
    } catch (e) { notice(`Couldn't save ${label}: ` + (e.message || e), "error"); }
  }
  async function clearSecret(name, label) {
    confirmDialog(`Clear the stored ${label}? This cannot be undone.`, "Clear", async () => {
      try {
        await api(`/api/secrets/${name}`, { method: "DELETE" });
        notice(`${label} cleared.`, "info");
        await refreshSettings();
        buildSettings();
      } catch (e) { notice(`Couldn't clear ${label}: ` + (e.message || e), "error"); }
    });
  }

  /* ============================ SESSIONS LIST ============================ */
  async function loadSessionsInto() {
    const list = document.getElementById("sess-list");
    if (!list) return; // drawer not open
    let sessions;
    try { sessions = await api("/api/sessions"); }
    catch (e) { list.textContent = ""; const p = document.createElement("div"); p.className = "sess-empty"; p.textContent = "Couldn't load sessions."; list.appendChild(p); return; }

    list.textContent = "";
    if (!sessions || !sessions.length) {
      const p = document.createElement("div");
      p.className = "sess-empty";
      p.textContent = "No sessions yet.";
      list.appendChild(p);
      return;
    }
    for (const meta of sessions) {
      list.appendChild(sessionItem(meta));
    }
  }

  function sessionItem(meta) {
    const item = document.createElement("div");
    item.className = "sess-item";
    const top = document.createElement("div");
    top.className = "s-top";
    const title = document.createElement("div");
    title.className = "s-title";
    title.textContent = meta.title || "Untitled session";
    const m = document.createElement("div");
    m.className = "s-meta";
    m.textContent = [fmtDate(meta.created_at), fmtDur(meta.duration_sec)].filter(Boolean).join(" · ");
    top.append(title, m);

    const actions = document.createElement("div");
    actions.className = "s-actions";
    const delAudio = document.createElement("button");
    delAudio.className = "link-btn";
    delAudio.textContent = "Delete audio only";
    delAudio.addEventListener("click", () => {
      confirmDialog(`Delete the audio file for "${meta.title || "this session"}"? The transcript is kept.`, "Delete audio", async () => {
        try { await api(`/api/sessions/${meta.session_id}/audio`, { method: "DELETE" }); notice("Audio deleted.", "info"); }
        catch (e) { notice("Couldn't delete audio: " + (e.message || e), "error"); }
      });
    });
    const delAll = document.createElement("button");
    delAll.className = "link-btn danger-link";
    delAll.style.color = "var(--fail)";
    delAll.textContent = "Delete session";
    delAll.addEventListener("click", () => {
      confirmDialog(`Permanently delete "${meta.title || "this session"}" and its transcript? This cannot be undone.`, "Delete", async () => {
        try { await api(`/api/sessions/${meta.session_id}`, { method: "DELETE" }); notice("Session deleted.", "info"); loadSessionsInto(); }
        catch (e) { notice("Couldn't delete session: " + (e.message || e), "error"); }
      });
    });
    actions.append(delAudio, delAll);
    item.append(top, actions);
    return item;
  }

  /* ============================ SESSION BROWSER (Mode A) ============================ */
  // The idle expanded view shows a grid of saved sessions. Clicking one loads its
  // transcript and switches to Mode B (transcript). A search box filters by
  // title/date. This is separate from the compact settings-drawer session list.
  async function loadSessionBrowser() {
    const cont = el.sessionBrowser;
    if (!cont) return;
    cont.textContent = "";
    const loading = document.createElement("div");
    loading.className = "sess-empty";
    loading.textContent = "Loading…";
    cont.appendChild(loading);
    let sessions;
    try {
      sessions = await api("/api/sessions");
    } catch (e) {
      cont.textContent = "";
      const p = document.createElement("div");
      p.className = "sess-empty";
      p.textContent = "Không tải được danh sách phiên.";
      cont.appendChild(p);
      return;
    }
    state.sessionsCache = Array.isArray(sessions) ? sessions : [];
    renderSessionBrowser();
  }

  function renderSessionBrowser() {
    const cont = el.sessionBrowser;
    if (!cont) return;
    const term = (state.browserSearch || "").toLowerCase();
    const list = (state.sessionsCache || []).filter((m) => {
      if (!term) return true;
      const hay = [m.title, fmtDate(m.created_at)].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(term);
    });
    cont.textContent = "";
    if (!list.length) {
      const p = document.createElement("div");
      p.className = "sess-empty";
      p.textContent = term ? "Không có phiên khớp." : "Chưa có phiên nào.";
      cont.appendChild(p);
      return;
    }
    for (const meta of list) cont.appendChild(sessionCard(meta));
  }

  function sessionCard(meta) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "sess-card";
    const title = document.createElement("div");
    title.className = "sc-title";
    title.textContent = meta.title || "Untitled session";
    const m = document.createElement("div");
    m.className = "sc-meta";
    const parts = [fmtDate(meta.created_at), fmtDur(meta.duration_sec)];
    const spk = meta.speaker_count != null
      ? meta.speaker_count
      : (Array.isArray(meta.speakers) ? meta.speakers.length : null);
    if (spk != null) parts.push(`${spk} người nói`);
    m.textContent = parts.filter(Boolean).join(" · ");
    card.append(title, m);
    card.addEventListener("click", () => openSession(meta.session_id, meta.title));
    return card;
  }

  // Reset the transcript surface (used before loading a saved session or a fresh
  // recording), so old rows never bleed into the new one.
  function clearTranscript() {
    el.xTranscript.textContent = "";
    state.utterances.clear();
    state.lastSeq = 0;
    // Reset the two result tabs + return to the live transcript view.
    state.summaryResult = null;
    state.analyzeResult = null;
    resetResultPanels();
    state.tab = "transcript";
    if (el.transcriptPanel) el.transcriptPanel.dataset.tab = "transcript";
    if (el.tabTranscript) el.tabTranscript.setAttribute("aria-selected", "true");
    if (el.tabSummary) el.tabSummary.setAttribute("aria-selected", "false");
    if (el.tabAnalyze) el.tabAnalyze.setAttribute("aria-selected", "false");
    renderRecent();
  }

  async function openSession(sid, fallbackTitle) {
    if (!sid) return;
    setToolStatus("Đang mở phiên…");
    let data;
    try {
      data = await api(`/api/sessions/${encodeURIComponent(sid)}`);
    } catch (e) {
      setToolStatus("");
      notice("Không mở được phiên: " + (e.message || e), "error");
      return;
    }
    clearTranscript();
    state.openedSessionId = sid;
    state.sessionId = sid;               // so summarize / export / rename target it
    if (el.xTitle) el.xTitle.value = (data && data.title) || fallbackTitle || "";
    const rows = coerceUtteranceRows(data && (data.utterances || data.records || data));
    for (const rec of rows) addUtterance(rec);
    // Pre-fill the matching result tab if the session already has a saved summary.
    const sum = data && data.summary;
    const md = sum ? (typeof sum === "string" ? sum : sum.markdown) : "";
    if (md) {
      const scenario = (sum && typeof sum === "object" && sum.scenario) || "reformat";
      const kind = scenario === "analyze" ? "analyze" : "summary";
      setCache(kind, {
        markdown: md,
        scenario,
        provider: sum && typeof sum === "object" ? sum.provider : undefined,
        saved: true,
      });
    }
    setToolStatus("");
    // A saved session is never ephemeral: restore the folder button + save actions.
    updateEphemeralUi();
    setUiMode("transcript");
  }

  if (el.backSessions) {
    el.backSessions.addEventListener("click", () => {
      state.openedSessionId = null;
      // Only clear when not actively recording (keep a live session intact).
      if (!state.recording) { clearTranscript(); state.sessionId = null; }
      setUiMode("browser");
    });
  }
  if (el.browserSearch) {
    el.browserSearch.addEventListener("input", () => {
      state.browserSearch = el.browserSearch.value.trim();
      renderSessionBrowser();
    });
  }

  /* ============================ CONFIRM DIALOG ============================ */
  let confirmHandler = null;
  function confirmDialog(message, okLabel, onOk) {
    el.confirmMsg.textContent = message;
    el.confirmOk.textContent = okLabel || "Confirm";
    confirmHandler = onOk;
    el.confirm.hidden = false;
  }
  el.confirmCancel.addEventListener("click", () => { el.confirm.hidden = true; confirmHandler = null; });
  el.confirmOk.addEventListener("click", async () => {
    const h = confirmHandler;
    el.confirm.hidden = true; confirmHandler = null;
    if (h) await h();
  });

  /* ============================ THEME ============================ */
  function applyTheme() {
    const theme = (state.settings && state.settings.theme) || "auto";
    document.documentElement.setAttribute("data-theme", theme);
  }

  /* ============================ WEBSOCKET ============================ */
  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws?token=${encodeURIComponent(TOKEN || "")}`;
  }

  function connectWs() {
    let ws;
    try { ws = new WebSocket(wsUrl()); }
    catch (e) { scheduleReconnect(); return; }
    state.ws = ws;

    ws.addEventListener("open", () => {
      state.wsBackoff = 500;
      // On reconnect, replay durable events we missed by seq.
      if (state.everConnected && state.lastSeq > 0) catchUp(state.lastSeq);
      state.everConnected = true;
      // Ask the server for a fresh status snapshot.
      requestStatus();
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      handleWsMessage(msg);
    });

    ws.addEventListener("close", () => { state.ws = null; scheduleReconnect(); });
    ws.addEventListener("error", () => { try { ws.close(); } catch (_) {} });
  }

  function scheduleReconnect() {
    clearTimeout(state.wsTimer);
    const delay = state.wsBackoff;
    state.wsBackoff = Math.min(state.wsBackoff * 2, 10000);
    state.wsTimer = setTimeout(connectWs, delay);
  }

  function requestStatus() {
    // Prefer a WS request; fall back to REST if the socket isn't open.
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      try { state.ws.send(JSON.stringify({ type: "get_status" })); return; } catch (_) {}
    }
    api("/api/capture/status").then(applyStatusSnapshot).catch(() => {});
  }

  function applyStatusSnapshot(st) {
    if (!st) return;
    if (typeof st.recording === "boolean") setRecording(st.recording);
    if (st.session_id) state.sessionId = st.session_id;
    // Sync ephemeral mode from the server (e.g. a draft session already running).
    if (typeof st.ephemeral === "boolean" && st.recording) state.ephemeral = st.ephemeral;
    updateEphemeralUi();
    renderStatus(st);
  }

  function patchFieldsFromMessage(msg) {
    if (msg.fields && typeof msg.fields === "object") return msg.fields;
    const fields = {};
    for (const [key, value] of Object.entries(msg)) {
      if (key !== "type" && key !== "seq") fields[key] = value;
    }
    return fields;
  }

  function handleWsMessage(msg) {
    switch (msg.type) {
      case "utterance":
        if (msg.record) addUtterance(msg.record);
        break;
      case "patch":
        if (msg.seq != null) {
          if (msg.seq > state.lastSeq) state.lastSeq = msg.seq;
          patchUtterance(msg.seq, patchFieldsFromMessage(msg));
        }
        break;
      case "status":
        if (typeof msg.recording === "boolean") setRecording(msg.recording);
        renderStatus(msg);
        break;
      case "rename":
        applySpeakerRename(msg.old, msg.new);
        break;
      case "summary": {
        // Backend emits {type:"summary", state:"done", markdown} after a save or the
        // auto-summary at finalize. Route by scenario (analyze → Analyze tab, else
        // Summary tab), cache it, and render if that tab is currently showing.
        const kind = msg.scenario === "analyze" ? "analyze" : "summary";
        if (msg.state === "done" && msg.markdown) {
          const prev = getCache(kind) || {};
          const payload = {
            markdown: msg.markdown,
            scenario: msg.scenario || prev.scenario || TAB_SCENARIO[kind],
            provider: msg.provider || prev.provider,
            reformat_fallback: prev.reformat_fallback,
            saved: true,
          };
          setCache(kind, payload);
          if (state.tab === kind) renderResult(kind, payload);
        } else if (msg.state === "error" && msg.error) {
          setCache(kind, { error: msg.error });
          if (state.tab === kind) renderResult(kind, { error: msg.error });
        }
        break;
      }
      case "rediarize":
        // Backend emits {type:"rediarize", state, detail} — drive progress and, on
        // completion, refresh the transcript to pick up the new speaker labels.
        if (msg.state || msg.progress != null) renderRediarizeProgress(msg);
        if (msg.state === "done") {
          setToolStatus("Re-diarize complete. Refreshing speaker labels.");
          refreshTranscriptFromServer();
        }
        break;
      case "error":
        notice(`${msg.message || "Error"}${msg.code ? " (" + msg.code + ")" : ""}`, "error");
        break;
      default:
        // Unknown message types are ignored on purpose.
        break;
    }
  }

  // Replay durable utterance events by seq after a reconnect gap.
  async function catchUp(sinceSeq) {
    if (!state.sessionId) return;
    try {
      const rows = coerceUtteranceRows(await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/utterances?since_seq=${sinceSeq}`));
      for (const rec of rows) addUtterance(rec);
    } catch (e) { /* best-effort catch-up */ }
  }

  /* ============================ BOOT ============================ */
  async function refreshSettings() {
    state.settings = await api("/api/settings");
    state.consentOk = !!(state.settings && state.settings.consent_acknowledged);
    applyTheme();
    updateTranslateButtons();
    refreshOutputDropdowns();
    refreshToggleEnabled();
    return state.settings;
  }

  async function boot() {
    if (!TOKEN) { el.noToken.hidden = false; return; }
    el.app.hidden = false;

    // 1) Settings drive consent + theme.
    try {
      await refreshSettings();
      await loadLanguages();
      updateTranslateButtons();   // now that languages are in, populate translate popovers
    } catch (e) {
      notice("Couldn't reach the local service. Some features may be unavailable.", "error");
      state.settings = {};
    }

    // 1b) Audio input/output devices for the two toolbar pickers.
    loadAudioDevices();

    // 2) Consent gate.
    if (!state.consentOk) openConsent();

    // 3) Preflight before the first record — auto-skips the gate unless something blocks.
    runPreflight({ auto: true });

    // 4) Current capture status (in case a session is already running).
    try { applyStatusSnapshot(await api("/api/capture/status")); } catch (_) {}
    renderRecent();

    // 5) Live stream.
    connectWs();

    // Default to compact bar underneath the overlays.
    setView("compact");
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!el.settings.hidden) el.settings.hidden = true;
      else if (!el.confirm.hidden) { el.confirm.hidden = true; confirmHandler = null; }
    }
  });

  boot();
})();
