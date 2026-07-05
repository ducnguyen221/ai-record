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
    currentSummary: null,
    modelCatalog: null,      // {default, models, current, installed, ollama_available}
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
  // Summary/analyze use fixed scenarios; provider comes from settings.
  const SCENARIO_LABELS = {
    reformat: "Tóm tắt",
    analyze: "Phân tích",
    minutes: "Meeting minutes",
    study_notes: "Study notes",
    action_tracker: "Action tracker",
    article: "Article",
  };
  const SUMMARY_PROVIDERS = [
    { value: "claude_cli", label: "Claude CLI" },
    { value: "codex_cli", label: "Codex CLI" },
    { value: "gemini", label: "Gemini" },
    { value: "ollama", label: "Ollama" },
  ];

  /* ============================ DOM SHORTCUTS ============================ */
  const $ = (id) => document.getElementById(id);
  const el = {
    app: $("app"),
    // compact
    cToggle: $("c-toggle"), cDot: $("c-dot"), cStatusText: $("c-status-text"),
    cStatus: $("c-status"), cRecent: $("c-recent"), cExpand: $("c-expand"),
    cInput: $("c-input"), cOutputDev: $("c-output-dev"), cScreen: $("c-screen"),
    cTranslate: $("c-translate"), cFolder: $("c-folder"), cExit: $("c-exit"), cSettings: $("c-settings"),
    // expanded
    expanded: $("expanded"),
    xCollapse: $("x-collapse"), xTitle: $("x-title"), xToggle: $("x-toggle"),
    xDot: $("x-dot"), xStatusText: $("x-status-text"), xStatus: $("x-status"),
    xChips: $("x-chips"), xSearch: $("x-search"), xSettings: $("x-settings"),
    xExit: $("x-exit"),
    xTranscript: $("x-transcript"), xJump: $("x-jump"),
    xInput: $("x-input"), xOutputDev: $("x-output-dev"), xScreen: $("x-screen"),
    xTranslate: $("x-translate"),
    // browser mode
    browserSearch: $("browser-search"), sessionBrowser: $("session-browser"),
    backSessions: $("back-sessions"),
    // transcript-mode actions
    sumReformat: $("sum-reformat"), sumAnalyze: $("sum-analyze"),
    copyTranscript: $("copy-transcript"),
    dlTranscriptMd: $("dl-transcript-md"), dlTranscriptTxt: $("dl-transcript-txt"),
    dlSummaryMd: $("dl-summary-md"), dlSummaryTxt: $("dl-summary-txt"),
    dlCombinedMd: $("dl-combined-md"), rediarizeRun: $("rediarize-run"),
    openFolder: $("open-folder"),
    toolStatus: $("tool-status"), summaryArea: $("summary-area"),
    summaryMeta: $("summary-meta"), summaryFallback: $("summary-fallback"),
    summaryOutput: $("summary-output"),
    summarySave: $("summary-save"), summaryCopy: $("summary-copy"),
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

  async function downloadAttachment(path, fallbackName) {
    const res = await fetch(path, { headers: { [HEADER]: TOKEN || "" } });
    if (!res.ok) {
      const err = new Error(`GET ${path} -> ${res.status}`);
      err.status = res.status;
      try { err.data = await res.json(); } catch (_) { /* ignore */ }
      throw err;
    }
    const blob = await res.blob();
    const filename = filenameFromDisposition(res.headers.get("content-disposition")) || fallbackName;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function filenameFromDisposition(disposition) {
    if (!disposition) return "";
    const utf = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf) {
      try { return decodeURIComponent(utf[1].trim().replace(/^"|"$/g, "")); }
      catch (_) { return utf[1].trim().replace(/^"|"$/g, ""); }
    }
    const plain = disposition.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1].trim() : "";
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
      requestResize(560, 200);
    }
  }

  // Swap the area under the expanded header between the saved-session browser
  // (Mode A) and the transcript + actions (Mode B). The toolbar is identical.
  function setUiMode(mode) {
    state.uiMode = mode === "transcript" ? "transcript" : "browser";
    if (el.expanded) el.expanded.dataset.mode = state.uiMode;
    if (state.uiMode === "browser") loadSessionBrowser();
    else scrollToLatest();
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
      btn.textContent = on ? "● Stop" : "Start";
    }
    // Lock the device picker buttons while recording.
    for (const dd of deviceDropdowns) dd.btn.disabled = on;
  }

  function refreshToggleEnabled() {
    const enabled = state.consentOk;
    for (const btn of [el.cToggle, el.xToggle]) {
      btn.disabled = !enabled;
      btn.title = enabled ? "" : "Acknowledge the consent notice before recording.";
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
    if (copyDropdown) {
      copyDropdown.pop.hidden = true;
      copyDropdown.btn.setAttribute("aria-expanded", "false");
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
        const r = await api("/api/capture/start", {
          method: "POST",
          body: { title, input_device: req.input_device, output_device: req.output_device },
        });
        state.sessionId = r && r.session_id;
        setRecording(true);
        renderStatus({ recording: true });
        setUiMode("transcript");
        notice("Recording started.", "info");
      } catch (e) {
        if (e.status === 403) { openConsent(); notice("Please acknowledge consent before recording.", "warn"); }
        else if (e.status === 422) notice("Chọn ít nhất một nguồn để ghi (micro hoặc loa).", "warn");
        else notice("Couldn't start recording: " + (e.message || e), "error");
      }
    }
  }
  el.cToggle.addEventListener("click", toggleRecording);
  el.xToggle.addEventListener("click", toggleRecording);

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

  async function copyTranscript(withSpeakers) {
    const text = withSpeakers ? buildTranscriptText() : buildTranscriptPlainText();
    if (!text.trim()) { setToolStatus("Chưa có nội dung để copy.", "warn"); return; }
    const ok = await copyText(text);
    setToolStatus(ok ? "Đã copy" : "Không copy được.", ok ? "info" : "error");
  }

  /* ---- Copy dropdown: "Chỉ văn bản" | "Kèm người nói" ---- */
  let copyDropdown = null;
  function registerCopyDropdown() {
    const btn = el.copyTranscript;
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
    copyDropdown = { btn, pop };
  }

  async function copySummary() {
    const md = state.currentSummary && state.currentSummary.markdown;
    if (!md) { setToolStatus("Chưa có tóm tắt để copy.", "warn"); return; }
    const ok = await copyText(String(md));
    setToolStatus(ok ? "Đã copy" : "Không copy được.", ok ? "info" : "error");
  }

  function renderSummary(payload) {
    // Error branch: a failed/unavailable summarize (non-ok response) shows the reason
    // in the summary panel instead of a blank "Summary ready" (review I2).
    if (payload && payload.error && !payload.markdown) {
      state.currentSummary = null;
      el.summaryArea.hidden = false;
      el.summaryFallback.hidden = true;
      el.summaryMeta.textContent = "Summary unavailable";
      if (el.summarySave) el.summarySave.hidden = true;
      if (el.summaryCopy) el.summaryCopy.hidden = true;
      renderMarkdown("**Summary failed:** " + String(payload.error), el.summaryOutput);
      return;
    }
    const markdown = payload && payload.markdown ? String(payload.markdown) : "";
    state.currentSummary = payload || null;
    el.summaryArea.hidden = !markdown;
    el.summaryFallback.hidden = !(payload && payload.reformat_fallback);
    const scenario = payload && payload.scenario ? scenarioLabel(payload.scenario) : "Summary";
    const provider = payload && payload.provider ? providerLabel(payload.provider) : "";
    el.summaryMeta.textContent = [scenario, provider].filter(Boolean).join(" · ");
    // A preview exists → offer to persist (Feature 3) + copy (Feature 4). `saved`
    // marks an already-persisted summary (from a save round-trip or WS "done").
    if (el.summarySave) {
      el.summarySave.hidden = !markdown;
      el.summarySave.disabled = false;
      el.summarySave.textContent = payload && payload.saved ? "Đã lưu" : "Lưu tóm tắt";
    }
    if (el.summaryCopy) el.summaryCopy.hidden = !markdown;
    renderMarkdown(markdown, el.summaryOutput);
  }

  /* Persist the previewed summary only on explicit user approval (Feature 3). */
  async function saveSummary() {
    const sid = activeSessionId();
    const cur = state.currentSummary;
    if (!sid) { notice("Start or select a session before saving.", "warn"); return; }
    if (!cur || !cur.markdown) { notice("Run a summary before saving.", "warn"); return; }
    el.summarySave.disabled = true;
    setToolStatus("Đang lưu tóm tắt...");
    try {
      await api(`/api/sessions/${encodeURIComponent(sid)}/summary/save`, {
        method: "POST",
        body: { markdown: cur.markdown, scenario: cur.scenario, provider: cur.provider },
      });
      cur.saved = true;
      el.summarySave.textContent = "Đã lưu";
      setToolStatus("Đã lưu tóm tắt.");
    } catch (e) {
      el.summarySave.disabled = false;
      setToolStatus("Không lưu được tóm tắt.", "error");
      notice("Couldn't save summary: " + (e.message || e), "error");
    }
  }

  function scenarioLabel(value) {
    return SCENARIO_LABELS[value] || value;
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

  // Summary (reformat, verbatim grouping) and Analyze (general analysis + critique)
  // are the same endpoint with a fixed scenario. The provider comes from settings.
  async function runSummarize(scenario, btn) {
    const sid = activeSessionId();
    if (!sid) { notice("Bắt đầu hoặc chọn một phiên trước khi tóm tắt.", "warn"); return; }
    const provider = (state.settings && (state.settings.summarizer_provider || state.settings.summary_provider)) || "claude_cli";
    if (btn) btn.disabled = true;
    setToolStatus(scenario === "analyze" ? "Đang phân tích..." : "Đang tóm tắt...");
    try {
      const payload = await api(`/api/sessions/${encodeURIComponent(sid)}/summarize`, {
        method: "POST",
        body: { scenario, provider },
      });
      renderSummary(payload);
      setToolStatus(payload && payload.reformat_fallback
        ? "Đã định dạng, giữ nguyên câu chữ."
        : (scenario === "analyze" ? "Phân tích xong." : "Tóm tắt xong."));
    } catch (e) {
      // Surface the backend reason (503 unavailable / 502 provider error) in the panel.
      const errText = (e.data && e.data.error) || e.message || String(e);
      renderSummary({ error: errText });
      setToolStatus(scenario === "analyze" ? "Phân tích thất bại." : "Tóm tắt thất bại.", "error");
      notice("Không xử lý được phiên: " + errText, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function downloadExport(what, fmt) {
    const sid = activeSessionId();
    if (!sid) { notice("Start or select a session before downloading.", "warn"); return; }
    const q = new URLSearchParams({ what, fmt });
    const fallback = `${sid}-${what}.${fmt}`;
    setToolStatus(`Preparing ${what} ${fmt.toUpperCase()} download...`);
    try {
      await downloadAttachment(`/api/sessions/${encodeURIComponent(sid)}/export?${q}`, fallback);
      setToolStatus("Download started.");
    } catch (e) {
      setToolStatus("Download failed.", "error");
      notice("Couldn't download export: " + (e.message || e), "error");
    }
  }

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
        el.rediarizeRun.disabled = false;
        setToolStatus("Re-diarize complete. Refreshing speaker labels.");
        await refreshTranscriptFromServer();
      } else if (failed) {
        clearInterval(state.rediarizeTimer);
        state.rediarizeTimer = null;
        el.rediarizeRun.disabled = false;
        setToolStatus("Re-diarize failed.", "error");
      }
    } catch (e) {
      clearInterval(state.rediarizeTimer);
      state.rediarizeTimer = null;
      el.rediarizeRun.disabled = false;
      setToolStatus("Could not read re-diarize status.", "error");
    }
  }

  async function runRediarize() {
    const sid = activeSessionId();
    if (!sid) { notice("Start or select a session before re-diarizing.", "warn"); return; }
    el.rediarizeRun.disabled = true;
    clearInterval(state.rediarizeTimer);
    setToolStatus("Starting accurate re-diarize...");
    try {
      await api(`/api/sessions/${encodeURIComponent(sid)}/rediarize`, { method: "POST" });
      state.rediarizeTimer = setInterval(pollRediarizeStatus, 1500);
      await pollRediarizeStatus();
    } catch (e) {
      setToolStatus("Re-diarize could not start.", "error");
      notice("Couldn't start re-diarize: " + (e.message || e), "error");
      el.rediarizeRun.disabled = false;
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

  if (el.sumReformat) el.sumReformat.addEventListener("click", () => runSummarize("reformat", el.sumReformat));
  if (el.sumAnalyze) el.sumAnalyze.addEventListener("click", () => runSummarize("analyze", el.sumAnalyze));
  if (el.summarySave) el.summarySave.addEventListener("click", saveSummary);
  if (el.summaryCopy) el.summaryCopy.addEventListener("click", copySummary);
  registerCopyDropdown();
  el.dlTranscriptMd.addEventListener("click", () => downloadExport("transcript", "md"));
  el.dlTranscriptTxt.addEventListener("click", () => downloadExport("transcript", "txt"));
  el.dlSummaryMd.addEventListener("click", () => downloadExport("summary", "md"));
  el.dlSummaryTxt.addEventListener("click", () => downloadExport("summary", "txt"));
  el.dlCombinedMd.addEventListener("click", () => downloadExport("combined", "md"));
  el.rediarizeRun.addEventListener("click", runRediarize);
  el.openFolder.addEventListener("click", openFolder);
  if (el.cFolder) el.cFolder.addEventListener("click", openFolder);

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
    gHw.appendChild(rowSelect("Whisper model", null, s.whisper_model,
      ["tiny", "base", "small", "medium", "large-v3", s.whisper_model].filter(uniq),
      (v) => putSetting({ whisper_model: v })));
    gHw.appendChild(rowSelect("Compute type", null, s.whisper_compute_type,
      ["int8", "int8_float16", "float16", "float32", s.whisper_compute_type].filter(uniq),
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
    state.currentSummary = null;
    if (el.summaryArea) el.summaryArea.hidden = true;
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
    // Show a saved summary if the session already has one.
    const sum = data && data.summary;
    const md = sum ? (typeof sum === "string" ? sum : sum.markdown) : "";
    if (md) {
      renderSummary({ markdown: md, scenario: sum.scenario, provider: sum.provider, saved: true });
    }
    setToolStatus("");
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
      case "summary":
        // Backend now emits {type:"summary", state:"done", markdown} only AFTER an
        // explicit save (Feature 3) → mark it saved. Preserve the current preview's
        // scenario/provider labels when this is the client that just saved.
        if (msg.state === "done" && msg.markdown) {
          const cur = state.currentSummary || {};
          renderSummary({
            markdown: msg.markdown,
            scenario: cur.scenario,
            provider: cur.provider,
            reformat_fallback: cur.reformat_fallback,
            saved: true,
          });
        } else if (msg.state === "error" && msg.error) {
          renderSummary({ error: msg.error });
        }
        break;
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
