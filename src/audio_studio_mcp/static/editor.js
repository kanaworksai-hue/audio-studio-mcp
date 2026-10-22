// Edit page: waveform with zoom, playhead and a drag-selected range, then one
// row of tasks: three AI edits (edit_audio: variation / inpaint / continue) and
// Export (process_audio). The waveform labels what the chosen task will touch. Mouse: click = move the playhead (outside the selection it
// also clears it), drag = select, Shift+click = move the nearest edge,
// double-click = select all, Ctrl/⌘+wheel = zoom, horizontal wheel = scroll.
"use strict";

const OVERVIEW_N = 4000;
const MIN_SPAN = 0.05;     // seconds visible at the deepest zoom
const DRAG_PX = 4;         // movement that turns a click into a selection
const MIN_SEL = 0.01;      // shortest selection (seconds)

// Export presets: what the user is making → process_audio settings.
const PRESETS = {
  game_sfx: { trim_silence: true, mono: true, loop: false, normalize: "peak", target_lufs: -16, peak_ceiling_db: -1,
    loop_crossfade_ms: 200, fade_in_ms: 0, fade_out_ms: 0, format: "ogg", sample_rate: "48000" },
  game_loop: { trim_silence: false, mono: false, loop: true, normalize: "loudness", target_lufs: -16, peak_ceiling_db: -1,
    loop_crossfade_ms: 200, fade_in_ms: 0, fade_out_ms: 0, format: "ogg", sample_rate: "48000" },
  video_bgm: { trim_silence: false, mono: false, loop: false, normalize: "loudness", target_lufs: -14, peak_ceiling_db: -1,
    loop_crossfade_ms: 200, fade_in_ms: 500, fade_out_ms: 2000, format: "wav", sample_rate: "48000" },
  original: { trim_silence: false, mono: false, loop: false, normalize: "none", target_lufs: -16, peak_ceiling_db: -1,
    loop_crossfade_ms: 200, fade_in_ms: 0, fade_out_ms: 0, format: "wav", sample_rate: "44100" },
};
const PRESET_ORDER = ["game_sfx", "game_loop", "video_bgm", "original", "custom"];

const Editor = {
  file: null, item: null, seconds: 0, sampleRate: 0, channels: 0,
  view: { s: 0, e: 1 }, sel: null, playhead: 0, playSel: false,
  overview: null, detail: null, detailTimer: null, drag: null,
  audio: new Audio(), visible: false, openSeq: 0, task: "ai",

  // ── setup ──────────────────────────────────────────────────────────────
  init() {
    const a = this.audio;
    a.preload = "auto";
    a.addEventListener("play", () => { window.dispatchEvent(new CustomEvent("as:play", { detail: "editor" })); this.tick(); this.renderTransport(); });
    a.addEventListener("pause", () => this.renderTransport());
    a.addEventListener("ended", () => { this.playhead = this.seconds; this.draw(); this.renderTransport(); });
    window.addEventListener("as:play", (e) => { if (e.detail !== "editor") a.pause(); });

    const wave = $("#w-wave");
    wave.addEventListener("pointerdown", (e) => this.pointerDown(e));
    wave.addEventListener("pointermove", (e) => this.pointerMove(e));
    wave.addEventListener("pointerup", (e) => this.pointerUp(e));
    wave.addEventListener("pointercancel", () => { this.drag = null; });
    wave.addEventListener("dblclick", () => this.selectAll());
    wave.addEventListener("wheel", (e) => this.wheel(e), { passive: false });
    wave.addEventListener("keydown", (e) => this.waveKey(e));
    for (const [id, edge] of [["#w-h-start", "a"], ["#w-h-end", "b"]]) {
      $(id).addEventListener("pointerdown", (e) => { e.stopPropagation(); this.startEdgeDrag(e, edge); });
      $(id).addEventListener("keydown", (e) => this.handleKey(e, edge));
    }
    const ov = $("#w-overview");
    ov.addEventListener("pointerdown", (e) => { ov.setPointerCapture(e.pointerId); this.overviewSeek(e); });
    ov.addEventListener("pointermove", (e) => { if (e.buttons & 1) this.overviewSeek(e); });

    $("#w-play").addEventListener("click", () => this.togglePlay());
    $("#w-loop").addEventListener("change", (e) => { this.audio.loop = e.target.checked && !this.sel; });
    $("#w-zoom-in").addEventListener("click", () => this.zoom(0.5));
    $("#w-zoom-out").addEventListener("click", () => this.zoom(2));
    $("#w-zoom-fit").addEventListener("click", () => this.setView(0, this.seconds));
    $("#w-zoom-sel").addEventListener("click", () => this.zoomToSelection());
    $("#w-sel-all").addEventListener("click", () => this.selectAll());
    $("#w-sel-clear").addEventListener("click", () => this.setSel(null));
    for (const id of ["#w-start", "#w-end"]) $(id).addEventListener("change", () => this.selFromInputs());
    $("#edit-to-library").addEventListener("click", () => showPage("library"));
    document.addEventListener("keydown", (e) => this.globalKey(e));
    new ResizeObserver(() => { if (this.visible) { this.scheduleDetail(); this.draw(); } }).observe($("#w-wave"));
    window.addEventListener("as:theme", () => this.draw());
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.draw());

    // task cards: the three AI modes (radios of #ai-form) and Export
    $("#mode-cards").addEventListener("change", () => this.setTask("ai"));
    for (const lab of document.querySelectorAll("#mode-cards label.mode-card")) lab.addEventListener("click", () => this.setTask("ai"));
    $("#st-export").addEventListener("click", () => this.setTask("export"));
    $("#mode-cards").addEventListener("keydown", (e) => {  // the radios sit outside #ai-form
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && this.task === "ai") { e.preventDefault(); $("#ai-form").requestSubmit(); }
    });
    this.initAiForm();
    this.initExportForm();
  },
  setTask(task) {
    this.task = task;
    $("#ai-form").hidden = task !== "ai";
    $("#export-form").hidden = task !== "export";
    $("#st-export").setAttribute("aria-pressed", String(task === "export"));
    this.updateAiForm();
    this.updateExportForm();
  },
  applyI18n() {
    this.renderTransport();
    this.renderSelection();
    this.renderInfo();
    this.updateAiForm();
    this.renderPresets();
    this.updateExportForm();
    if (!this.file) this.renderRecent();
  },

  // ── open a file ────────────────────────────────────────────────────────
  async open(file) {
    if (file === this.file && this.overview) return;
    const seq = ++this.openSeq;
    this.audio.pause();
    this.file = file;
    this.item = null; this.overview = null; this.detail = null; this.sel = null; this.playhead = 0;
    this.seconds = 0;
    $("#edit-empty").hidden = true;
    $("#edit-main").hidden = false;
    $("#w-loading").hidden = false;
    $("#w-loading").textContent = t("wave.loading");
    this.renderInfo();
    let item, peaks;
    try {
      item = await api(`/api/library/${encodeURIComponent(file)}`);  // 404 if it is not in the output folder
      if (seq !== this.openSeq) return;
      this.audio.src = audioUrl(file);
      peaks = await Peaks.get(file, OVERVIEW_N, null, null, Math.round(item.mtime));
    } catch (e) {
      if (seq !== this.openSeq) return;
      this.file = null;
      this.audio.removeAttribute("src");
      toast(e.status === 404 ? t("edit.notFound", { file }) : e.message);
      this.showEmpty();
      if (location.hash.startsWith("#edit/")) history.replaceState(null, "", "#edit");
      return;
    }
    if (seq !== this.openSeq) return;
    this.item = item; this.overview = peaks;
    this.seconds = peaks.seconds; this.sampleRate = peaks.sample_rate; this.channels = peaks.channels;
    this.view = { s: 0, e: this.seconds || 1 };
    $("#w-loading").hidden = true;
    $("#w-start").max = $("#w-end").max = String(this.seconds);
    this.prefill();
    this.renderInfo();
    this.renderSelection();
    this.renderTransport();
    this.draw();
    renderJobs();
  },
  showEmpty() {
    $("#edit-main").hidden = true;
    $("#edit-empty").hidden = false;
    this.renderRecent();
  },
  shown() {
    this.visible = true;
    if (!this.file) { this.showEmpty(); return; }
    requestAnimationFrame(() => this.draw());
  },
  hidden() {
    this.visible = false;
    this.audio.pause();
  },
  async renderRecent() {
    const box = $("#edit-recent");
    let items = [];
    try { items = (await api("/api/library?limit=6")).items; } catch { /* offline */ }
    box.replaceChildren(...items.map((it) => el("button", { type: "button", class: "recent-item", onclick: () => openEditor(it.file) },
      el("span", { class: "recent-name", text: (it.meta && it.meta.title) || it.file }),
      el("span", { class: "muted small", text: `${fmtLen(it.seconds)} · ${fmtDate(it.mtime)}` }))));
    if (items.length) box.prepend(el("p", { class: "small muted recent-head", text: t("edit.recent") }));
  },
  // An input path can be opened here only if it is a file of the output folder.
  inOutputDir(path) {
    if (!path || !state.status) return false;
    const name = baseName(path), dir = String(path).slice(0, -name.length - 1);
    return dir === state.status.output_dir;
  },
  renderInfo() {
    if (!this.file) return;
    const m = (this.item && this.item.meta) || {};
    $("#edit-name").textContent = m.title || this.file;
    $("#edit-name").title = this.file;
    const parts = [];
    if (this.seconds) parts.push(this.fmt(this.seconds));
    if (this.sampleRate) parts.push(`${(this.sampleRate / 1000).toFixed(1).replace(/\.0$/, "")} kHz`, this.channels === 1 ? t("edit.mono") : t("edit.stereo"));
    if (m.audio_type) parts.push(t(`type.${m.audio_type}`));
    if (m.kind) parts.push(kindLabel(m));
    $("#edit-info").textContent = parts.join(" · ");
    const src = $("#edit-source");
    src.replaceChildren();
    if (this.inOutputDir(m.input_audio)) {
      const name = baseName(m.input_audio);
      src.append(el("span", { class: "muted", text: t("edit.madeFrom") }), " ",
        el("button", { type: "button", class: "link-btn", text: name, onclick: () => openEditor(name) }));
    }
  },

  // ── geometry and drawing ─────────────────────────────────────────────────
  width() { return $("#w-wave").clientWidth || 1; },
  t2x(t) { return ((t - this.view.s) / (this.view.e - this.view.s)) * this.width(); },
  x2t(x) { return this.view.s + (x / this.width()) * (this.view.e - this.view.s); },
  clampT(t) { return Math.max(0, Math.min(this.seconds, t)); },
  eventT(e) { return this.clampT(this.x2t(e.clientX - $("#w-wave").getBoundingClientRect().left)); },
  setView(s, e) {
    if (!this.seconds) return;
    let span = Math.max(MIN_SPAN, Math.min(this.seconds, e - s));
    s = Math.max(0, Math.min(this.seconds - span, s));
    this.view = { s, e: s + span };
    this.scheduleDetail();
    this.draw();
  },
  zoom(factor, center) {
    const { s, e } = this.view, span = e - s;
    if (center == null) center = this.playhead >= s && this.playhead <= e ? this.playhead : (s + e) / 2;
    const ns = span * factor, k = (center - s) / span;
    this.setView(center - k * ns, center - k * ns + ns);
  },
  zoomToSelection() {
    if (!this.sel) return;
    const pad = (this.sel.b - this.sel.a) * 0.05;
    this.setView(this.sel.a - pad, this.sel.b + pad);
  },
  // Fetch finer peaks when the overview has fewer buckets than pixels in view.
  scheduleDetail() {
    clearTimeout(this.detailTimer);
    if (!this.overview || !this.seconds) return;
    const px = this.width() * (window.devicePixelRatio || 1);
    const span = this.view.e - this.view.s;
    if (OVERVIEW_N * (span / this.seconds) >= px) return;
    const d = this.detail;
    if (d && d.start <= this.view.s && d.end >= this.view.e && d.min.length / (d.end - d.start) * span >= px) return;
    this.detailTimer = setTimeout(async () => {
      const s = Math.max(0, this.view.s - span / 2), e = Math.min(this.seconds, this.view.e + span / 2);
      const n = Math.min(20000, Math.ceil(px * ((e - s) / span)));
      const file = this.file;
      try {
        const p = await Peaks.get(file, n, s, e, this.item && Math.round(this.item.mtime));
        if (file === this.file) { this.detail = p; this.draw(); }
      } catch { /* keep the overview */ }
    }, 120);
  },
  draw() {
    if (!this.visible || !this.overview) return;
    const cv = $("#w-canvas"), ctx = fitCanvas(cv), W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = cssVar("--wave-axis");
    ctx.fillRect(0, Math.floor(H / 2), W, 1);
    const { s, e } = this.view, span = e - s;
    const d = this.detail && this.detail.start <= s + 1e-6 && this.detail.end >= e - 1e-6 ? this.detail : this.overview;
    const dStart = d === this.overview ? 0 : d.start, dEnd = d === this.overview ? this.seconds : d.end;
    const n = d.min.length;
    // slice the buckets in view, then draw them across the canvas
    const i0 = Math.max(0, Math.floor(((s - dStart) / (dEnd - dStart)) * n));
    const i1 = Math.min(n, Math.ceil(((e - dStart) / (dEnd - dStart)) * n));
    const t0 = dStart + (i0 / n) * (dEnd - dStart), t1 = dStart + (i1 / n) * (dEnd - dStart);
    drawPeaks(ctx, d.min.slice(i0, i1), d.max.slice(i0, i1), {
      color: cssVar("--wave"), played: cssVar("--wave-played"),
      split: (this.playhead - t0) / Math.max(1e-9, t1 - t0),
      x0: ((t0 - s) / span) * W, x1: ((t1 - s) / span) * W, gain: 0.95 });
    this.drawRuler();
    this.drawOverview();
    this.renderOverlay();
  },
  drawRuler() {
    const cv = $("#w-ruler"), ctx = fitCanvas(cv), W = cv.width, H = cv.height, dpr = window.devicePixelRatio || 1;
    ctx.clearRect(0, 0, W, H);
    const { s, e } = this.view, span = e - s;
    const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
    const minPx = 80 * dpr;
    const step = steps.find((st) => (st / span) * W >= minPx) || 600;
    const decimals = step < 0.1 ? 2 : step < 1 ? 1 : 0;
    ctx.fillStyle = cssVar("--muted");
    ctx.strokeStyle = cssVar("--border-strong");
    ctx.font = `${11 * dpr}px "IBM Plex Sans", system-ui, sans-serif`;
    ctx.textBaseline = "top";
    for (let tk = Math.ceil(s / step) * step; tk <= e + 1e-9; tk += step) {
      const x = Math.round(((tk - s) / span) * W) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, H - 6 * dpr); ctx.lineTo(x, H); ctx.stroke();
      ctx.fillText(this.seconds >= 60 ? fmtClock(tk, decimals) : tk.toFixed(decimals), x + 3 * dpr, 2 * dpr);
    }
  },
  drawOverview() {
    const box = $("#w-overview"), zoomed = this.view.e - this.view.s < this.seconds * 0.999;
    box.hidden = !zoomed;
    if (!zoomed) return;
    const cv = $("#w-ov-canvas"), ctx = fitCanvas(cv);
    ctx.clearRect(0, 0, cv.width, cv.height);
    drawPeaks(ctx, this.overview.min, this.overview.max, { color: cssVar("--wave"), gain: 0.9 });
    const vb = $("#w-view");
    vb.style.left = `${(this.view.s / this.seconds) * 100}%`;
    vb.style.width = `${Math.max(0.5, ((this.view.e - this.view.s) / this.seconds) * 100)}%`;
  },
  overviewSeek(e) {
    const r = $("#w-overview").getBoundingClientRect();
    const c = ((e.clientX - r.left) / r.width) * this.seconds, span = this.view.e - this.view.s;
    this.setView(c - span / 2, c + span / 2);
  },
  renderOverlay() {
    const W = this.width(), ph = $("#w-playhead"), x = this.t2x(this.playhead);
    ph.hidden = x < -1 || x > W + 1;
    ph.style.left = `${x}px`;
    const wave = $("#w-wave");
    wave.setAttribute("aria-valuemax", String(Math.round(this.seconds * 100) / 100));
    wave.setAttribute("aria-valuenow", String(Math.round(this.playhead * 100) / 100));
    wave.setAttribute("aria-valuetext", `${fmtClock(this.playhead, 1)} / ${fmtClock(this.seconds, 1)}`);
    this.renderHints();
    const box = $("#w-sel");
    if (!this.sel) { box.hidden = true; return; }
    const a = this.t2x(this.sel.a), b = this.t2x(this.sel.b);
    box.hidden = b < 0 || a > W;
    box.style.left = `${a}px`;
    box.style.width = `${Math.max(1, b - a)}px`;
    for (const [id, v] of [["#w-h-start", this.sel.a], ["#w-h-end", this.sel.b]]) {
      const h = $(id);
      h.setAttribute("aria-valuemin", "0");
      h.setAttribute("aria-valuemax", String(Math.round(this.seconds * 100) / 100));
      h.setAttribute("aria-valuenow", String(Math.round(v * 100) / 100));
      h.setAttribute("aria-valuetext", fmtClock(v, 2));
    }
  },

  // What the chosen task will touch, said on the waveform itself.
  renderHints() {
    const mode = this.task === "export" ? "export" : this.aiMode();
    const trim = this.task === "export" && this.xField("trim_selection").checked && !!this.sel;
    const wave = $("#w-wave");
    wave.dataset.task = mode;
    wave.classList.toggle("sel-used", mode === "inpaint" || trim);
    const label = $("#w-sel-label");
    label.hidden = !(mode === "inpaint" || trim);
    label.textContent = mode === "inpaint" ? t("wave.region.inpaint") : trim ? t("wave.region.export") : "";
    const hint = $("#w-hint");
    let text = "";
    if (this.seconds && mode === "inpaint" && !this.sel) text = t("wave.dragHint");
    else if (this.seconds && this.sel && (mode === "variation" || mode === "continue" || (mode === "export" && !trim))) text = t("wave.wholeNote");
    hint.hidden = !text;
    hint.textContent = text;
    hint.classList.toggle("center", mode === "inpaint");
    // extend: a strip at the end of the sound says how much is added
    const ghost = $("#w-ghost"), target = parseFloat(this.aiField("duration_seconds").value);
    const add = target - this.seconds, atEnd = this.view.e >= this.seconds - 1e-6;
    ghost.hidden = !(mode === "continue" && add > 0.01 && atEnd);
    ghost.textContent = ghost.hidden ? "" : t("wave.ghost", { len: `${Math.round(add * 10) / 10} s` });
  },

  // ── selection ──────────────────────────────────────────────────────────
  // keep: an edge is being moved, so hold a minimal width instead of dropping the selection
  setSel(sel, { quiet, keep } = {}) {
    const had = !!this.sel;
    if (sel) {
      let a = this.clampT(Math.min(sel.a, sel.b)), b = this.clampT(Math.max(sel.a, sel.b));
      if (b - a < MIN_SEL && keep && this.seconds >= MIN_SEL) {
        if (a + MIN_SEL <= this.seconds) b = a + MIN_SEL; else a = b - MIN_SEL;
      }
      sel = b - a >= MIN_SEL ? { a, b } : null;
    }
    this.sel = sel;
    this.audio.loop = $("#w-loop").checked && !sel;
    if (!!sel !== had) $("#x-trim-sel").checked = !!sel;  // a new selection is usually what to export
    this.renderSelection(quiet);
    this.draw();
    this.updateAiForm();
    this.updateExportForm();
  },
  selectAll() { if (this.seconds) this.setSel({ a: 0, b: this.seconds }); },
  selFromInputs() {
    const a = parseFloat($("#w-start").value), b = parseFloat($("#w-end").value);
    const bad = !(a >= 0 && b > a && b <= this.seconds + 1e-6);
    $("#w-start").toggleAttribute("aria-invalid", bad && !(a >= 0));
    $("#w-end").toggleAttribute("aria-invalid", bad);
    if (!bad) this.setSel({ a, b }, { quiet: true });
  },
  renderSelection(quiet) {
    const s = this.sel;
    if (!quiet || !s) {
      $("#w-start").value = s ? s.a.toFixed(2) : "";
      $("#w-end").value = s ? s.b.toFixed(2) : "";
      $("#w-start").removeAttribute("aria-invalid"); $("#w-end").removeAttribute("aria-invalid");
    }
    $("#w-start").placeholder = "0.00";
    $("#w-end").placeholder = this.seconds ? this.seconds.toFixed(2) : "";
    $("#w-len").textContent = s ? t("wave.length", { len: this.fmt(s.b - s.a) }) : t("wave.noSelection");
    $("#w-sel-clear").disabled = !s;
    $("#w-zoom-sel").disabled = !s;
  },

  // ── pointer / keyboard ─────────────────────────────────────────────────
  pointerDown(e) {
    if (e.button !== 0 || !this.seconds) return;
    const wave = $("#w-wave");
    wave.setPointerCapture(e.pointerId);
    wave.focus({ preventScroll: true });
    const tm = this.eventT(e);
    if (e.shiftKey && this.sel) {
      const edge = Math.abs(tm - this.sel.a) < Math.abs(tm - this.sel.b) ? "a" : "b";
      this.drag = { mode: "edge", edge };
      this.setSel({ ...this.sel, [edge]: tm }, { keep: true });
    } else {
      this.drag = { mode: "pending", x0: e.clientX, anchor: tm };
    }
  },
  startEdgeDrag(e, edge) {
    if (e.button !== 0) return;
    $("#w-wave").setPointerCapture(e.pointerId);
    this.drag = { mode: "edge", edge };
  },
  pointerMove(e) {
    const d = this.drag;
    if (!d) return;
    const tm = this.eventT(e);
    if (d.mode === "pending" && Math.abs(e.clientX - d.x0) > DRAG_PX) d.mode = "select";
    if (d.mode === "select") this.setSel({ a: d.anchor, b: tm });
    else if (d.mode === "edge" && this.sel) {
      const other = d.edge === "a" ? this.sel.b : this.sel.a;
      if ((d.edge === "a" && tm > other) || (d.edge === "b" && tm < other)) d.edge = d.edge === "a" ? "b" : "a";
      this.setSel(d.edge === "a" ? { a: tm, b: other } : { a: other, b: tm }, { keep: true });
    }
  },
  pointerUp(e) {
    const d = this.drag;
    this.drag = null;
    if (!d || d.mode !== "pending") return;
    const tm = this.eventT(e);
    if (this.sel && (tm < this.sel.a || tm > this.sel.b)) this.setSel(null);
    this.seek(tm);
  },
  wheel(e) {
    if (!this.seconds) return;
    const span = this.view.e - this.view.s;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      this.zoom(Math.exp(Math.max(-1, Math.min(1, e.deltaY * 0.01))), this.eventT(e));
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
      if (span >= this.seconds) return;
      e.preventDefault();
      const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      const dt = (dx / this.width()) * span;
      this.setView(this.view.s + dt, this.view.e + dt);
    }
  },
  waveKey(e) {
    const step = e.shiftKey ? 5 : 1;
    if (e.key === "ArrowRight") this.seek(this.playhead + step);
    else if (e.key === "ArrowLeft") this.seek(this.playhead - step);
    else if (e.key === "Home") this.seek(this.sel ? this.sel.a : 0);
    else if (e.key === "End") this.seek(this.sel ? this.sel.b : this.seconds);
    else return;
    e.preventDefault();
  },
  handleKey(e, edge) {
    if (!this.sel) return;
    const step = e.shiftKey ? 0.1 : 0.01, { a, b } = this.sel;
    let v;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") v = this.sel[edge] + step;
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") v = this.sel[edge] - step;
    else if (e.key === "Home") v = edge === "a" ? 0 : a + MIN_SEL;
    else if (e.key === "End") v = edge === "b" ? this.seconds : b - MIN_SEL;
    else return;
    e.preventDefault();
    e.stopPropagation();
    // an edge stops at the other one (the selection never disappears from a key press)
    v = edge === "a" ? Math.min(v, b - MIN_SEL) : Math.max(v, a + MIN_SEL);
    this.setSel(edge === "a" ? { a: v, b } : { a, b: v }, { keep: true });
  },
  globalKey(e) {
    if (!this.visible || !this.file || typing(e) || e.altKey || e.defaultPrevented) return;
    if (e.target.closest && e.target.closest("#edit-jobs")) return;  // keys there compare results (abKeys)
    if (e.target.closest && e.target.closest("button, a, summary, [role=menuitem]") && (e.key === " " || e.key === "Enter")) return;
    const k = e.key;
    if ((e.metaKey || e.ctrlKey) && k.toLowerCase() === "a") { e.preventDefault(); this.selectAll(); return; }
    if (e.metaKey || e.ctrlKey) {
      if (k === "0") { e.preventDefault(); this.setView(0, this.seconds); }
      return;
    }
    if (k === " ") { e.preventDefault(); this.togglePlay(); }
    else if (k === "l" || k === "L") { $("#w-loop").click(); }
    else if (k === "Escape") { if (!document.querySelector("details.menu[open]")) this.setSel(null); }
    else if (k === "+" || k === "=") this.zoom(0.5);
    else if (k === "-" || k === "_") this.zoom(2);
    else if (k === "0") this.setView(0, this.seconds);
    else if (k === "z" || k === "Z") this.zoomToSelection();
  },

  // ── playback ───────────────────────────────────────────────────────────
  seek(tm) {
    this.playhead = this.clampT(tm);
    this.playSel = !!this.sel && this.playhead >= this.sel.a && this.playhead < this.sel.b;
    if (this.audio.readyState >= 1) this.audio.currentTime = this.playhead;
    this.followPlayhead();
    this.draw();
    this.renderTransport();
  },
  async togglePlay() {
    const a = this.audio;
    if (!a.paused) { a.pause(); return; }
    let start = this.playhead;
    if (this.sel && (start < this.sel.a || start >= this.sel.b - 0.01)) start = this.sel.a;
    else if (!this.sel && start >= this.seconds - 0.01) start = 0;
    this.playSel = !!this.sel;
    const file = this.file;
    if (!(await whenReady(a)) || file !== this.file) return;
    a.currentTime = start;
    this.playhead = start;
    try { await a.play(); } catch { /* interrupted */ }
  },
  tick() {
    const a = this.audio;
    if (a.paused) { this.draw(); return; }
    let tm = a.currentTime;
    if (this.playSel && this.sel && tm >= this.sel.b) {
      if ($("#w-loop").checked) { a.currentTime = tm = this.sel.a; }
      else { a.pause(); a.currentTime = tm = this.sel.a; }
    }
    this.playhead = tm;
    this.followPlayhead();
    this.draw();
    this.renderClock();
    requestAnimationFrame(() => this.tick());
  },
  // Page the view along while playing (or after a seek) so the playhead stays visible.
  followPlayhead() {
    const { s, e } = this.view, span = e - s;
    if (span >= this.seconds) return;
    if (this.playhead > e || this.playhead < s) this.setView(this.playhead - span * 0.1, this.playhead + span * 0.9);
  },
  renderClock() {
    $("#w-clock").textContent = `${this.fmt(this.playhead, undefined, false)} / ${this.fmt(this.seconds)}`;
  },
  renderTransport() {
    const playing = !this.audio.paused;
    $("#w-play").classList.toggle("playing", playing);
    $("#w-play-text").textContent = playing ? t("player.pause") : this.sel ? t("wave.playSel") : t("player.play");
    this.renderClock();
  },

  // ── Change with AI (edit_audio) ──────────────────────────────────────────
  aiField(name) { return $("#ai-form").elements.namedItem(name); },
  aiMode() { return this.aiField("mode").value; },
  initAiForm() {
    const f = $("#ai-form");
    f.addEventListener("change", () => this.updateAiForm());
    f.addEventListener("input", (e) => { if (e.target.removeAttribute) e.target.removeAttribute("aria-invalid"); this.updateAiForm(); });
    f.addEventListener("submit", (e) => { e.preventDefault(); this.submitAi(); });
    f.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); f.requestSubmit(); } });
  },
  max() { return state.status ? state.status.max_seconds : 380; },
  extendDefault() {
    const s = this.seconds;
    return Math.min(this.max(), Math.ceil(s + Math.min(30, Math.max(5, s))));
  },
  prefill() {
    const m = (this.item && this.item.meta) || {};
    const src = m.source || {};
    const type = TYPES.includes(m.audio_type) ? m.audio_type : TYPES.includes(src.audio_type) ? src.audio_type : "music";
    this.aiField("audio_type").value = type;
    const prompt = m.prompt || promptLabel({ prompt: "", ...prefixSound(m.sound_fields) }) || src.prompt || "";
    this.aiField("prompt").value = prompt;
    this.aiField("duration_seconds").value = this.extendDefault();
    for (const k of ["title", "seed", "cfg", "steps", "negative_prompt"]) this.aiField(k).value = "";
    clearErrors($("#ai-form"), $("#ai-error"));
    // export: a preset that suits the kind of sound
    this.setPreset(type === "sfx" ? "game_sfx" : type === "instrument" ? "game_loop" : "video_bgm");
    $("#export-form").elements.namedItem("title").value = "";
    clearErrors($("#export-form"), $("#x-error"));
    this.updateAiForm();
  },
  // Times as the selection fields show them: seconds under a minute, m:ss.d above.
  fmt(sec, d = this.seconds < 10 ? 2 : 1, unit = true) {
    if (this.seconds >= 60) return fmtClock(sec, d);
    return `${sec.toFixed(d)}${unit ? " s" : ""}`;
  },
  selText() { return this.sel ? `${this.fmt(this.sel.a, undefined, false)}–${this.fmt(this.sel.b)}` : ""; },
  outLen() {
    const trim = this.xField("trim_selection").checked && this.sel;
    let len = trim ? this.sel.b - this.sel.a : this.seconds;
    if (this.xField("loop").checked) len -= (Number(this.xField("loop_crossfade_ms").value) || 200) / 1000;
    return Math.max(0, len);
  },
  // Fades never take more than a quarter of the output each (a 2 s fade on a 0.4 s clip silences it).
  fades() {
    const cap = (this.outLen() * 1000) / 4;
    const fi = Number(this.xField("fade_in_ms").value) || 0, fo = Number(this.xField("fade_out_ms").value) || 0;
    return { fi: Math.round(Math.min(fi, cap)), fo: Math.round(Math.min(fo, cap)), clamped: fi > cap || fo > cap };
  },
  updateAiForm() {
    if (!$("#ai-form")) return;
    const mode = this.aiMode(), n = parseInt(this.aiField("variations").value, 10) || 1;
    $("#strength-field").hidden = mode !== "variation";
    $("#inpaint-note").hidden = mode !== "inpaint";
    $("#extend-field").hidden = mode !== "continue";
    $("#strength-out").textContent = Number(this.aiField("strength").value).toFixed(2);
    for (const c of document.querySelectorAll("#mode-cards label.mode-card")) c.classList.toggle("checked", this.task === "ai" && c.querySelector("input").checked);
    $("#st-export").classList.toggle("checked", this.task === "export");
    $("#tool-title").textContent = this.task === "export" ? t("task.export") : t(`mode.${mode}`);
    if (this.overview) this.renderHints();
    const note = $("#inpaint-note");
    note.classList.toggle("warn", !this.sel);
    note.textContent = this.sel ? t("edit.inpaintSel", { range: this.selText() }) : t("edit.inpaintNoSel");
    $("#ai-prompt-hint").textContent = t(`edit.promptHint.${mode}`);
    // extend
    const dur = this.aiField("duration_seconds");
    dur.min = String(Math.floor(this.seconds) + 1);
    dur.max = String(this.max());
    const target = parseFloat(dur.value);
    $("#extend-hint").textContent = t("edit.extendHint", { now: this.fmt(this.seconds), max: this.max() });
    const adds = [5, 10, 30, 60].filter((d) => this.seconds + d <= this.max());
    $("#extend-chips").replaceChildren(...adds.map((d) => el("button", { type: "button", class: "chip",
      "aria-pressed": String(Math.abs(target - Math.ceil(this.seconds + d)) < 0.01), text: `+${d} s`,
      onclick: () => { dur.value = Math.ceil(this.seconds + d); this.updateAiForm(); } })));
    // submit label states what will happen
    const atMax = this.seconds >= this.max() - 0.5;  // nothing left to extend
    if (mode === "continue" && atMax) $("#extend-hint").textContent = t("edit.atMax", { max: this.max() });
    const btn = $("#ai-submit");
    btn.disabled = !this.file || (mode === "inpaint" && !this.sel) || (mode === "continue" && atMax);
    if (mode === "variation") btn.textContent = n > 1 ? t("edit.submitVariationN", { n }) : t("edit.submitVariation");
    else if (mode === "inpaint") btn.textContent = this.sel ? t("edit.submitInpaint", { range: this.selText() }) : t("mode.inpaint");
    else btn.textContent = t("edit.submitContinue", { len: target > 0 ? `${target} s` : "—" });
    if (n > 1 && mode !== "variation") btn.textContent += t("edit.nVersions", { n });
    // AI edits of an export (mono, resampled, normalized) are better done on its original
    const m = (this.item && this.item.meta) || {};
    const exp = $("#export-note");
    exp.hidden = !(m.kind === "process" && this.inOutputDir(m.input_audio));
    if (!exp.hidden) {
      const orig = baseName(m.input_audio);
      exp.replaceChildren(el("span", { text: t("edit.isExport") }), " ",
        el("button", { type: "button", class: "link-btn", text: t("edit.openOriginal"), onclick: () => openEditor(orig) }));
    }
  },
  async submitAi() {
    const f = $("#ai-form"), mode = this.aiMode();
    clearErrors(f, $("#ai-error"));
    if (mode === "inpaint" && !this.sel) return;
    const args = { input_audio: this.file, mode, audio_type: this.aiField("audio_type").value,
      prompt: this.aiField("prompt").value.trim(), variations: parseInt(this.aiField("variations").value, 10) };
    let expect = this.seconds;
    if (mode === "variation") args.strength = Number(this.aiField("strength").value);
    if (mode === "inpaint") { args.start_seconds = +this.sel.a.toFixed(3); args.end_seconds = +this.sel.b.toFixed(3); }
    if (mode === "continue") {
      const raw = this.aiField("duration_seconds").value.trim();
      if (raw !== "") args.duration_seconds = Number(raw);
      expect = args.duration_seconds || expect;
    }
    for (const k of ["seed", "cfg", "steps"]) {
      const raw = this.aiField(k).value.trim();
      if (raw !== "") args[k] = Number(raw);
    }
    for (const k of ["title", "negative_prompt"]) {
      const v = this.aiField(k).value.trim();
      if (v) args[k] = v;
    }
    const btn = $("#ai-submit");
    btn.disabled = true;
    const job = await queueJob("edit_audio", args, { expectSeconds: expect * args.variations,
      onError: (e) => showFormError(e.message, e.body && e.body.fields, f, $("#ai-error"), $("#ai-advanced")) });
    this.updateAiForm();
    if (job) toast(t("toast.editQueued"));
  },

  // ── Export (process_audio) ───────────────────────────────────────────────
  xField(name) { return $("#export-form").elements.namedItem(name); },
  initExportForm() {
    const f = $("#export-form");
    f.addEventListener("change", (e) => {
      if (e.target.name === "preset") this.setPreset(e.target.value);
      else if (e.target.name !== "trim_selection" && e.target.name !== "title") this.markCustom();
      this.updateExportForm();
    });
    f.addEventListener("input", (e) => {
      if (e.target.removeAttribute) e.target.removeAttribute("aria-invalid");
      if (e.target.type === "number") this.markCustom();
      this.updateExportForm();
    });
    f.addEventListener("submit", (e) => { e.preventDefault(); this.submitExport(); });
    this.renderPresets();
  },
  renderPresets() {
    const box = $("#presets");
    if (!box) return;
    const cur = this.preset || "original";
    box.replaceChildren(...PRESET_ORDER.map((p) => el("label", { class: `preset${p === cur ? " checked" : ""}` },
      el("input", { type: "radio", name: "preset", value: p, checked: p === cur }),
      el("span", { class: "preset-name", text: t(`preset.${p}`) }))));
    $("#preset-desc").textContent = t(`preset.${cur}Desc`);
  },
  setPreset(p) {
    this.preset = p;
    let v = PRESETS[p];
    // "keep as is" keeps the file's own rate when it is one we can write
    if (p === "original" && [44100, 48000].includes(this.sampleRate)) v = { ...v, sample_rate: String(this.sampleRate) };
    if (v) {
      for (const [k, val] of Object.entries(v)) {
        const fld = this.xField(k);
        if (!fld) continue;
        if (fld instanceof RadioNodeList) fld.value = String(val);
        else if (fld.type === "checkbox") fld.checked = val;
        else fld.value = String(val);
      }
    }
    if (p === "custom") $("#x-custom").open = true;
    this.renderPresets();
    this.updateExportForm();
  },
  markCustom() {
    if (this.preset !== "custom") { this.preset = "custom"; this.renderPresets(); }
  },
  updateExportForm() {
    if (!$("#export-form")) return;
    const trim = this.xField("trim_selection");
    trim.disabled = !this.sel;
    if (!this.sel) trim.checked = false;
    $("#x-trim-sel-text").textContent = this.sel
      ? t("exp.trimSel", { range: this.selText(), len: this.fmt(this.sel.b - this.sel.a), total: this.fmt(this.seconds) })
      : t("exp.trimSelNone");
    $("#trim-sel-row").classList.toggle("disabled", !this.sel);
    const norm = this.xField("normalize").value;
    $("#x-lufs-field").hidden = norm !== "loudness";
    $("#x-xfade-field").hidden = !this.xField("loop").checked;
    // plain-language summary of what the export will do
    const parts = [];
    if (trim.checked) parts.push(t("sum.trim", { range: this.selText() }));
    if (this.xField("trim_silence").checked) parts.push(t("sum.silence"));
    if (this.xField("mono").checked) parts.push(t("sum.mono"));
    if (norm === "peak") parts.push(t("sum.peak", { db: this.xField("peak_ceiling_db").value || "-1" }));
    if (norm === "loudness") parts.push(t("sum.lufs", { lufs: this.xField("target_lufs").value || "-16" }));
    if (this.xField("loop").checked) parts.push(t("sum.loop", { ms: this.xField("loop_crossfade_ms").value || "200" }));
    const { fi, fo, clamped } = this.fades();
    if (fi || fo) parts.push(t(clamped ? "sum.fadeShort" : "sum.fade", { a: fi, b: fo }));
    const fmt = String(this.xField("format").value).toUpperCase(), sr = Number(this.xField("sample_rate").value) / 1000;
    parts.push(`${fmt} ${sr} kHz`);
    $("#x-summary").textContent = t("sum.prefix", { len: this.fmt(this.outLen()) }) + parts.join(" · ");
    $("#x-submit").textContent = this.seconds ? t("exp.submitLen", { len: this.fmt(this.outLen()), fmt }) : t("exp.submit");
    $("#x-submit").disabled = !this.file;
    if (this.overview) this.renderHints();
  },
  async submitExport() {
    const f = $("#export-form");
    clearErrors(f, $("#x-error"));
    const { fi, fo } = this.fades();
    const args = { input_audio: this.file, trim_silence: this.xField("trim_silence").checked, mono: this.xField("mono").checked,
      normalize: this.xField("normalize").value, loop: this.xField("loop").checked,
      fade_in_ms: fi, fade_out_ms: fo, format: this.xField("format").value,
      sample_rate: Number(this.xField("sample_rate").value) };
    // an emptied field means "the tool's default", not 0
    const num = (k, name = k) => { const raw = this.xField(k).value.trim(); if (raw !== "") args[name] = Number(raw); };
    num("peak_ceiling_db");
    if (args.normalize === "loudness") num("target_lufs");
    if (args.loop) num("loop_crossfade_ms");
    if (this.xField("trim_selection").checked && this.sel) {
      args.start_seconds = +this.sel.a.toFixed(3);
      args.end_seconds = +this.sel.b.toFixed(3);
    }
    const title = this.xField("title").value.trim();
    if (title) args.title = title;
    $("#x-submit").disabled = true;
    const preset = this.preset;
    const job = await queueJob("process_audio", args, {
      onError: (e) => showFormError(e.message, e.body && e.body.fields, f, $("#x-error"), $("#x-custom")) });
    if (job) { state.presets.set(job.id, preset); renderJobs(); }
    this.updateExportForm();
  },

  // ── results of this file ───────────────────────────────────────────────
  renderJobs(list) {
    const mine = this.file ? list.filter((j) => baseName((j.arguments || {}).input_audio || "") === this.file) : [];
    renderJobList($("#edit-jobs"), mine);
    $("#edit-jobs-empty").hidden = mine.length > 0;
  },
  jobFinished(job) {
    if (job.status !== "done") return;
    if (job.tool === "process_audio" && job.result && job.result.path) {
      const file = baseName(job.result.path);
      toast(t("toast.exported", { file }), { label: t("act.download"), run: () => {
        const a = el("a", { href: audioUrl(file, true), download: file });
        document.body.append(a); a.click(); a.remove();
      } });
    }
  },
};
