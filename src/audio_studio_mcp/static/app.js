// Audio Studio web UI. Plain JS, no build step. Talks to /api (see web.py).
// Load order: i18n.js, wave.js, editor.js, app.js (this file boots the page).
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = {
  lang: "en",
  status: null,
  loaded: false,          // model loaded (from /api/jobs while polling)
  jobs: new Map(),        // id -> job view from the server
  expect: new Map(),      // id -> audio seconds the job will produce (for the estimate)
  polling: false,
  lib: { items: [], total: 0, query: "" },
};
const PAGE = 50;
const RESTORE_JOBS = 8;
const TYPES = ["music", "instrument", "sfx"];

// Example prompts per type: a click fills the form. Prompts stay English (the
// model's language); the chip label is translated.
const EXAMPLES = {
  music: [
    { key: "lofi", prompt: "Lo-fi hip-hop chill track with mellow electric piano, soft vinyl crackle, low-pass filtered drums and soft plucked bass for a relaxed, dreamy vibe.", seconds: 60, bpm: 80 },
    { key: "chiptune", prompt: "Upbeat 8-bit chiptune adventure theme with bright square-wave lead, bubbly arpeggios and punchy noise drums, cheerful and energetic.", seconds: 45, bpm: 140 },
    { key: "epic", prompt: "Epic orchestral battle music with driving staccato strings, powerful brass fanfares, thundering taiko drums and a heroic, urgent mood.", seconds: 90, bpm: 120 },
    { key: "ambient", prompt: "Calm ambient soundscape with warm evolving synth pads, gentle felt piano notes and distant shimmering textures, peaceful and spacious.", seconds: 120 },
  ],
  instrument: [
    { key: "guitar", prompt: "Fingerpicked nylon-string acoustic guitar loop, warm and intimate, close-mic recording with soft room tone.", seconds: 12, bpm: 90 },
    { key: "drums", prompt: "Tight funk drum loop with crisp snare, punchy kick and swung closed hi-hats, dry studio recording.", seconds: 8, bpm: 100 },
    { key: "pad", prompt: "Lush analog synth pad sustaining a minor chord, slow filter sweep, wide stereo and soft reverb.", seconds: 16 },
  ],
  sfx: [
    { key: "glass", prompt: "", source: "glass bottle", action: "shattering on impact, fragments scattering", material: "thick green glass, sharp bright transient", space: "concrete floor, close-up, dry", seconds: 3 },
    { key: "footsteps", prompt: "", source: "footsteps", action: "slow walking, steady pace", material: "leather boots on gravel", space: "outdoors, close-up", seconds: 5 },
    { key: "rain", prompt: "Steady rain on a window with occasional distant thunder rumbles.", source: "", action: "", material: "", space: "indoors, cozy room", seconds: 12 },
    { key: "ui", prompt: "", source: "soft UI confirm chime", action: "short bright two-note rise", material: "clean synthetic bell tone", space: "dry, close", seconds: 1.5 },
    { key: "door", prompt: "", source: "old wooden door", action: "slowly creaking open then closing with a thud", material: "worn wood, rusty metal hinges", space: "small stone room, slight reverb", seconds: 4 },
  ],
};
// Length presets per type (seconds).
const DURATIONS = {
  music: [30, 60, 120, 180],
  instrument: [8, 12, 20, 30],
  sfx: [1.5, 3, 5, 10],
};

// ── i18n ───────────────────────────────────────────────────────────────────
function pickLang(pref) {
  if (pref && pref !== "auto" && I18N[pref]) return pref;
  for (const l of navigator.languages || [navigator.language || "en"]) {
    const base = l.toLowerCase().split("-")[0];
    if (I18N[base]) return base;
  }
  return "en";
}
function t(key, vars) {
  let s = (I18N[state.lang] && I18N[state.lang][key]) ?? I18N.en[key] ?? key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
  return s;
}
function applyI18n() {
  document.documentElement.lang = state.lang === "zh" ? "zh-CN" : state.lang;
  document.querySelectorAll("[data-i18n]").forEach((n) => { n.textContent = t(n.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((n) => { n.placeholder = t(n.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-title]").forEach((n) => { n.title = t(n.dataset.i18nTitle); });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((n) => { n.setAttribute("aria-label", t(n.dataset.i18nAriaLabel)); });
  updateTypeUI();
  renderStatus();
  renderJobs();
  renderLibrary();
  renderStarters();
  Editor.applyI18n();
}

// ── helpers ────────────────────────────────────────────────────────────────
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(n.dataset, v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) n.append(c);
  return n;
}
function fmtTime(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
function fmtLen(sec) {
  if (typeof sec !== "number") return "";
  return sec < 60 ? `${sec.toFixed(sec < 10 ? 1 : 0)} s` : fmtTime(sec);
}
function fmtDate(epoch) {
  const loc = state.lang === "zh" ? "zh-CN" : state.lang;
  return new Date(epoch * 1000).toLocaleString(loc, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function audioUrl(file, download) {
  return `/api/audio/${encodeURIComponent(file)}${download ? "?download=1" : ""}`;
}
function baseName(p) { return String(p).split(/[\\/]/).pop(); }
function typing(e) {
  const n = e.target;
  return n && (n.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(n.tagName));
}

let toastTimer;
function toast(msg, action) {
  const n = $("#toast");
  n.replaceChildren(el("span", { text: msg }), action ? el("button", { class: "link-btn toast-act", type: "button", text: action.label,
    onclick: () => { n.hidden = true; action.run(); } }) : null);
  n.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { n.hidden = true; }, action ? 6000 : 2200);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast(t("toast.copied")); }
  catch { toast(t("toast.copyFailed")); }
}

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  let res;
  try { res = await fetch(path, init); }
  catch { const e = new Error(t("err.network")); e.network = true; throw e; }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(body.error || res.statusText); e.body = body; e.status = res.status; throw e; }
  return body;
}

// A small "⋯" menu (details/summary) for secondary actions.
function moreMenu(items) {
  const menu = el("details", { class: "menu" },
    el("summary", { class: "menu-btn", "aria-label": t("act.more"), title: t("act.more") }, "⋯"),
    el("div", { class: "menu-list", role: "menu" }, items.filter(Boolean).map(([label, run]) =>
      el("button", { type: "button", role: "menuitem", text: label, onclick: () => { menu.open = false; run(); } }))));
  return menu;
}
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.menu[open]").forEach((m) => { if (!m.contains(e.target)) m.open = false; });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll("details.menu[open]").forEach((m) => { m.open = false; m.querySelector("summary").focus(); });
});

// ── theme / language settings ────────────────────────────────────────────────
function initSettings() {
  const theme = localStorage.getItem("as.theme") || "system";
  $("#theme").value = theme;
  $("#theme").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "system") { delete document.documentElement.dataset.theme; localStorage.removeItem("as.theme"); }
    else { document.documentElement.dataset.theme = v; localStorage.setItem("as.theme", v); }
    window.dispatchEvent(new Event("as:theme"));
  });
  const lang = localStorage.getItem("as.lang") || "auto";
  $("#lang").value = lang;
  state.lang = pickLang(lang);
  $("#lang").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "auto") localStorage.removeItem("as.lang"); else localStorage.setItem("as.lang", v);
    state.lang = pickLang(v);
    applyI18n();
  });
}

// ── pages ────────────────────────────────────────────────────────────────────
// Hash routes: #generate, #library, #edit/<file name>
function showPage(name, push = true) {
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.page === name)));
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${name}`; });
  const hash = name === "edit" && Editor.file ? `#edit/${encodeURIComponent(Editor.file)}` : `#${name}`;
  if (push && location.hash !== hash) history.replaceState(null, "", hash);
  if (name === "library") loadLibrary(true);
  if (name === "edit") Editor.shown();
  else Editor.hidden();
}
function openEditor(file) {
  Editor.open(file);
  showPage("edit");
}
function route() {
  const h = location.hash;
  if (h.startsWith("#edit/")) openEditor(decodeURIComponent(h.slice(6)));
  else showPage(h === "#library" ? "library" : h === "#edit" ? "edit" : "generate", false);
}
function initTabs() {
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.page)));
  window.addEventListener("hashchange", route);  // typed or pasted links
  route();
}

// ── status bar ───────────────────────────────────────────────────────────────
function renderStatus() {
  const box = $("#model-state"), text = $("#model-text"), s = state.status;
  if (!s) { box.dataset.state = "error"; text.textContent = t("status.offline"); $("#unload").disabled = true; return; }
  const busy = s.jobs.running > 0;
  box.dataset.state = busy ? "busy" : s.loaded ? "loaded" : "idle";
  text.textContent = busy ? t("status.working") : s.loaded ? t("status.loaded") : t("status.notLoaded");
  box.title = `${s.backend} — ${s.model}`;
  $("#version").textContent = `v${s.version}`;
  $("#unload").disabled = !s.loaded || busy;
  $("#queue-info").textContent = (s.jobs.running || s.jobs.queued) ? t("jobs.queueInfo", s.jobs) : "";
  $("#lib-folder").textContent = t("lib.folder", { dir: s.output_dir });
  updateDurationUI();
}
async function refreshStatus() {
  try { state.status = await api("/api/status"); state.loaded = state.status.loaded; }
  catch { state.status = null; }
  renderStatus();
}
async function unloadModel() {
  $("#unload").disabled = true;
  try {
    const r = await api("/api/unload", { json: {} });
    toast(r.unloaded ? t("status.unloaded") : t("status.nothingToUnload"));
  } catch (e) { toast(e.message); }
  refreshStatus();
}

// ── generate form ────────────────────────────────────────────────────────────
const form = () => $("#gen-form");
const field = (name) => form().elements.namedItem(name);
function currentType() { return field("audio_type").value; }
function defaultSeconds(type) { return state.status ? state.status.default_seconds[type] : null; }
function updateDurationUI() {
  const type = currentType(), d = defaultSeconds(type);
  const input = $("#duration_seconds");
  input.placeholder = d ? String(d) : "";
  if (state.status) input.max = String(state.status.max_seconds);
  const max = state.status ? state.status.max_seconds : 380;
  $("#duration-hint").textContent = t("gen.durationHint", { def: d ?? "—", max });
  const cur = input.value.trim() === "" ? d : Number(input.value);
  $("#durations").replaceChildren(...DURATIONS[type].filter((s) => s <= max).map((s) =>
    el("button", { type: "button", class: "chip", "aria-pressed": String(cur === s), text: fmtLen(s),
      onclick: () => { input.value = s; updateDurationUI(); } })));
}
function updateTypeUI() {
  const type = currentType(), sfx = type === "sfx";
  $("#sfx-fields").hidden = !sfx;
  $("#bpm-field").hidden = sfx;
  $("#type-hint").textContent = t(`type.${type}Hint`);
  $("#prompt-label").textContent = t(`gen.prompt.${type}`);
  $("#prompt-hint").textContent = t(`gen.promptHint.${type}`);
  $("#prompt").placeholder = t(`gen.promptPh.${type}`);
  $("#examples").replaceChildren(el("span", { class: "chips-label muted small", text: t("gen.tryExample") }),
    ...EXAMPLES[type].map((ex) => el("button", { type: "button", class: "chip", text: t(`ex.${ex.key}`),
      title: ex.prompt || [ex.source, ex.action, ex.material, ex.space].filter(Boolean).join(", "),
      onclick: () => applyExample(type, ex) })));
  updateDurationUI();
  updateSubmitLabel();
}
function applyExample(type, ex) {
  field("audio_type").value = type;
  $("#prompt").value = ex.prompt || "";
  for (const [k, v] of [["sound_source", ex.source], ["sound_action", ex.action], ["sound_material", ex.material], ["sound_space", ex.space]]) field(k).value = v || "";
  $("#duration_seconds").value = ex.seconds ?? "";
  $("#bpm").value = ex.bpm ?? "";
  clearErrors();
  updateTypeUI();
  $("#prompt").focus();
}
function updateSubmitLabel() {
  const n = parseInt(field("variations").value, 10) || 1;
  $("#gen-submit").textContent = n > 1 ? t("gen.submitN", { n }) : t("gen.submit");
}
const ADV_FIELDS = ["seed", "cfg", "steps", "peak_ceiling_db", "negative_prompt", "title"];
function updateAdvancedDot() {
  $("#adv-dot").hidden = !ADV_FIELDS.some((k) => field(k).value.trim() !== "");
}

// Sent as typed; the server validates (a seed of 1.5 is an error, not 1).
const NUMBER_FIELDS = ["duration_seconds", "bpm", "seed", "cfg", "steps", "peak_ceiling_db"];
function readForm() {
  const type = currentType();
  const args = { audio_type: type, prompt: field("prompt").value.trim(), variations: parseInt(field("variations").value, 10) };
  if (type === "sfx") {
    for (const k of ["sound_source", "sound_action", "sound_material", "sound_space"]) {
      const v = field(k).value.trim();
      if (v) args[k] = v;
    }
  }
  for (const k of NUMBER_FIELDS) {
    if (k === "bpm" && type === "sfx") continue;
    const raw = field(k).value.trim();
    if (raw !== "") args[k] = Number(raw);
  }
  const title = field("title").value.trim();
  if (title) args.title = title;
  const neg = field("negative_prompt").value.trim();
  if (neg) args.negative_prompt = neg;
  return args;
}
function fillForm(meta) {
  form().reset();
  const type = TYPES.includes(meta.audio_type) ? meta.audio_type : "music";
  field("audio_type").value = type;
  field("prompt").value = meta.prompt || "";
  const sf = meta.sound_fields || {};
  field("sound_source").value = sf.source || ""; field("sound_action").value = sf.action || "";
  field("sound_material").value = sf.material || ""; field("sound_space").value = sf.space || "";
  field("duration_seconds").value = meta.seconds ?? "";
  field("bpm").value = meta.bpm ?? "";
  field("seed").value = meta.seed ?? "";
  field("cfg").value = meta.cfg != null && meta.cfg !== 1 ? meta.cfg : "";
  field("steps").value = meta.steps != null && meta.steps !== 8 ? meta.steps : "";
  field("negative_prompt").value = meta.negative_prompt || "";
  if (field("cfg").value || field("steps").value || field("negative_prompt").value) $("#advanced").open = true;
  updateTypeUI();
  updateAdvancedDot();
}
function clearErrors(f = form(), box = $("#form-error")) {
  box.hidden = true;
  f.querySelectorAll("[aria-invalid]").forEach((n) => n.removeAttribute("aria-invalid"));
}
// Show an API error next to the form; mark the fields it names.
function showFormError(msg, fields, f = form(), box = $("#form-error"), adv = $("#advanced")) {
  box.textContent = msg; box.hidden = false;
  for (const fe of fields || []) {
    const input = f.elements.namedItem(fe.field);
    const node = input && (input.setAttribute ? input : input[0]);
    if (node && node.setAttribute) {
      node.setAttribute("aria-invalid", "true");
      if (adv && adv.contains(node)) adv.open = true;
    }
  }
}
// Queue a tool call; returns the job or null (the error is shown by onError).
async function queueJob(tool, args, { onError, expectSeconds } = {}) {
  try {
    const job = await api("/api/jobs", { json: { tool, arguments: args } });
    state.jobs.set(job.id, job);
    if (expectSeconds) state.expect.set(job.id, expectSeconds);
    renderJobs();
    pollJobs();
    refreshStatus();
    return job;
  } catch (e) {
    if (onError) onError(e); else toast(e.message);
    return null;
  }
}
async function submitJob(args) {
  clearErrors();
  const hasText = args.prompt || args.sound_source || args.sound_action || args.sound_material || args.sound_space;
  if (!hasText) { showFormError(t("err.emptyPrompt")); $("#prompt").setAttribute("aria-invalid", "true"); $("#prompt").focus(); return null; }
  $("#gen-submit").disabled = true;
  try {
    const secs = args.duration_seconds ?? defaultSeconds(args.audio_type);
    return await queueJob("generate_audio", args, { expectSeconds: secs * (args.variations || 1),
      onError: (e) => showFormError(e.message, e.body && e.body.fields) });
  } finally {
    $("#gen-submit").disabled = false;
  }
}
function initForm() {
  form().addEventListener("change", (e) => {
    if (e.target.name === "audio_type") updateTypeUI();
    if (e.target.name === "variations") updateSubmitLabel();
  });
  form().addEventListener("input", (e) => {
    if (e.target.removeAttribute) e.target.removeAttribute("aria-invalid");
    if (e.target.id === "duration_seconds") updateDurationUI();
    updateAdvancedDot();
  });
  form().addEventListener("submit", (e) => { e.preventDefault(); submitJob(readForm()); });
  form().addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form().requestSubmit(); }
  });
  $("#gen-reset").addEventListener("click", () => { form().reset(); clearErrors(); updateTypeUI(); updateAdvancedDot(); });
  $("#adv-reset").addEventListener("click", () => {
    for (const k of ADV_FIELDS) field(k).value = "";
    updateAdvancedDot();
  });
  $("#unload").addEventListener("click", unloadModel);
}

// Starter cards in the empty Results area: one click fills the form.
function renderStarters() {
  const picks = [["sfx", EXAMPLES.sfx[0]], ["music", EXAMPLES.music[1]], ["instrument", EXAMPLES.instrument[0]]];
  $("#starters").replaceChildren(...picks.map(([type, ex]) => el("button", { type: "button", class: "starter-card",
    onclick: () => applyExample(type, ex) },
    el("span", { class: "badge", text: t(`type.${type}`) }),
    el("span", { class: "starter-name", text: t(`ex.${ex.key}`) }),
    el("span", { class: "muted small", text: fmtLen(ex.seconds) }))));
}

// ── jobs ─────────────────────────────────────────────────────────────────────
function rates() { try { return JSON.parse(localStorage.getItem("as.rates") || "{}"); } catch { return {}; } }
function rateKey(job) { return `${job.tool}:${job.arguments.mode || ""}`; }
// Learn seconds of wall time per second of audio from finished jobs (without model load).
function learnRate(job) {
  const r = job.result;
  if (!r || r.included_model_load || !r.wall_seconds || !r.seconds) return;
  const n = (r.candidates || []).length || 1;
  const all = rates();
  all[rateKey(job)] = r.wall_seconds / (r.seconds * n);
  localStorage.setItem("as.rates", JSON.stringify(all));
}
function estimate(job) {
  const secs = state.expect.get(job.id), rate = rates()[rateKey(job)];
  return secs && rate ? Math.max(1, rate * secs) : null;
}
function jobStateText(job) {
  if (job.status === "queued") return t("jobs.queued", { n: job.position ?? 1 });
  if (job.status === "running") {
    if (job.tool !== "process_audio" && !state.loaded) return t("jobs.loading", { t: fmtTime(job.elapsed) });
    const est = estimate(job);
    return est ? t("jobs.runningEst", { t: fmtTime(job.elapsed), e: fmtTime(est) }) : t("jobs.running", { t: fmtTime(job.elapsed) });
  }
  if (job.status === "done") return t("jobs.done", { t: fmtTime(job.elapsed) });
  if (job.status === "cancelled") return t("jobs.cancelled");
  return t("jobs.error");
}
function promptLabel(a) {
  const parts = [a.prompt, a.sound_source, a.sound_action, a.sound_material, a.sound_space].filter(Boolean);
  return parts.join(", ");
}
function jobTitle(job) {
  const a = job.arguments || {};
  if (job.tool === "process_audio") return t("jobs.exportOf", { file: baseName(a.input_audio || "") });
  if (job.tool === "edit_audio") return `${t(`mode.${a.mode}`)} · ${promptLabel(a) || baseName(a.input_audio || "")}`;
  return promptLabel(a) || "—";
}
function jobBadge(job) {
  if (job.tool === "process_audio") return t("kind.process");
  return t(`type.${(job.arguments || {}).audio_type || "music"}`);
}
const LETTERS = "ABCD";
function candidateCard(c, i, job) {
  const file = baseName(c.path), mini = new MiniWave(file, c.seconds);
  const many = job.result.candidates.length > 1;
  return el("div", { class: "cand", dataset: { index: i } },
    el("div", { class: "cand-head" },
      el("strong", { text: many ? t("cand.label", { n: LETTERS[i] || i + 1 }) : t("cand.single") }),
      c.seed != null ? el("span", { class: "muted", text: t("cand.seed", { seed: c.seed }) }) : null),
    mini.node,
    el("div", { class: "cand-actions" },
      el("button", { class: "btn btn-sm", type: "button", text: t("act.edit"), onclick: () => openEditor(file) }),
      el("a", { class: "btn btn-sm btn-quiet", href: audioUrl(file, true), download: file, text: t("act.download") }),
      moreMenu([
        [t("act.copyPath"), () => copyText(c.path)],
        job.tool === "generate_audio" && c.seed != null ? [t("act.useSeed"), () => {
          field("seed").value = c.seed; $("#advanced").open = true; updateAdvancedDot(); showPage("generate");
          toast(t("toast.seedSet", { seed: c.seed }));
        }] : null,
      ])));
}
function resultCandidates(job) {
  const r = job.result || {};
  if (Array.isArray(r.candidates)) return r.candidates;
  if (r.path) return [{ path: r.path, seconds: r.seconds }];  // process_audio: one file
  return [];
}
function jobCard(job) {
  const active = job.status === "queued" || job.status === "running";
  const head = el("div", { class: "job-head" },
    el("span", { class: "badge", text: jobBadge(job) }),
    el("span", { class: "job-prompt", title: jobTitle(job), text: jobTitle(job) }),
    el("span", { class: "job-state" }, active ? el("span", { class: "spinner", "aria-hidden": "true" }) : null,
      el("span", { class: "job-state-text", text: jobStateText(job) })));
  const card = el("article", { class: "panel job", dataset: { status: job.status, id: job.id, tool: job.tool }, onkeydown: (e) => abKeys(e, card) }, head);
  if (job.status === "queued") {
    head.append(el("button", { class: "btn btn-sm btn-quiet", type: "button", text: t("act.cancel"), onclick: () => cancelJob(job) }));
  }
  if (job.status === "running") card.append(el("div", { class: "progress", "aria-hidden": "true" }, el("div")));
  if (job.status === "error") card.append(el("p", { class: "job-error", text: job.error || "" }));
  if (job.status === "done") {
    const cands = resultCandidates(job);
    if (job.result && job.result.candidates) {
      card.append(el("div", { class: "candidates" }, cands.map((c, i) => candidateCard(c, i, job))));
    } else {
      card.append(el("div", { class: "candidates" }, cands.map((c) => candidateCard(c, 0, { ...job, result: { candidates: [c] } }))));
      if (job.result && job.result.steps) card.append(el("p", { class: "muted small job-steps", text: job.result.steps.join(" → ") }));
    }
    const foot = el("div", { class: "job-foot" });
    if (cands.length > 1) foot.append(el("span", { class: "muted small", text: t("cand.abHint") }));
    if (job.tool === "generate_audio") {
      foot.append(el("button", { class: "link-btn", type: "button", text: t("act.moreLike"), onclick: () => moreLikeThis(job) }));
    }
    if (foot.children.length) card.append(foot);
  }
  return card;
}
function updateProgress(card, job) {
  const bar = card.querySelector(".progress > div"), est = estimate(job);
  if (!bar) return;
  if (est && state.loaded) {
    bar.parentElement.classList.add("determinate");
    bar.style.width = `${Math.min(95, (job.elapsed / est) * 100)}%`;
  } else {
    bar.parentElement.classList.remove("determinate");
    bar.style.width = "";
  }
}
// 1–4 (or A–D) play candidate N of this card from the same position; ←/→ step.
function abKeys(e, card) {
  if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  const minis = [...card.querySelectorAll(".mini-wave")].map((n) => n.mini);
  if (minis.length < 2) return;
  let i = "1234".indexOf(e.key);
  if (i < 0) i = "abcd".indexOf(e.key.toLowerCase());
  const cur = minis.indexOf(Player.owner);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    i = ((cur < 0 ? 0 : cur) + (e.key === "ArrowDown" ? 1 : minis.length - 1)) % minis.length;
  }
  if (i < 0 || i >= minis.length) return;
  e.preventDefault();
  const at = cur >= 0 ? Player.audio.currentTime : 0;
  Player.play(minis[i], at);
  minis[i].track.focus();
}
async function cancelJob(job) {
  try { state.jobs.set(job.id, await api(`/api/jobs/${job.id}/cancel`, { json: {} })); }
  catch (e) { toast(e.message); }
  renderJobs();
}
function moreLikeThis(job) {
  const args = { ...job.arguments };
  delete args.seed;  // new random seeds
  delete args.title;
  submitJob(args).then((j) => { if (j) toast(t("toast.moreQueued")); });
}
// Generate-page jobs go to Results; edit/process jobs to the editor of their input.
function renderJobList(box, list) {
  const existing = new Map([...box.children].map((n) => [n.dataset.id, n]));
  const nodes = list.map((job) => {
    const old = existing.get(job.id);
    if (old && old.dataset.status === job.status && old.dataset.lang === state.lang) {
      const st = old.querySelector(".job-state-text");
      if (st) st.textContent = jobStateText(job);
      updateProgress(old, job);
      return old;
    }
    const card = jobCard(job);
    card.dataset.lang = state.lang;
    updateProgress(card, job);
    return card;
  });
  nodes.forEach((n, i) => { if (box.children[i] !== n) box.insertBefore(n, box.children[i] || null); });
  while (box.children.length > nodes.length) box.lastChild.remove();
}
function renderJobs() {
  const list = [...state.jobs.values()].sort((x, y) => y.created - x.created);
  const gen = list.filter((j) => j.tool === "generate_audio");
  renderJobList($("#jobs"), gen);
  $("#jobs-empty").hidden = gen.length > 0;
  Editor.renderJobs(list.filter((j) => j.tool !== "generate_audio"));
}
let unseen = 0;
function noteFinished() {
  if (!document.hidden) return;
  unseen++;
  document.title = `(${unseen}) ✓ Audio Studio`;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { unseen = 0; document.title = "Audio Studio"; } });
async function pollJobs() {
  if (state.polling) return;
  state.polling = true;
  try {
    while ([...state.jobs.values()].some((j) => j.status === "queued" || j.status === "running")) {
      let finished = false;
      try {
        const r = await api("/api/jobs");
        state.loaded = r.loaded;
        const seen = new Set();
        for (const j of r.items) {
          const prev = state.jobs.get(j.id);
          if (prev && prev.status !== j.status && (j.status === "done" || j.status === "error")) {
            finished = true;
            if (j.status === "done") { learnRate(j); noteFinished(); }
            if (j.tool !== "generate_audio") Editor.jobFinished(j);
          }
          if (prev || j.status === "queued" || j.status === "running") state.jobs.set(j.id, j);
          seen.add(j.id);
        }
        // The server restarted and forgot these: stop waiting for them.
        for (const j of state.jobs.values()) {
          if (!seen.has(j.id) && (j.status === "queued" || j.status === "running")) {
            state.jobs.set(j.id, { ...j, status: "error", position: null, error: t("jobs.lost") });
          }
        }
        renderJobs();
      } catch { /* server restarting: keep trying */ }
      if (finished) { refreshStatus(); if (!$("#page-library").hidden) loadLibrary(true); }
      await new Promise((r) => setTimeout(r, 800));
    }
  } finally {
    state.polling = false;
    refreshStatus();
  }
}
async function restoreJobs() {
  try {
    const r = await api("/api/jobs");
    state.loaded = r.loaded;
    // After a reload show the latest few jobs plus anything still waiting.
    r.items.forEach((j, i) => { if (i < RESTORE_JOBS || j.status === "queued" || j.status === "running") state.jobs.set(j.id, j); });
    renderJobs();
    pollJobs();
  } catch { /* offline */ }
}

// ── library ──────────────────────────────────────────────────────────────────
function metaRows(item) {
  const m = item.meta;
  if (!m) return [[t("meta.path"), item.path], ["", t("meta.noSidecar")]];
  const sf = m.sound_fields && Object.values(m.sound_fields).filter(Boolean).join(" / ");
  const rows = [
    [t("meta.kind"), kindLabel(m)],
    [t("meta.type"), m.audio_type && t(`type.${m.audio_type}`)],
    [t("meta.prompt"), m.prompt], [t("meta.sound"), sf], [t("meta.promptUsed"), m.prompt_used],
    [t("meta.idea"), m.original_idea],
    [t("meta.seconds"), fmtLen(typeof m.seconds === "number" ? m.seconds : item.seconds)],
    [t("meta.bpm"), m.bpm], [t("meta.seed"), m.seed], [t("meta.cfg"), m.cfg], [t("meta.steps"), m.steps],
    [t("meta.strength"), m.strength], [t("meta.region"), Array.isArray(m.region) ? m.region.map((x) => `${x}s`).join(" – ") : null],
    [t("meta.negative"), m.negative_prompt],
    [t("meta.stepsDone"), Array.isArray(m.steps) ? m.steps.join(" → ") : null],
    [t("meta.format"), m.kind === "process" ? `${String(m.format || "").toUpperCase()} · ${m.sample_rate} Hz · ${m.channels === 1 ? "mono" : "stereo"}` : null],
    [t("meta.loudness"), m.output_lufs != null ? `${m.output_lufs} LUFS` : null],
    [t("meta.peak"), m.peak_dbfs != null ? `${m.peak_dbfs} dBFS` : null],
    [t("meta.model"), m.model ? `${m.model} (${m.backend})` : null],
    [t("meta.input"), m.input_audio], [t("meta.created"), m.created], [t("meta.path"), item.path],
  ];
  return rows.filter(([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v)));
}
function kindLabel(m) {
  const kind = String(m.kind || "");
  if (kind.startsWith("edit")) return `${t("kind.edit")} · ${t(`mode.${kind.split(":")[1]}`)}`;
  return I18N.en[`kind.${kind}`] ? t(`kind.${kind}`) : kind;
}
function regenerate(item) {
  const m = item.meta;
  fillForm(m);
  showPage("generate");
  const args = readForm();
  args.variations = 1;
  args.seed = m.seed;
  if (m.title) args.title = m.title;
  submitJob(args).then((job) => { if (job) toast(t("toast.regenQueued")); });
}
function libraryItem(item) {
  const m = item.meta || {};
  const title = m.title || item.file;
  // The form has no "raw" type, so only these can be regenerated faithfully.
  const canRegen = m.kind === "generate" && m.seed != null && TYPES.includes(m.audio_type);
  const details = el("dl", { class: "item-details", hidden: true },
    metaRows(item).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: String(v) })]));
  const toggle = el("button", { class: "btn btn-sm btn-quiet", type: "button", "aria-expanded": "false", text: t("act.details") });
  toggle.addEventListener("click", () => {
    details.hidden = !details.hidden;
    toggle.setAttribute("aria-expanded", String(!details.hidden));
    toggle.textContent = details.hidden ? t("act.details") : t("act.hideDetails");
  });
  const kind = String(m.kind || "");
  const badges = [m.audio_type ? t(`type.${m.audio_type}`) : null,
    kind.startsWith("edit") ? t(`mode.${kind.split(":")[1]}`) : kind === "process" ? t("kind.process") : null,
    kind === "process" || !m.kind ? item.format.toUpperCase() : null].filter(Boolean);
  const mini = new MiniWave(item.file, item.seconds);
  const sub = promptLabel({ prompt: m.prompt, ...prefixSound(m.sound_fields) }) || (m.source && m.source.prompt) || "";
  return el("article", { class: "panel item" },
    el("div", { class: "item-main" },
      el("div", { class: "item-title", title: item.file, text: title }),
      el("div", { class: "item-meta" },
        badges.map((b) => el("span", { class: "badge", text: b })),
        el("span", { text: fmtDate(item.mtime) }),
        m.seed != null ? el("span", { text: t("cand.seed", { seed: m.seed }) }) : null),
      sub ? el("div", { class: "item-prompt", title: sub, text: sub }) : null),
    el("div", { class: "item-side" },
      mini.node,
      el("div", { class: "item-actions" },
        el("button", { class: "btn btn-sm", type: "button", text: t("act.edit"), onclick: () => openEditor(item.file) }),
        el("a", { class: "btn btn-sm btn-quiet", href: audioUrl(item.file, true), download: item.file, text: t("act.download") }),
        toggle,
        moreMenu([
          canRegen ? [t("act.regen"), () => regenerate(item)] : null,
          [t("act.copyPath"), () => copyText(item.path)],
        ]))),
    details);
}
function prefixSound(sf) {
  sf = sf || {};
  return { sound_source: sf.source, sound_action: sf.action, sound_material: sf.material, sound_space: sf.space };
}
function renderLibrary() {
  const box = $("#library"), L = state.lib;
  box.replaceChildren(...L.items.map(libraryItem));
  $("#lib-count").textContent = t("lib.count", { n: L.total });
  $("#lib-empty").hidden = L.items.length > 0;
  $("#lib-empty-text").textContent = L.query ? t("lib.noMatch", { q: L.query }) : t("lib.empty");
  $("#lib-empty-btn").textContent = L.query ? t("lib.clearSearch") : t("lib.generateFirst");
  $("#lib-more").hidden = L.items.length >= L.total;
}
let libSeq = 0;
async function loadLibrary(reset) {
  const L = state.lib, seq = ++libSeq;
  const offset = reset ? 0 : L.items.length;
  try {
    const r = await api(`/api/library?q=${encodeURIComponent(L.query)}&offset=${offset}&limit=${PAGE}`);
    if (seq !== libSeq) return;  // a newer search finished first
    L.items = reset ? r.items : L.items.concat(r.items);
    L.total = r.total;
  } catch (e) {
    if (seq !== libSeq) return;
    L.items = []; L.total = 0;
    toast(e.message);
  }
  renderLibrary();
}
function initLibrary() {
  let timer;
  $("#lib-search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.lib.query = e.target.value.trim(); loadLibrary(true); }, 200);
  });
  $("#lib-refresh").addEventListener("click", () => loadLibrary(true));
  $("#lib-more").addEventListener("click", () => loadLibrary(false));
  $("#lib-empty-btn").addEventListener("click", () => {
    if (state.lib.query) { $("#lib-search").value = ""; state.lib.query = ""; loadLibrary(true); }
    else { showPage("generate"); $("#prompt").focus(); }
  });
}

// ── boot ─────────────────────────────────────────────────────────────────────
Player.init();
initSettings();
initForm();
initLibrary();
Editor.init();
applyI18n();
initTabs();
refreshStatus();
restoreJobs();
setInterval(refreshStatus, 5000);
