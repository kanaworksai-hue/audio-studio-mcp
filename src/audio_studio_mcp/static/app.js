// Audio Studio web UI. Plain JS, no build step. Talks to /api (see web.py).
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = {
  lang: "en",
  status: null,
  jobs: new Map(),        // id -> job view from the server
  polling: false,
  lib: { items: [], total: 0, offset: 0, query: "" },
};
const PAGE = 50;

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
  updateTypeUI();
  renderStatus();
  renderJobs();
  renderLibrary();
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

let toastTimer;
function toast(msg) {
  const n = $("#toast");
  n.textContent = msg; n.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { n.hidden = true; }, 2200);
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

// ── theme / language settings ────────────────────────────────────────────────
function initSettings() {
  const theme = localStorage.getItem("as.theme") || "system";
  $("#theme").value = theme;
  $("#theme").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "system") { delete document.documentElement.dataset.theme; localStorage.removeItem("as.theme"); }
    else { document.documentElement.dataset.theme = v; localStorage.setItem("as.theme", v); }
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

// ── tabs ─────────────────────────────────────────────────────────────────────
function showPage(name, push = true) {
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.page === name)));
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== `page-${name}`; });
  if (push && location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "library") loadLibrary(true);
}
function initTabs() {
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.page)));
  showPage(location.hash === "#library" ? "library" : "generate", false);
}

// ── status bar ───────────────────────────────────────────────────────────────
function renderStatus() {
  const box = $("#model-state"), text = $("#model-text"), s = state.status;
  if (!s) { box.dataset.state = "error"; text.textContent = t("status.offline"); return; }
  const busy = s.jobs.running > 0;
  box.dataset.state = busy ? "busy" : s.loaded ? "loaded" : "idle";
  text.textContent = `${busy ? t("status.working") : s.loaded ? t("status.loaded") : t("status.notLoaded")} · ${s.backend}`;
  box.title = `${s.backend} — ${s.model}`;
  $("#version").textContent = `v${s.version}`;
  $("#unload").disabled = !s.loaded || busy;
  $("#queue-info").textContent = (s.jobs.running || s.jobs.queued) ? t("jobs.queueInfo", s.jobs) : "";
  $("#lib-folder").textContent = t("lib.folder", { dir: s.output_dir });
  updateDurationPlaceholder();
}
async function refreshStatus() {
  try { state.status = await api("/api/status"); }
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
function currentType() { return form().audio_type.value; }
function updateDurationPlaceholder() {
  const d = state.status && state.status.default_seconds;
  $("#duration_seconds").placeholder = d ? String(d[currentType()]) : "";
  if (state.status) $("#duration_seconds").max = String(state.status.max_seconds);
}
function updateTypeUI() {
  const type = currentType(), sfx = type === "sfx";
  $("#sfx-fields").hidden = !sfx;
  $("#bpm-field").hidden = sfx;
  $("#prompt-label").textContent = t(`gen.prompt.${type}`);
  $("#prompt-hint").textContent = t(`gen.promptHint.${type}`);
  $("#prompt").placeholder = t(`gen.promptPh.${type}`);
  updateDurationPlaceholder();
}

const NUMBER_FIELDS = { duration_seconds: parseFloat, bpm: parseInt, seed: parseInt, cfg: parseFloat, steps: parseInt, peak_ceiling_db: parseFloat };
function readForm() {
  const f = form(), type = currentType();
  const args = { audio_type: type, prompt: f.prompt.value.trim(), variations: parseInt(f.variations.value, 10) };
  if (type === "sfx") {
    for (const k of ["sound_source", "sound_action", "sound_material", "sound_space"]) {
      const v = f[k].value.trim();
      if (v) args[k] = v;
    }
  }
  for (const [k, parse] of Object.entries(NUMBER_FIELDS)) {
    if (k === "bpm" && type === "sfx") continue;
    const raw = f[k].value.trim();
    if (raw !== "") args[k] = parse(raw, 10);
  }
  const title = f.title.value.trim();
  if (title) args.title = title;
  const neg = f.negative_prompt.value.trim();
  if (neg) args.negative_prompt = neg;
  return args;
}
function fillForm(meta) {
  const f = form();
  f.reset();
  const type = ["music", "instrument", "sfx"].includes(meta.audio_type) ? meta.audio_type : "music";
  f.audio_type.value = type;
  f.prompt.value = meta.prompt || "";
  const sf = meta.sound_fields || {};
  f.sound_source.value = sf.source || ""; f.sound_action.value = sf.action || "";
  f.sound_material.value = sf.material || ""; f.sound_space.value = sf.space || "";
  f.duration_seconds.value = meta.seconds ?? "";
  f.bpm.value = meta.bpm ?? "";
  f.seed.value = meta.seed ?? "";
  f.cfg.value = meta.cfg != null && meta.cfg !== 1 ? meta.cfg : "";
  f.steps.value = meta.steps != null && meta.steps !== 8 ? meta.steps : "";
  f.negative_prompt.value = meta.negative_prompt || "";
  if (f.cfg.value || f.steps.value || f.negative_prompt.value) $("#advanced").open = true;
  updateTypeUI();
}
function clearErrors() {
  $("#form-error").hidden = true;
  form().querySelectorAll("[aria-invalid]").forEach((n) => n.removeAttribute("aria-invalid"));
}
function showFormError(msg, fields) {
  const box = $("#form-error");
  box.textContent = msg; box.hidden = false;
  for (const f of fields || []) {
    const input = form().elements[f.field];
    if (input && input.setAttribute) {
      input.setAttribute("aria-invalid", "true");
      if (["seed", "cfg", "steps", "peak_ceiling_db", "negative_prompt"].includes(f.field)) $("#advanced").open = true;
    }
  }
}
async function submitJob(args) {
  clearErrors();
  const hasText = args.prompt || args.sound_source || args.sound_action || args.sound_material || args.sound_space;
  if (!hasText) { showFormError(t("err.emptyPrompt")); $("#prompt").setAttribute("aria-invalid", "true"); return null; }
  $("#gen-submit").disabled = true;
  try {
    const job = await api("/api/jobs", { json: { tool: "generate_audio", arguments: args } });
    state.jobs.set(job.id, job);
    renderJobs();
    pollJobs();
    refreshStatus();
    return job;
  } catch (e) {
    showFormError(e.message, e.body && e.body.fields);
    return null;
  } finally {
    $("#gen-submit").disabled = false;
  }
}
function initForm() {
  form().addEventListener("change", (e) => { if (e.target.name === "audio_type") updateTypeUI(); });
  form().addEventListener("input", (e) => e.target.removeAttribute && e.target.removeAttribute("aria-invalid"));
  form().addEventListener("submit", (e) => { e.preventDefault(); submitJob(readForm()); });
  form().addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form().requestSubmit(); }
  });
  $("#gen-reset").addEventListener("click", () => { form().reset(); clearErrors(); updateTypeUI(); });
  $("#unload").addEventListener("click", unloadModel);
}

// ── jobs ─────────────────────────────────────────────────────────────────────
function jobStateText(job) {
  if (job.status === "queued") return t("jobs.queued", { n: job.position ?? 1 });
  if (job.status === "running") return t("jobs.running", { t: fmtTime(job.elapsed) });
  if (job.status === "done") return t("jobs.done", { t: fmtTime(job.elapsed) });
  return t("jobs.error");
}
function promptLabel(a) {
  const parts = [a.prompt, a.sound_source, a.sound_action, a.sound_material, a.sound_space].filter(Boolean);
  return parts.join(", ") || "—";
}
function candidateCard(c, i) {
  const file = baseName(c.path);
  return el("div", { class: "cand" },
    el("div", { class: "cand-head" },
      el("strong", { text: t("cand.label", { n: i + 1 }) }),
      el("span", { class: "muted", text: t("cand.seed", { seed: c.seed }) })),
    el("audio", { controls: true, preload: "metadata", src: audioUrl(file) }),
    el("div", { class: "cand-actions" },
      el("a", { class: "link-btn", href: audioUrl(file, true), download: file, text: t("act.download") }),
      el("button", { class: "link-btn", type: "button", text: t("act.copyPath"), onclick: () => copyText(c.path) }),
      el("button", { class: "link-btn", type: "button", text: t("act.useSeed"),
        onclick: () => { form().seed.value = c.seed; $("#advanced").open = true; toast(t("toast.seedSet", { seed: c.seed })); } })));
}
function jobCard(job) {
  const a = job.arguments || {};
  const active = job.status === "queued" || job.status === "running";
  const card = el("article", { class: "panel job", dataset: { status: job.status, id: job.id } },
    el("div", { class: "job-head" },
      el("span", { class: "badge", text: t(`type.${a.audio_type || "music"}`) }),
      el("span", { class: "job-prompt", title: promptLabel(a), text: promptLabel(a) }),
      el("span", { class: "job-state" }, active ? el("span", { class: "spinner", "aria-hidden": "true" }) : null,
        el("span", { class: "job-state-text", text: jobStateText(job) }))));
  if (job.status === "running") card.append(el("div", { class: "progress", "aria-hidden": "true" }, el("div")));
  if (job.status === "error") card.append(el("p", { class: "job-error", text: job.error || "" }));
  if (job.status === "done" && job.result && Array.isArray(job.result.candidates)) {
    card.append(el("div", { class: "candidates" }, job.result.candidates.map(candidateCard)));
  }
  return card;
}
function renderJobs() {
  const box = $("#jobs");
  const list = [...state.jobs.values()].sort((x, y) => y.created - x.created);
  $("#jobs-empty").hidden = list.length > 0;
  // Keep finished cards (their <audio> may be playing); rebuild only cards whose status changed.
  const existing = new Map([...box.children].map((n) => [n.dataset.id, n]));
  const nodes = list.map((job) => {
    const old = existing.get(job.id);
    if (old && old.dataset.status === job.status && old.dataset.lang === state.lang) {
      const st = old.querySelector(".job-state-text");
      if (st) st.textContent = jobStateText(job);
      return old;
    }
    const card = jobCard(job);
    card.dataset.lang = state.lang;
    return card;
  });
  nodes.forEach((n, i) => { if (box.children[i] !== n) box.insertBefore(n, box.children[i] || null); });
  while (box.children.length > nodes.length) box.lastChild.remove();
}
async function pollJobs() {
  if (state.polling) return;
  state.polling = true;
  try {
    while ([...state.jobs.values()].some((j) => j.status === "queued" || j.status === "running")) {
      let finished = false;
      try {
        const r = await api("/api/jobs");
        for (const j of r.items) {
          const prev = state.jobs.get(j.id);
          if (prev && prev.status !== j.status && (j.status === "done" || j.status === "error")) finished = true;
          state.jobs.set(j.id, j);
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
    for (const j of r.items) state.jobs.set(j.id, j);
    renderJobs();
    pollJobs();
  } catch { /* offline */ }
}

// ── library ──────────────────────────────────────────────────────────────────
function metaRows(item) {
  const m = item.meta;
  if (!m) return [[t("meta.path"), item.path], ["", t("meta.noSidecar")]];
  const sf = m.sound_fields && Object.values(m.sound_fields).filter(Boolean).join(" / ");
  const kind = String(m.kind || "");
  const rows = [
    [t("meta.kind"), kind.startsWith("edit") ? `${t("kind.edit")} (${kind.split(":")[1] || ""})` : (I18N.en[`kind.${kind}`] ? t(`kind.${kind}`) : kind)],
    [t("meta.type"), m.audio_type && t(`type.${m.audio_type}`)],
    [t("meta.prompt"), m.prompt], [t("meta.sound"), sf], [t("meta.promptUsed"), m.prompt_used],
    [t("meta.idea"), m.original_idea],
    [t("meta.seconds"), fmtLen(typeof m.seconds === "number" ? m.seconds : item.seconds)],
    [t("meta.bpm"), m.bpm], [t("meta.seed"), m.seed], [t("meta.cfg"), m.cfg], [t("meta.steps"), m.steps],
    [t("meta.negative"), m.negative_prompt],
    [t("meta.peak"), m.peak_dbfs != null ? `${m.peak_dbfs} dBFS` : null],
    [t("meta.model"), m.model ? `${m.model} (${m.backend})` : null],
    [t("meta.input"), m.input_audio], [t("meta.created"), m.created], [t("meta.path"), item.path],
  ];
  return rows.filter(([, v]) => v !== null && v !== undefined && v !== "");
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
  const canRegen = m.kind === "generate" && m.seed != null;
  const details = el("dl", { class: "item-details", hidden: true },
    metaRows(item).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: String(v) })]));
  const toggle = el("button", { class: "link-btn", type: "button", "aria-expanded": "false", text: t("act.details") });
  toggle.addEventListener("click", () => {
    details.hidden = !details.hidden;
    toggle.setAttribute("aria-expanded", String(!details.hidden));
    toggle.textContent = details.hidden ? t("act.details") : t("act.hideDetails");
  });
  const kind = String(m.kind || "");
  const badge = m.audio_type ? t(`type.${m.audio_type}`) : kind === "process" ? t("kind.process") : item.format.toUpperCase();
  return el("article", { class: "panel item" },
    el("div", { class: "item-main" },
      el("div", { class: "item-title", title: item.file, text: title }),
      el("div", { class: "item-meta" },
        el("span", { class: "badge", text: badge }),
        kind.startsWith("edit") ? el("span", { class: "badge", text: t("kind.edit") }) : null,
        el("span", { text: fmtLen(item.seconds) }),
        el("span", { text: fmtDate(item.mtime) }),
        m.seed != null ? el("span", { text: t("cand.seed", { seed: m.seed }) }) : null),
      m.prompt || m.sound_fields ? el("div", { class: "item-prompt", text: promptLabel({ prompt: m.prompt, ...prefixSound(m.sound_fields) }) }) : null),
    el("div", { class: "item-side" },
      el("audio", { controls: true, preload: "none", src: audioUrl(item.file) }),
      el("div", { class: "item-actions" },
        toggle,
        el("button", { class: "link-btn", type: "button", disabled: !canRegen, text: t("act.regen"), onclick: () => regenerate(item) }),
        el("button", { class: "link-btn", type: "button", text: t("act.copyPath"), onclick: () => copyText(item.path) }),
        el("a", { class: "link-btn", href: audioUrl(item.file, true), download: item.file, text: t("act.download") }))),
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
  $("#lib-empty").textContent = L.query ? t("lib.noMatch") : t("lib.empty");
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
}

// ── boot ─────────────────────────────────────────────────────────────────────
initSettings();
initForm();
initLibrary();
applyI18n();
initTabs();
refreshStatus();
restoreJobs();
setInterval(refreshStatus, 5000);
