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
    captureChoice: "both",
    languages: [],
    rediarizeTimer: null,
    currentSummary: null,
  };

  const MAX_ROWS = 500;      // cap DOM nodes for very long transcripts
  const UNKNOWN_SPEAKER = "Speaker ?";
  const CAPTURE_CHOICES = {
    both:   { mode: "meeting",   sources: ["you", "them"] },
    mic:    { mode: "dictation", sources: ["you"] },
    system: { mode: "meeting",   sources: ["them"] },
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
    cSource: $("c-source"), cTranslate: $("c-translate"), cFolder: $("c-folder"),
    // expanded
    xCollapse: $("x-collapse"), xTitle: $("x-title"), xToggle: $("x-toggle"),
    xDot: $("x-dot"), xStatusText: $("x-status-text"), xStatus: $("x-status"),
    xChips: $("x-chips"), xSearch: $("x-search"), xSettings: $("x-settings"),
    xExit: $("x-exit"),
    xTranscript: $("x-transcript"), xJump: $("x-jump"),
    xSource: $("x-source"), xTranslate: $("x-translate"),
    sumScenario: $("sum-scenario"), sumProvider: $("sum-provider"), sumRun: $("sum-run"),
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
    if (expanded) { requestResize(900, 640); scrollToLatest(); }
    else { requestResize(560, 180); }
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
    for (const sel of [el.cSource, el.xSource]) sel.disabled = on;
  }

  function refreshToggleEnabled() {
    const enabled = state.consentOk;
    for (const btn of [el.cToggle, el.xToggle]) {
      btn.disabled = !enabled;
      btn.title = enabled ? "" : "Acknowledge the consent notice before recording.";
    }
  }

  function setCaptureChoice(choice) {
    state.captureChoice = CAPTURE_CHOICES[choice] ? choice : "both";
    for (const sel of [el.cSource, el.xSource]) {
      if (sel.value !== state.captureChoice) sel.value = state.captureChoice;
    }
  }

  function captureRequest() {
    const selected = CAPTURE_CHOICES[state.captureChoice] || CAPTURE_CHOICES.both;
    return { mode: selected.mode, sources: selected.sources.slice() };
  }

  function bindCaptureSource(sel) {
    sel.addEventListener("change", () => setCaptureChoice(sel.value));
  }
  bindCaptureSource(el.cSource);
  bindCaptureSource(el.xSource);

  function updateTranslateButtons() {
    const on = !!(state.settings && state.settings.translate_enabled);
    for (const btn of [el.cTranslate, el.xTranslate]) {
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.title = on ? "Translation is on" : "Translation is off";
    }
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
  el.cTranslate.addEventListener("click", () => setTranslateEnabled(!(state.settings && state.settings.translate_enabled)));
  el.xTranslate.addEventListener("click", () => setTranslateEnabled(!(state.settings && state.settings.translate_enabled)));

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
  function recentEntries() {
    // Last 3 utterances by seq.
    const seqs = [...state.utterances.keys()].sort((a, b) => a - b);
    return seqs.slice(-3).map((s) => state.utterances.get(s).record);
  }
  function isRecentSeq(seq) {
    const seqs = [...state.utterances.keys()].sort((a, b) => a - b).slice(-3);
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
    return state.sessionId;
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
      try {
        const title = el.xTitle.value.trim() || undefined;
        const capture = captureRequest();
        const r = await api("/api/capture/start", {
          method: "POST",
          body: { title, mode: capture.mode, sources: capture.sources },
        });
        state.sessionId = r && r.session_id;
        setRecording(true);
        renderStatus({ recording: true });
        notice("Recording started.", "info");
      } catch (e) {
        if (e.status === 403) { openConsent(); notice("Please acknowledge consent before recording.", "warn"); }
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
    try {
      const r = await api("/api/open-folder", { method: "POST" });
      setToolStatus(`Đã mở thư mục lưu: ${r && r.path ? r.path : ""}`);
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

  async function copyTranscript() {
    const text = buildTranscriptText();
    if (!text.trim()) { setToolStatus("Chưa có nội dung để copy.", "warn"); return; }
    const ok = await copyText(text);
    setToolStatus(ok ? "Đã copy" : "Không copy được.", ok ? "info" : "error");
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
    const opt = [...el.sumScenario.options].find((o) => o.value === value);
    return opt ? opt.textContent : value;
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

  async function runSummary() {
    const sid = activeSessionId();
    if (!sid) { notice("Start or select a session before summarizing.", "warn"); return; }
    const scenario = el.sumScenario.value || "reformat";
    const provider = el.sumProvider.value || (state.settings && state.settings.summarizer_provider) || "claude_cli";
    el.sumRun.disabled = true;
    setToolStatus("Running summary...");
    try {
      const payload = await api(`/api/sessions/${encodeURIComponent(sid)}/summarize`, {
        method: "POST",
        body: { scenario, provider },
      });
      renderSummary(payload);
      setToolStatus(payload && payload.reformat_fallback
        ? "Summary formatted deterministically to preserve exact wording."
        : "Summary ready.");
    } catch (e) {
      // Surface the backend reason (503 unavailable / 502 provider error) in the panel.
      const errText = (e.data && e.data.error) || e.message || String(e);
      renderSummary({ error: errText });
      setToolStatus("Summary failed.", "error");
      notice("Couldn't summarize session: " + errText, "error");
    } finally {
      el.sumRun.disabled = false;
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

  el.sumRun.addEventListener("click", runSummary);
  if (el.summarySave) el.summarySave.addEventListener("click", saveSummary);
  if (el.summaryCopy) el.summaryCopy.addEventListener("click", copySummary);
  if (el.copyTranscript) el.copyTranscript.addEventListener("click", copyTranscript);
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
    confirmDialog("Thoát ai-record? Phiên đang ghi sẽ được lưu lại.", "Thoát", async () => {
      const pw = window.pywebview;
      if (pw && pw.api && typeof pw.api.exit === "function") {
        try { pw.api.exit(); return; } catch (_) { /* fall through to browser path */ }
      }
      try { await api("/api/quit", { method: "POST" }); } catch (_) { /* best-effort */ }
      try { window.close(); } catch (_) { /* browsers may block */ }
    });
  }
  if (el.xExit) el.xExit.addEventListener("click", quitApp);

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

  async function runPreflight() {
    el.pfRows.textContent = "Checking…";
    let pf;
    try { pf = await api("/api/preflight"); }
    catch (e) { el.pfRows.textContent = ""; el.pfRows.appendChild(pfRow("Preflight request failed", "fail", e.message || "")); return; }

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
  }
  function presetExplain(p) {
    switch (String(p).toLowerCase()) {
      case "cpu": return "Chosen because no supported GPU was found.";
      case "cuda": case "gpu": return "A CUDA GPU was found and will be used for real-time transcription.";
      default: return "Selected automatically based on the detected hardware.";
    }
  }
  el.pfRefresh.addEventListener("click", runPreflight);
  el.pfContinue.addEventListener("click", () => { el.preflight.hidden = true; setView("compact"); });

  /* ============================ SETTINGS DRAWER ============================ */
  async function openSettings() {
    el.settings.hidden = false;
    buildSettings();
    await loadLanguages();
    buildSettings();
  }
  el.xSettings.addEventListener("click", openSettings);
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

  function syncSummaryProvider() {
    const provider = (state.settings && (state.settings.summarizer_provider || state.settings.summary_provider)) || "claude_cli";
    ensureSelectValue(el.sumProvider, provider, providerLabel(provider));
  }

  async function putSetting(patch) {
    try {
      const updated = await api("/api/settings", { method: "PUT", body: patch });
      state.settings = updated || Object.assign(state.settings || {}, patch);
      applyTheme();
      updateTranslateButtons();
      if ("translate_enabled" in patch) refreshTranslationRows();
      if ("summarizer_provider" in patch || "summary_provider" in patch) syncSummaryProvider();
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

    /* --- Translation --- */
    const gTr = group("Translation");
    gTr.appendChild(rowToggle("Translate", "show a translation line under each utterance",
      s.translate_enabled, (v) => putSetting({ translate_enabled: v })));
    gTr.appendChild(rowLanguages("Source languages", "empty means any non-target language",
      s.source_languages || [], state.languages));
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

    /* --- Output files (Feature 2) --- */
    const gOut = group("Lưu kết quả");
    gOut.appendChild(rowReadonly("Bản ghi (.md)", "transcript.md — luôn lưu", "luôn lưu"));
    gOut.appendChild(rowToggle("Kèm audio (.mp3)", "giữ audio, chuyển sang mp3 khi kết thúc",
      s.keep_audio, (v) => putSetting({ keep_audio: v, audio_export_format: "mp3" })));
    gOut.appendChild(rowToggle("Kèm .txt", "cũng lưu transcript.txt dạng văn bản thuần",
      s.save_txt, (v) => putSetting({ save_txt: v })));
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

    /* --- Summarization --- */
    const gSum = group("Summarization");
    const selectedSummaryProvider = s.summarizer_provider || s.summary_provider || "claude_cli";
    const summaryProviderOptions = SUMMARY_PROVIDERS.slice();
    if (!summaryProviderOptions.some((p) => p.value === selectedSummaryProvider)) {
      summaryProviderOptions.push({ value: selectedSummaryProvider, label: selectedSummaryProvider });
    }
    gSum.appendChild(rowSelect("Provider", "Gemini/Ollama avoid local agent tools for untrusted transcripts",
      selectedSummaryProvider,
      summaryProviderOptions,
      (v) => {
        el.sumProvider.value = v;
        putSetting({ summarizer_provider: v });
      }));
    gSum.appendChild(rowToggle("Use translations", "feed Vietnamese text to summaries when available",
      s.summary_use_translation, (v) => putSetting({ summary_use_translation: v })));
    el.setBody.appendChild(gSum);

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
    syncSummaryProvider();
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
    } catch (e) {
      notice("Couldn't reach the local service. Some features may be unavailable.", "error");
      state.settings = {};
    }

    // 2) Consent gate.
    if (!state.consentOk) openConsent();

    // 3) Preflight before the first record.
    el.preflight.hidden = false;
    runPreflight();

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
