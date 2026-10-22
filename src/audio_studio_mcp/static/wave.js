// Waveform drawing, peaks loading and the shared player for result / library
// cards. One <audio> element plays every card, so starting one sound stops the
// previous one; the editor has its own element and pauses this one (and back).
"use strict";

const Peaks = {
  cache: new Map(),  // key -> Promise of the /api/peaks answer
  get(file, n, start, end, version) {
    const q = `n=${n}` + (start != null ? `&start=${start.toFixed(4)}&end=${end.toFixed(4)}` : "") + (version ? `&v=${version}` : "");
    const key = `${file}?${q}`;
    if (!this.cache.has(key)) {
      if (this.cache.size > 200) this.cache.clear();
      const p = fetch(`/api/peaks/${encodeURIComponent(file)}?${q}`).then((r) => {
        if (!r.ok) throw new Error(`peaks ${r.status}`);
        return r.json();
      });
      p.catch(() => this.cache.delete(key));
      this.cache.set(key, p);
    }
    return this.cache.get(key);
  },
};

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// Size a canvas to its CSS box at device resolution; returns the 2D context.
function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1, r = cv.getBoundingClientRect();
  const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  return cv.getContext("2d");
}

// Draw min/max columns. `split` (0..1) colours the part left of it as played.
function drawPeaks(ctx, mins, maxs, { color, played, split = 0, x0 = 0, x1 = ctx.canvas.width, gain = 1 }) {
  const H = ctx.canvas.height, mid = H / 2, n = mins.length, w = x1 - x0;
  if (!n || w <= 0) return;
  const amp = (H / 2 - 1) * gain;
  const splitX = x0 + split * w;
  for (let x = Math.floor(x0); x < x1; x++) {
    const i0 = Math.floor(((x - x0) / w) * n), i1 = Math.max(i0 + 1, Math.floor(((x + 1 - x0) / w) * n));
    let lo = 1, hi = -1;
    for (let i = i0; i < i1 && i < n; i++) { if (mins[i] < lo) lo = mins[i]; if (maxs[i] > hi) hi = maxs[i]; }
    if (hi < lo) continue;
    const top = mid - hi * amp, bot = mid - lo * amp;
    ctx.fillStyle = played && x < splitX ? played : color;
    ctx.fillRect(x, top, 1, Math.max(1, bot - top));
  }
}

// ── shared card player ─────────────────────────────────────────────────────
const Player = {
  audio: new Audio(),
  file: null,
  owner: null,        // the MiniWave currently loaded
  prev: null,         // the one before (to redraw it as stopped)
  listeners: new Set(),
  init() {
    const a = this.audio;
    a.preload = "auto";
    const tick = () => this.emit(false);
    for (const ev of ["play", "pause", "ended", "timeupdate", "loadedmetadata", "seeked"]) a.addEventListener(ev, tick);
    a.addEventListener("play", () => { this.raf(); window.dispatchEvent(new CustomEvent("as:play", { detail: "cards" })); });
    window.addEventListener("as:play", (e) => { if (e.detail !== "cards") a.pause(); });
    let rt;  // redraw thumbnails after a resize or theme change
    const redraw = () => { clearTimeout(rt); rt = setTimeout(() => this.emit(true), 100); };
    window.addEventListener("resize", redraw);
    window.addEventListener("as:theme", redraw);
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  },
  raf() {
    if (this.audio.paused) return;
    this.emit(false);
    requestAnimationFrame(() => this.raf());
  },
  // all = every thumbnail (resize, theme); otherwise only the playing one and the previous one
  emit(all) {
    if (all) { for (const m of [...this.listeners]) m.render(); return; }
    for (const m of [this.owner, this.prev]) if (m) m.render();
  },
  async play(mini, at) {
    const a = this.audio;
    if (this.file !== mini.file) {
      this.file = mini.file;
      a.src = audioUrl(mini.file);
    }
    if (this.owner !== mini) { this.prev = this.owner; this.owner = mini; }
    if (at != null) {
      if (!(await whenReady(a)) || this.owner !== mini) return;
      a.currentTime = Math.max(0, Math.min(at, (a.duration || at) - 0.01));
    }
    try { await a.play(); } catch { /* interrupted by another play() */ }
    this.emit(false);
  },
  pause() { this.audio.pause(); },
  isPlaying(mini) { return this.owner === mini && !this.audio.paused; },
  time(mini) { return this.owner === mini ? this.audio.currentTime : 0; },
};

// Compact player: play button + waveform thumbnail (click / arrow keys seek) + time.
class MiniWave {
  constructor(file, seconds, version) {
    this.file = file;
    this.version = version;
    this.seconds = seconds || 0;
    this.peaks = null;
    this.btn = el("button", { class: "play-btn", type: "button", "aria-label": t("player.play"), onclick: () => this.toggle() });
    this.cv = el("canvas", { class: "mini-wave-canvas", "aria-hidden": "true" });
    this.track = el("div", { class: "mini-wave-track", role: "slider", tabindex: "0", "aria-label": t("player.position"),
      "aria-valuemin": "0", onpointerdown: (e) => this.seekPointer(e), onkeydown: (e) => this.key(e) }, this.cv);
    this.time = el("span", { class: "mini-time" });
    this.node = el("div", { class: "mini-wave", dataset: { file } }, this.btn, this.track, this.time);
    this.node.mini = this;
    Player.listeners.add(this);
    MiniWave.observer.observe(this.node);
    this.render();
  }
  async load() {
    if (this.peaks) return;
    try {
      this.peaks = await Peaks.get(this.file, 240, null, null, this.version);
      this.seconds = this.peaks.seconds;
    } catch { this.peaks = { min: [], max: [] }; }
    this.render();
  }
  // Called when the card is removed or re-rendered.
  dispose() {
    Player.listeners.delete(this);
    MiniWave.observer.unobserve(this.node);
  }
  toggle() { Player.isPlaying(this) ? Player.pause() : Player.play(this, Player.owner === this ? null : 0); }
  seekTo(sec) { Player.play(this, Math.max(0, Math.min(this.seconds, sec))); }
  seekPointer(e) {
    if (!this.seconds) return;
    const r = this.track.getBoundingClientRect();
    this.seekTo(((e.clientX - r.left) / r.width) * this.seconds);
  }
  key(e) {
    const step = e.shiftKey ? 5 : 1, now = Player.time(this);
    if (e.key === "ArrowRight") this.seekTo(now + step);
    else if (e.key === "ArrowLeft") this.seekTo(now - step);
    else if (e.key === "Home") this.seekTo(0);
    else if (e.key === " " || e.key === "Enter") this.toggle();
    else return;
    e.preventDefault();
  }
  render() {
    if (!this.node.isConnected) {  // card removed from the page: forget it
      if (this.attached) this.dispose();
      return this.update();
    }
    this.attached = true;
    this.update();
    if (!this.peaks) return;
    const now = Player.time(this);
    const ctx = fitCanvas(this.cv);
    ctx.clearRect(0, 0, this.cv.width, this.cv.height);
    drawPeaks(ctx, this.peaks.min, this.peaks.max, {
      color: cssVar("--wave"), played: cssVar("--wave-played"), split: this.seconds ? now / this.seconds : 0 });
  }
  // button, time and slider values (no drawing)
  update() {
    const playing = Player.isPlaying(this), now = Player.time(this);
    this.btn.classList.toggle("playing", playing);
    this.btn.setAttribute("aria-label", playing ? t("player.pause") : t("player.play"));
    const f = (x) => (this.seconds < 60 ? `${x.toFixed(1)} s` : fmtClock(x));  // short sounds: tenths
    this.time.textContent = Player.owner === this && now > 0 ? `${f(now)} / ${f(this.seconds)}` : f(this.seconds);
    this.track.setAttribute("aria-valuemax", String(Math.round(this.seconds * 10) / 10));
    this.track.setAttribute("aria-valuenow", String(Math.round(now * 10) / 10));
    this.track.setAttribute("aria-valuetext", `${fmtClock(now)} / ${fmtClock(this.seconds)}`);
  }
}
MiniWave.observer = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.mini.load(); MiniWave.observer.unobserve(e.target); }
}, { rootMargin: "200px" });

// Resolves true once the element knows its duration, false on an error or after 10 s.
function whenReady(a) {
  if (a.readyState >= 1) return Promise.resolve(true);
  return new Promise((resolve) => {
    const done = (ok) => { clearTimeout(timer); a.removeEventListener("loadedmetadata", yes); a.removeEventListener("error", no); resolve(ok); };
    const yes = () => done(true), no = () => done(false);
    const timer = setTimeout(no, 10000);
    a.addEventListener("loadedmetadata", yes);
    a.addEventListener("error", no);
  });
}
// Dispose the thumbnails inside nodes that are being removed or replaced.
function disposeMinis(root) {
  root.querySelectorAll(".mini-wave").forEach((n) => n.mini && n.mini.dispose());
}

// m:ss.t for waveform times, m:ss for card lengths
function fmtClock(sec, decimals = 0) {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60), s = sec - m * 60;
  const ss = decimals ? s.toFixed(decimals).padStart(3 + decimals, "0") : String(Math.floor(s)).padStart(2, "0");
  return `${m}:${ss}`;
}
