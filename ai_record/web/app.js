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
  };

  const MAX_ROWS = 500;      // cap DOM nodes for very long transcripts

  /* ============================ DOM SHORTCUTS ============================ */
  const $ = (id) => document.getElementById(id);
  const el = {
    app: $("app"),
    // compact
    cToggle: $("c-toggle"), cDot: $("c-dot"), cStatusText: $("c-status-text"),
    cStatus: $("c-status"), cRecent: $("c-recent"), cExpand: $("c-expand"),
    // expanded
    xCollapse: $("x-collapse"), xTitle: $("x-title"), xToggle: $("x-toggle"),
    xDot: $("x-dot"), xStatusText: $("x-status-text"), xStatus: $("x-status"),
    xChips: $("x-chips"), xSearch: $("x-search"), xSettings: $("x-settings"),
    xTranscript: $("x-transcript"), xJump: $("x-jump"),
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
    if (expanded) { requestResize(900, 640); scrollToLatest(); }
    else { requestResize(520, 120); }
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
  }

  function refreshToggleEnabled() {
    const enabled = state.consentOk;
    for (const btn of [el.cToggle, el.xToggle]) {
      btn.disabled = !enabled;
      btn.title = enabled ? "" : "Acknowledge the consent notice before recording.";
    }
  }

  /* ============================ TRANSCRIPT RENDERING ============================ */
  function speakerText(rec) {
    // Unknown / low-confidence -> "?"
    if (rec.speaker && rec.speaker.trim()) {
      const low = rec.diarization_confidence != null && rec.diarization_confidence < 0.5;
      return { text: rec.speaker, cls: low ? "unknown" : "" };
    }
    // No speaker yet: diarization pending placeholder.
    return { text: "?", cls: "pending" };
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
    if (sp.cls) spk.classList.add(sp.cls);
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
    if (rec.translation_error) {
      trEl.textContent = `Translation failed: ${rec.translation_error}`;
      trEl.classList.add("error");
    } else if (rec.translation) {
      trEl.textContent = rec.translation;
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
      if (sp.cls) spk.classList.add(sp.cls);
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
    const current = rec.speaker && rec.speaker.trim() ? rec.speaker : "";
    const input = document.createElement("input");
    input.className = "spk-edit";
    input.value = current;
    input.placeholder = "Speaker name";
    spkEl.textContent = "";
    spkEl.appendChild(input);
    input.focus();
    input.select();

    const finish = (commit) => {
      const val = input.value.trim();
      spkEl.textContent = "";
      if (commit && val && val !== current) {
        renameSpeaker(current || rec.speaker, val);
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

  // Relabel all rows whose speaker matches `oldName` -> `newName`.
  function renameSpeaker(oldName, newName) {
    if (!newName) return;
    for (const { record, el: row } of state.utterances.values()) {
      if ((record.speaker || "") === (oldName || "")) {
        record.speaker = newName;
        const spk = row.querySelector(".spk");
        spk.textContent = newName;
        spk.classList.remove("pending", "unknown");
      }
    }
    renderRecent();
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
        const r = await api("/api/capture/start", { method: "POST", body: { title } });
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

  // Session title -> PUT settings? Title belongs to the session; persist on blur if recording.
  el.xTitle.addEventListener("change", () => {
    // Title is captured at start; here we just keep it local. Server owns session title.
  });

  /* ============================ EXPAND / COLLAPSE ============================ */
  el.cExpand.addEventListener("click", () => setView("expanded"));
  el.xCollapse.addEventListener("click", () => setView("compact"));

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
  function openSettings() {
    el.settings.hidden = false;
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

  async function putSetting(patch) {
    try {
      const updated = await api("/api/settings", { method: "PUT", body: patch });
      state.settings = updated || Object.assign(state.settings || {}, patch);
      applyTheme();
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
    gTr.appendChild(rowSelect("Provider", null, s.translation_provider,
      ["local", "gemini", s.translation_provider].filter(uniq),
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

    /* --- Later versions (disabled) --- */
    const gM4 = group("Post-processing");
    const summarize = mkRow("Summarize session", "available in a later version");
    const sBtn = document.createElement("button");
    sBtn.className = "btn"; sBtn.textContent = "Summarize"; sBtn.disabled = true;
    summarize.querySelector(".ctl").appendChild(sBtn);
    gM4.appendChild(summarize);
    const rediar = mkRow("Re-diarize", "available in a later version");
    const rBtn = document.createElement("button");
    rBtn.className = "btn"; rBtn.textContent = "Re-diarize"; rBtn.disabled = true;
    rediar.querySelector(".ctl").appendChild(rBtn);
    gM4.appendChild(rediar);
    el.setBody.appendChild(gM4);

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

  function handleWsMessage(msg) {
    switch (msg.type) {
      case "utterance":
        if (msg.record) addUtterance(msg.record);
        break;
      case "patch":
        if (msg.seq != null) {
          if (msg.seq > state.lastSeq) state.lastSeq = msg.seq;
          patchUtterance(msg.seq, msg.fields || {});
        }
        break;
      case "status":
        if (typeof msg.recording === "boolean") setRecording(msg.recording);
        renderStatus(msg);
        break;
      case "rename":
        renameSpeaker(msg.old, msg.new);
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
      const rows = await api(`/api/sessions/${state.sessionId}/utterances?since_seq=${sinceSeq}`);
      if (Array.isArray(rows)) for (const rec of rows) addUtterance(rec);
    } catch (e) { /* best-effort catch-up */ }
  }

  /* ============================ BOOT ============================ */
  async function refreshSettings() {
    state.settings = await api("/api/settings");
    state.consentOk = !!(state.settings && state.settings.consent_acknowledged);
    applyTheme();
    refreshToggleEnabled();
    return state.settings;
  }

  async function boot() {
    if (!TOKEN) { el.noToken.hidden = false; return; }
    el.app.hidden = false;

    // 1) Settings drive consent + theme.
    try {
      await refreshSettings();
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
