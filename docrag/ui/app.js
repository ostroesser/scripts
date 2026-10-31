"use strict";
// docrag web UI: no framework, no network beyond this local server.

const $ = (id) => document.getElementById(id);
const KIND_NAME = { docs: "Documentation", chat: "Mattermost" };
const ALL = { docs: "docs:*", chat: "chat:*" };
const STORE_KEY = "docrag.filters";

const state = {
  sources: [],          // [{key, kind, group, label, count}]
  selected: [],         // source keys, in the order they were picked
  results: [],          // last search results
  header: "",
  off: new Set(),       // indexes of unticked results
  queries: [],          // queries the last search ran
  notices: [],          // what the server has to say about them
  brief: "",            // prompt template, from the server
  autoKeywords: false,  // the Match switch is on only because queries were entered
};
const MAX_QUERIES = 8;  // MAX_QUERIES in docrag.py; only for the button label, the server applies it

// ------------------------------------------------------------------ helpers
const fmtK = (n) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k" : String(n));
const tokens = (chars) => Math.round(chars / 4);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
function fold(s) {
  // accent- and case-insensitive form, with a map back to positions in the original string
  let out = "", map = [];
  for (let i = 0; i < s.length; i++) {
    const f = s[i].normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
    for (const ch of f) { out += ch; map.push(i); }
  }
  return { out, map };
}
function loadSaved() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; } catch { return {}; }
}
function save(obj) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(obj)); } catch { /* private window */ }
}

// ------------------------------------------------------------------ source combobox
const combo = {
  input: $("combo-input"), list: $("combo-list"), box: $("combo-box"), chips: $("chips"),
  options: [], active: -1,

  open() {
    if (!this.list.hidden) return;
    this.list.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
    this.render();
  },
  close() {
    this.list.hidden = true;
    this.input.setAttribute("aria-expanded", "false");
    this.active = -1;
    this.input.removeAttribute("aria-activedescendant");
  },
  covered(src) { return state.selected.includes(ALL[src.kind]) && !src.key.endsWith(":*"); },

  matches(src, words) {
    const hay = fold(`${src.label} ${src.group}`).out;
    return words.every((w) => hay.includes(w));
  },
  highlight(text, words) {
    const { out, map } = fold(text);
    const marks = new Array(text.length).fill(false);
    for (const w of words) {
      let i = out.indexOf(w);
      while (w && i !== -1) {
        for (let j = i; j < i + w.length; j++) marks[map[j]] = true;
        i = out.indexOf(w, i + w.length);
      }
    }
    const frag = document.createDocumentFragment();
    let buf = "", on = false;
    const flush = () => {
      if (!buf) return;
      frag.append(on ? Object.assign(el("mark"), { textContent: buf }) : document.createTextNode(buf));
      buf = "";
    };
    for (let i = 0; i < text.length; i++) {
      if (marks[i] !== on) { flush(); on = marks[i]; }
      buf += text[i];
    }
    flush();
    return frag;
  },

  render() {
    const words = fold(this.input.value.trim()).out.split(/\s+/).filter(Boolean);
    this.list.replaceChildren();
    this.options = [];
    for (const kind of ["docs", "chat"]) {
      const items = state.sources.filter((s) => s.kind === kind && this.matches(s, words));
      if (!items.length) continue;
      this.list.append(Object.assign(el("li", "combo-group", KIND_NAME[kind]), { role: "presentation" }));
      if (!words.length) {
        const total = state.sources.filter((s) => s.kind === kind).reduce((a, s) => a + s.count, 0);
        items.unshift({ key: ALL[kind], kind, group: "", count: total,
                        label: kind === "docs" ? "All documentation" : "All Mattermost channels" });
      }
      for (const src of items) {
        const li = el("li", `combo-option ${kind}${src.key.endsWith(":*") ? " all" : ""}`);
        li.id = `opt-${this.options.length}`;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", String(state.selected.includes(src.key)));
        if (this.covered(src)) li.classList.add("covered");
        li.append(el("span", "check", "✓"));
        const name = el("span", "name");
        name.append(this.highlight(src.label, words));
        if (kind === "chat" && src.group && src.group !== src.label) {
          const sub = el("span", "sub");
          sub.append(this.highlight(src.group, words));
          name.append(sub);
        }
        li.append(name, el("span", "count", `${src.count.toLocaleString()} ${kind === "docs" ? "pages" : "threads"}`));
        li.addEventListener("mousedown", (e) => { e.preventDefault(); this.toggle(src.key); });
        this.list.append(li);
        this.options.push({ li, key: src.key });
      }
    }
    if (!this.options.length) {
      this.list.append(el("li", "combo-empty", `No source matches “${this.input.value.trim()}”`));
    }
    this.setActive(Math.min(Math.max(this.active, 0), this.options.length - 1));
  },
  setActive(i) {
    this.options.forEach((o, j) => o.li.classList.toggle("active", j === i));
    this.active = i;
    if (i >= 0 && this.options[i]) {
      this.options[i].li.scrollIntoView({ block: "nearest" });
      this.input.setAttribute("aria-activedescendant", this.options[i].li.id);
    }
  },
  toggle(key) {
    if (state.selected.includes(key)) {
      state.selected = state.selected.filter((k) => k !== key);
    } else {
      const kind = key.split(":")[0];
      // "All …" replaces the individual sources of that kind, and the other way round
      state.selected = key.endsWith(":*")
        ? state.selected.filter((k) => !k.startsWith(kind + ":"))
        : state.selected.filter((k) => k !== ALL[kind]);
      state.selected.push(key);
    }
    this.input.value = "";
    this.renderChips();
    this.render();
    persist();
  },
  renderChips() {
    this.chips.replaceChildren();
    for (const key of state.selected) {
      const kind = key.split(":")[0];
      const src = state.sources.find((s) => s.key === key);
      const label = key.endsWith(":*") ? (kind === "docs" ? "All documentation" : "All Mattermost")
                                        : (src ? src.label : key.slice(kind.length + 1));
      const chip = el("span", `chip ${kind}`);
      chip.title = KIND_NAME[kind] + ": " + label;
      const x = el("button", null, "×");
      x.type = "button";
      x.setAttribute("aria-label", `Remove ${label}`);
      x.addEventListener("click", (e) => { e.stopPropagation(); this.toggle(key); this.input.focus(); });
      chip.append(el("span", null, label), x);
      this.chips.append(chip);
    }
    this.input.placeholder = state.selected.length ? "Add a source…" : "All sources";
  },
  bind() {
    this.box.addEventListener("click", () => { this.input.focus(); this.open(); });
    $("combo-toggle").addEventListener("click", (e) => {
      e.stopPropagation();
      if (this.list.hidden) { this.input.focus(); this.open(); } else this.close();
    });
    this.input.addEventListener("input", () => { this.open(); this.active = 0; this.render(); });
    this.input.addEventListener("focus", () => this.open());
    this.input.addEventListener("blur", () => setTimeout(() => this.close(), 120));
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        this.open();
        const n = this.options.length;
        if (n) this.setActive((this.active + (e.key === "ArrowDown" ? 1 : -1) + n) % n);
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (!this.list.hidden && this.options[this.active]) this.toggle(this.options[this.active].key);
      } else if (e.key === "Escape") {
        this.close();
      } else if (e.key === "Backspace" && !this.input.value && state.selected.length) {
        this.toggle(state.selected[state.selected.length - 1]);
      }
    });
  },
};

// ------------------------------------------------------------------ filters
const q = $("q"), queries = $("queries"), since = $("since"), k = $("k"), size = $("size"), rerank = $("rerank"),
      keywords = $("keywords");

function autoGrow() {
  q.style.height = "auto"; q.style.height = q.scrollHeight + "px";
  queries.style.height = "auto"; queries.style.height = queries.scrollHeight + 2 + "px";   // + its border
}
function queryLines() {
  // same cleaning as clean_queries() in docrag.py, without the cap
  const seen = new Set();
  return queries.value.split("\n").map((l) => l.trim().replace(/\s+/g, " ")).filter((l) => {
    const key = l.toLowerCase();
    if (!l || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function showQueries() {
  const n = Math.min(queryLines().length, MAX_QUERIES);
  $("go").textContent = n ? `Search ${n} quer${n === 1 ? "y" : "ies"}` : "Search";
  $("queries-clear").hidden = !queries.value;
}
function queriesChanged() {
  // Short queries with exact names need keyword matching: on while there are queries,
  // back off with them if that's the only reason it was on. The switch still has the last word.
  const has = queryLines().length > 0;
  if (has && !keywords.checked) {
    keywords.checked = true;
    state.autoKeywords = true;
  } else if (!has && state.autoKeywords) {
    keywords.checked = false;
    state.autoKeywords = false;
  }
  showKeywords();
  showQueries();
  autoGrow();
  persist();
}
function showSize() {
  const v = +size.value;
  $("size-out").textContent = `${fmtK(v)} characters, about ${fmtK(tokens(v))} tokens`;
}
function persist() {
  save({ src: state.selected, since: since.value, k: k.value, size: size.value, rerank: rerank.checked,
         keywords: keywords.checked, autoKeywords: state.autoKeywords,
         // saved as a pair, so that queries never come back without the question they were written for
         queries: queries.value, question: queries.value ? q.value : "" });
}
function showRerank() {
  $("rerank-text").textContent = rerank.checked ? "Precise" : "Fast";
}
function showKeywords() {
  $("keywords-text").textContent = keywords.checked ? "Meaning + keywords" : "Meaning";
}
function clampK() { k.value = Math.max(1, Math.min(50, parseInt(k.value, 10) || 5)); }

// ------------------------------------------------------------------ search
async function search() {
  const question = q.value.trim();
  if (!question) { q.focus(); return; }
  clampK();
  const params = new URLSearchParams({ q: question, k: k.value, size: size.value, rerank: rerank.checked ? "1" : "0",
                                      keywords: keywords.checked ? "1" : "0" });
  queryLines().forEach((line) => params.append("query", line));
  if (since.value) params.set("since", since.value);
  state.selected.forEach((s) => params.append("src", s));
  history.replaceState(null, "", "?" + params.toString());
  persist();

  const go = $("go");
  go.disabled = true;
  go.textContent = "Searching…";
  const slow = setTimeout(() => notice(rerank.checked
    ? "Searching and re-reading the best matches. The first search also loads the models."
    : "Searching. The first search also loads the model."), 1500);
  try {
    const r = await fetch("/api/search?" + params.toString());
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    state.results = data.results;
    state.header = data.header;
    state.seconds = data.seconds;
    state.reranked = data.reranked;
    state.warning = data.warning;
    state.queries = data.queries;
    state.notices = data.notices;
    state.off = new Set();
    renderResults();
  } catch (e) {
    state.results = [];
    state.queries = [];
    state.notices = [];
    renderResults();
    notice(`Search failed: ${e.message}. Check the terminal running docrag serve.`, true);
  } finally {
    clearTimeout(slow);
    go.disabled = false;
    showQueries();
  }
}

function notice(text, isError = false) {
  const n = $("notice");
  n.hidden = !text;
  n.textContent = text || "";
  n.classList.toggle("error", isError);
}

function displayTitle(r) {
  // chat titles are "Channel · YYYY-MM-DD · first words": channel and date are already shown
  if (r.kind === "chat") {
    const m = r.title.match(/^.*? · \d{4}-\d{2}-\d{2} · (.+)$/);
    if (m) return m[1];
  }
  return r.title;
}

function renderResults() {
  const list = $("results");
  list.replaceChildren();
  notice([state.warning && `Results without precise ranking: ${state.warning}`, ...state.notices].filter(Boolean).join(" "),
         !!state.warning);
  $("packet").hidden = !state.results.length;
  renderLegend();
  if (!state.results.length) {
    if (q.value.trim()) notice("No pages match. Remove a source, or move the chat date further back.");
    return;
  }
  state.results.forEach((r, i) => {
    const li = el("li", `result ${r.kind}`);
    const box = Object.assign(el("input"), { type: "checkbox", checked: true });
    box.setAttribute("aria-label", `Include “${r.title}” in the copy`);
    box.addEventListener("change", () => {
      box.checked ? state.off.delete(i) : state.off.add(i);
      li.classList.toggle("off", !box.checked);
      renderPacket();
    });

    const head = el("div", "result-head");
    const top = el("div", "result-top");
    top.append(el("div", "result-source", `${KIND_NAME[r.kind]}: ${r.label}`));
    if (state.queries.length > 1) top.append(foundBy(r));
    head.append(top, el("h2", "result-title", displayTitle(r)));
    const meta = el("div", "result-meta");
    if (r.date) meta.append(el("span", null, r.kind === "chat" ? `Last reply ${r.date}` : r.date));
    meta.append(el("span", null, `${r.whole ? "Whole page" : "Relevant extracts"}, ${fmtK(r.chars)} characters`));
    if (r.url) {
      const a = Object.assign(el("a", null, r.kind === "chat" ? "Open in Mattermost" : "Open page"),
                              { href: r.url, target: "_blank", rel: "noopener" });
      meta.append(a);
    }
    const toggle = Object.assign(el("button", "linkish", "Preview"), { type: "button" });
    toggle.setAttribute("aria-expanded", "false");
    let pre = null;
    toggle.addEventListener("click", () => {
      if (pre) { pre.remove(); pre = null; toggle.textContent = "Preview"; toggle.setAttribute("aria-expanded", "false"); return; }
      pre = el("pre", "preview", r.text);
      li.append(pre);
      toggle.textContent = "Hide preview";
      toggle.setAttribute("aria-expanded", "true");
    });
    meta.append(toggle);
    head.append(meta);
    li.append(box, head);
    list.append(li);
  });
  renderPacket();
}

function foundBy(r) {
  const box = el("div", "badges");
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", `Found by quer${r.found_by.length === 1 ? "y" : "ies"} ${r.found_by.map((n) => n + 1).join(", ")}`);
  for (const n of r.found_by) {
    const b = el("span", "badge", String(n + 1));
    b.title = `Found by query ${n + 1}: ${state.queries[n]}`;
    box.append(b);
  }
  return box;
}

function renderLegend() {
  const legend = $("query-legend");
  legend.replaceChildren();
  legend.hidden = state.queries.length < 2;
  if (legend.hidden) return;
  const used = new Set(state.results.flatMap((r) => r.found_by));
  state.queries.forEach((text, n) => {
    const li = el("li", used.has(n) ? "" : "unused");
    li.append(el("span", "badge", String(n + 1)), el("span", null, text));
    legend.append(li);
  });
}

function packetText() {
  const blocks = state.results
    .filter((_, i) => !state.off.has(i))
    .map((r, n) => r.block.replace(/^---\n## Page \d+:/, `---\n## Page ${n + 1}:`));
  return [state.header, ...blocks].join("\n");
}

function renderPacket() {
  const budget = +size.value;
  const kept = state.results.filter((_, i) => !state.off.has(i));
  const total = packetText().length;
  const meter = $("meter");
  meter.replaceChildren();
  for (const r of kept) {
    const seg = el("i", r.kind);
    seg.style.width = `${Math.min(100, (r.block.length / Math.max(budget, total)) * 100)}%`;
    seg.title = `${r.title} (${fmtK(r.chars)})`;
    meter.append(seg);
  }
  const t = $("packet-text");
  t.replaceChildren();
  const strong = el("strong", null, `${kept.length} of ${state.results.length} pages`);
  t.append(strong, document.createTextNode(
    `, ${fmtK(total)} characters, about ${fmtK(tokens(total))} tokens (size ${fmtK(budget)})`
    + (state.seconds != null ? `. Found in ${state.seconds.toFixed(1)} s`
       + (state.queries.length > 1 ? ` with ${state.queries.length} queries` : "")
       + `${state.reranked ? (state.queries.length > 1 ? " and" : " with") + " precise ranking" : ""}.` : "")));
  $("copy").disabled = !kept.length;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(el("textarea"), { value: text });
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

function flashCopied(b) {
  const label = b.dataset.label || (b.dataset.label = b.textContent);
  b.textContent = "Copied";
  b.classList.add("btn-done");
  clearTimeout(b.flash);
  b.flash = setTimeout(() => { b.textContent = label; b.classList.remove("btn-done"); }, 1800);
}

async function copyPacket() {
  if (!state.results.length) return;
  await copyText(packetText());
  flashCopied($("copy"));
}

async function copyBrief() {
  const question = q.value.trim();
  if (!question) { q.focus(); notice("Type your question first: the brief includes it."); return; }
  if (!state.brief) { notice("Couldn't load the brief. Is docrag serve still running?", true); return; }
  await copyText(state.brief.replaceAll("{question}", () => question));
  flashCopied($("brief"));
}

// ------------------------------------------------------------------ start
async function init() {
  combo.bind();
  const saved = loadSaved();
  const url = new URLSearchParams(location.search);
  // A URL holding a search wins entirely: an old bookmark must not pick up saved queries.
  const fromUrl = url.has("q") || url.has("query");
  q.value = fromUrl ? url.get("q") || "" : saved.question || "";
  queries.value = fromUrl ? url.getAll("query").join("\n") : saved.queries || "";
  state.autoKeywords = !fromUrl && saved.autoKeywords === true;
  since.value = url.get("since") ?? saved.since ?? "";
  k.value = url.get("k") || saved.k || 5;
  size.value = url.get("size") || saved.size || 24000;
  rerank.checked = url.has("rerank") ? url.get("rerank") !== "0" : saved.rerank !== false;
  showRerank();
  keywords.checked = url.has("keywords") ? url.get("keywords") === "1"
    : url.has("query") || saved.keywords === true;
  showKeywords();
  state.selected = url.getAll("src").length ? url.getAll("src") : (saved.src || []);
  autoGrow();
  showSize();
  showQueries();

  q.addEventListener("input", () => { autoGrow(); persist(); });
  queries.addEventListener("input", queriesChanged);
  for (const t of [q, queries]) {
    t.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); search(); }
    });
  }
  $("queries-clear").addEventListener("click", () => { queries.value = ""; queriesChanged(); queries.focus(); });
  $("brief").addEventListener("click", copyBrief);
  $("ask").addEventListener("submit", (e) => { e.preventDefault(); search(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); copyPacket(); }
  });
  $("copy").addEventListener("click", copyPacket);
  document.querySelectorAll(".step").forEach((b) =>
    b.addEventListener("click", () => { k.value = (parseInt(k.value, 10) || 5) + +b.dataset.step; clampK(); persist(); }));
  k.addEventListener("change", () => { clampK(); persist(); });
  since.addEventListener("change", persist);
  size.addEventListener("input", () => { showSize(); if (state.results.length) renderPacket(); });
  size.addEventListener("change", persist);
  rerank.addEventListener("change", () => { showRerank(); persist(); });
  keywords.addEventListener("change", () => { state.autoKeywords = false; showKeywords(); persist(); });

  try {
    const r = await fetch("/api/sources");
    const data = await r.json();
    state.sources = data.sources;
    state.brief = data.brief_template || "";
    if (!data.reranker) $("rerank-field").hidden = true;   // server started with --no-rerank
    const known = new Set(state.sources.map((s) => s.key).concat(Object.values(ALL)));
    state.selected = state.selected.filter((key) => known.has(key));
    const nDocs = state.sources.filter((s) => s.kind === "docs").length;
    const nChat = state.sources.filter((s) => s.kind === "chat").length;
    $("stats").textContent = `${data.pages.toLocaleString()} pages indexed from ${nDocs} doc${nDocs === 1 ? "" : "s"}`
      + ` and ${nChat} channel${nChat === 1 ? "" : "s"}`;
  } catch {
    $("stats").textContent = "Couldn't load the index. Is docrag serve still running?";
  }
  combo.renderChips();
  if (fromUrl && q.value) search(); else q.focus();
}
init();
