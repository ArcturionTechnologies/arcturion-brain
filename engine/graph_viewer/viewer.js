// Arcturion Brain Graph Viewer — Item 8 (production polish 2026-05-19).
// Renders /graph_subset into a force-directed graph with family + predicate-family
// filters, centrality threshold, click-to-query context pack, palette, screenshot,
// theme toggle, and persistent view state.

const FAMILIES = [
  "document", "code", "automation", "application",
  "agent", "event", "external", "concept",
];

// edges.family column values present in brain.sqlite (see /status top_predicates).
// 8 distinct families; 'semantic' is off by default because the
// semantically_similar_to edge alone is ~36% of the graph.
const PREDICATE_FAMILIES = [
  "code", "structure", "external", "runtime",
  "social", "lifecycle", "operational", "semantic",
];
const PREDICATE_FAMILIES_DEFAULT_ON = [
  "code", "structure", "external", "runtime",
  "social", "lifecycle", "operational",
];

function familyColor(fam) {
  const probe = getComputedStyle(document.documentElement);
  const map = {
    document: "--doc", code: "--code", automation: "--automation",
    application: "--application", agent: "--agent", event: "--event",
    external: "--external", concept: "--concept",
  };
  const k = map[fam];
  return k ? (probe.getPropertyValue(k).trim() || "#787b86") : "#787b86";
}

const STATE_KEY = "arcturion.brain-viewer.v2";
const THEME_KEY = "arcturion.brain-viewer.theme";

let graphInstance = null;
let lastData = { nodes: [], links: [] };
let frozen = false;
let stabilizedOnce = false;

function $(id) { return document.getElementById(id); }
function status(msg) { $("status").textContent = msg; }
function clearChildren(el) { while (el.firstChild) el.removeChild(el.firstChild); }

// ── State persistence ───────────────────────────────────────────────────────

function loadState() {
  try { return JSON.parse(localStorage.getItem(STATE_KEY) || "{}"); }
  catch { return {}; }
}
function saveState() {
  const state = {
    families: getSelectedFamilies(),
    predicateFamilies: getSelectedPredicateFamilies(),
    minCent: $("min-cent").value,
    limit: $("limit").value,
    search: $("search").value,
  };
  try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); }
  catch { /* quota etc. */ }
}

function loadTheme() {
  const t = localStorage.getItem(THEME_KEY);
  if (t === "light" || t === "dark") {
    document.documentElement.setAttribute("data-theme", t);
  }
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem(THEME_KEY, next); } catch {}
  if (graphInstance) {
    graphInstance.nodeColor(n => familyColor(n.family));
    if (lastData.nodes.length) graphInstance.graphData(lastData);
  }
}

// ── Filter UI ───────────────────────────────────────────────────────────────

function buildFamilyFilters(restored) {
  const container = $("family-filters");
  clearChildren(container);
  const restoredSet = restored ? new Set(restored) : null;
  for (const fam of FAMILIES) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = `fam-${fam}`;
    cb.dataset.family = fam;
    cb.checked = restoredSet ? restoredSet.has(fam) : true;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.style.background = familyColor(fam);
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" "));
    label.appendChild(badge);
    label.appendChild(document.createTextNode(fam));
    container.appendChild(label);
  }
}

function buildPredicateFilters(restored) {
  const container = $("predicate-filters");
  clearChildren(container);
  const restoredSet = restored ? new Set(restored) : null;
  const defaultSet = new Set(PREDICATE_FAMILIES_DEFAULT_ON);
  for (const pf of PREDICATE_FAMILIES) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = `pred-${pf}`;
    cb.dataset.predicateFamily = pf;
    cb.checked = restoredSet ? restoredSet.has(pf) : defaultSet.has(pf);
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + pf));
    container.appendChild(label);
  }
}

function getSelectedFamilies() {
  return FAMILIES.filter(f => {
    const el = $(`fam-${f}`); return el && el.checked;
  });
}
function getSelectedPredicateFamilies() {
  return PREDICATE_FAMILIES.filter(p => {
    const el = $(`pred-${p}`); return el && el.checked;
  });
}

function setStats(nodeCount, edgeCount) {
  const c = $("stats");
  clearChildren(c);
  const row = (label, value) => {
    const div = document.createElement("div");
    div.className = "stat";
    div.appendChild(document.createTextNode(`${label}: `));
    const b = document.createElement("b");
    b.textContent = String(value);
    div.appendChild(b);
    return div;
  };
  c.appendChild(row("nodes", nodeCount));
  c.appendChild(row("edges", edgeCount));
}

// ── Data fetch & render ─────────────────────────────────────────────────────

async function loadGraph() {
  saveState();
  status("loading…");
  stabilizedOnce = false;
  frozen = false;
  $("freeze-layout").textContent = "❄️ Freeze layout";

  const families = getSelectedFamilies().join(",");
  const predicateFamilies = getSelectedPredicateFamilies().join(",");
  const minCent = parseFloat($("min-cent").value || 0) / 1000;
  const limit = parseInt($("limit").value || 500);
  const search = $("search").value.trim();

  const params = new URLSearchParams({
    families, min_centrality: minCent, limit,
  });
  if (predicateFamilies) params.set("predicate_families", predicateFamilies);
  if (search) params.set("search", search);

  const t0 = performance.now();
  let resp;
  try {
    resp = await fetch(`/graph_subset?${params}`);
  } catch (e) {
    status(`error: ${e.message}`); return;
  }
  if (!resp.ok) { status(`HTTP ${resp.status}`); return; }
  const data = await resp.json();
  lastData = data;
  renderGraph(data);
  setStats(data.nodes.length, data.links.length);
  const ms = Math.round(performance.now() - t0);
  status(`${data.nodes.length} nodes · ${data.links.length} edges · ${ms} ms`);
}

function renderGraph(data) {
  if (!graphInstance) {
    graphInstance = ForceGraph()($("graph"))
      .nodeId("id")
      .nodeLabel(n => `${n.title || n.id} — ${n.family}`)
      .nodeColor(n => familyColor(n.family))
      .nodeVal(n => 1 + (n.centrality || 0) * 200)
      .linkSource("from")
      .linkTarget("to")
      .linkOpacity(0.3)
      .linkColor(() => getComputedStyle(document.documentElement).getPropertyValue("--border").trim() || "#2a2e3a")
      .linkDirectionalArrowLength(2)
      .linkDirectionalArrowRelPos(1)
      .backgroundColor(getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#0b0e14")
      .warmupTicks(50)
      .cooldownTime(3000)
      .cooldownTicks(Infinity)
      .onEngineStop(() => {
        if (!stabilizedOnce) {
          stabilizedOnce = true;
          status(statusTextIdle());
        }
      })
      .onNodeClick(n => openContextPanel(n));
  } else {
    graphInstance
      .backgroundColor(getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#0b0e14")
      .linkColor(() => getComputedStyle(document.documentElement).getPropertyValue("--border").trim() || "#2a2e3a");
  }
  graphInstance.graphData(data);
  setTimeout(() => graphInstance.zoomToFit(400, 60), 600);
}

function statusTextIdle() {
  return `${lastData.nodes.length} nodes · ${lastData.links.length} edges · stable`;
}

function freezeLayout() {
  if (!graphInstance) return;
  if (!frozen) {
    graphInstance.cooldownTicks(0);
    frozen = true;
    $("freeze-layout").textContent = "🔥 Resume layout";
    status(statusTextIdle() + " · frozen");
  } else {
    graphInstance.cooldownTicks(Infinity).d3ReheatSimulation();
    frozen = false;
    $("freeze-layout").textContent = "❄️ Freeze layout";
    status("simulating…");
  }
}

// ── Context panel (markdown via DOMPurify-returned DOM fragment) ────────────

async function openContextPanel(node) {
  $("panel-title").textContent = `${node.title || node.id} — ${node.family}`;
  const body = $("panel-body");
  clearChildren(body);
  const em = document.createElement("em");
  em.style.color = "var(--muted)";
  em.textContent = "loading context pack…";
  body.appendChild(em);
  $("context-panel").classList.add("open");
  try {
    const r = await fetch(`/query?topic=${encodeURIComponent(node.id)}&radius=1&budget=1500`);
    const txt = await r.text();
    renderPanelMarkdown(txt);
  } catch (e) {
    body.textContent = `error: ${e.message}`;
  }
}

function renderPanelMarkdown(md) {
  // marked.js → DOMPurify (RETURN_DOM_FRAGMENT, no innerHTML round-trip).
  let html;
  try {
    html = marked.parse(md, { gfm: true, breaks: false });
  } catch {
    $("panel-body").textContent = md;
    return;
  }
  const frag = DOMPurify.sanitize(html, {
    ADD_ATTR: ["target"],
    RETURN_DOM_FRAGMENT: true,
  });
  const body = $("panel-body");
  clearChildren(body);
  body.appendChild(frag);

  body.querySelectorAll("a").forEach(a => {
    a.addEventListener("click", ev => {
      const href = a.getAttribute("href") || "";
      const nodeId = parseNodeRef(href);
      if (nodeId) {
        ev.preventDefault();
        recenterOnNode(nodeId);
      }
    });
    const href = a.getAttribute("href") || "";
    if (/^https?:/i.test(href)) a.setAttribute("target", "_blank");
  });
}

function parseNodeRef(href) {
  if (!href) return null;
  let m = href.match(/^arcturion:\/\/node\/(.+)$/i);
  if (m) return decodeURIComponent(m[1]);
  m = href.match(/^\/?query\?topic=([^&]+)/i);
  if (m) return decodeURIComponent(m[1]);
  m = href.match(/^node:\/\/(.+)$/i);
  if (m) return decodeURIComponent(m[1]);
  if (/^[a-z0-9][a-z0-9_\-./]+$/i.test(href) && !href.startsWith("/") && !href.includes(":")) {
    if (lastData.nodes.some(n => n.id === href)) return href;
  }
  return null;
}

async function recenterOnNode(nodeId) {
  const local = lastData.nodes.find(n => n.id === nodeId);
  if (local && graphInstance) {
    graphInstance.centerAt(local.x, local.y, 600);
    graphInstance.zoom(4, 600);
    openContextPanel(local);
    return;
  }
  status("recentering…");
  try {
    const r = await fetch(`/search?text=${encodeURIComponent(nodeId)}&limit=1`);
    if (r.ok) {
      const hits = await r.json();
      if (hits.length) { openContextPanel(hits[0]); return; }
    }
  } catch {}
  status("not found in current subset");
}

// ── Command palette (⌘K) ────────────────────────────────────────────────────

let paletteCursor = 0;
let paletteMatches = [];

function fuzzyScore(needle, hay) {
  if (!needle) return 0.001;
  needle = needle.toLowerCase();
  hay = hay.toLowerCase();
  if (hay === needle) return 1000;
  if (hay.startsWith(needle)) return 800;
  let idx = 0, score = 0, lastMatch = -1;
  for (let i = 0; i < hay.length && idx < needle.length; i++) {
    if (hay[i] === needle[idx]) {
      score += (lastMatch === i - 1) ? 8 : 3;
      if (i === 0 || /[\s_\-./]/.test(hay[i - 1])) score += 5;
      lastMatch = i;
      idx++;
    }
  }
  return idx === needle.length ? score : 0;
}

function openPalette() {
  $("palette-overlay").classList.add("open");
  $("palette-input").value = "";
  $("palette-input").focus();
  paletteCursor = 0;
  renderPaletteResults("");
}
function closePalette() {
  $("palette-overlay").classList.remove("open");
}
function renderPaletteResults(query) {
  const out = $("palette-results");
  clearChildren(out);
  const scored = lastData.nodes
    .map(n => ({ n, s: fuzzyScore(query, (n.title || n.id) + " " + n.id) }))
    .filter(x => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .slice(0, 40);
  paletteMatches = scored.map(x => x.n);
  paletteCursor = Math.min(paletteCursor, Math.max(0, paletteMatches.length - 1));
  paletteMatches.forEach((node, i) => {
    const div = document.createElement("div");
    div.className = "palette-item" + (i === paletteCursor ? " active" : "");
    const title = document.createElement("span");
    title.className = "palette-item-title";
    title.textContent = node.title || node.id;
    const fam = document.createElement("small");
    fam.textContent = node.family;
    div.appendChild(title);
    div.appendChild(fam);
    div.addEventListener("click", () => {
      paletteCursor = i;
      paletteAccept();
    });
    out.appendChild(div);
  });
}
function paletteAccept() {
  if (!paletteMatches.length) { closePalette(); return; }
  const node = paletteMatches[paletteCursor];
  closePalette();
  if (graphInstance && node.x != null) {
    graphInstance.centerAt(node.x, node.y, 600);
    graphInstance.zoom(4, 600);
  }
  openContextPanel(node);
}

// ── Screenshot ──────────────────────────────────────────────────────────────

function downloadScreenshot() {
  if (!graphInstance) return;
  const canvas = document.querySelector("#graph canvas");
  if (!canvas) { status("no canvas"); return; }
  const url = canvas.toDataURL("image/png");
  const a = document.createElement("a");
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  a.href = url;
  a.download = `arcturion-brain-${today}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  status("PNG downloaded");
}

// ── Wiring ──────────────────────────────────────────────────────────────────

function resetFilters() {
  try { localStorage.removeItem(STATE_KEY); } catch {}
  buildFamilyFilters(null);
  buildPredicateFilters(null);
  $("min-cent").value = 0;
  $("limit").value = 500;
  $("search").value = "";
  wireFilterChangeHandlers();
  loadGraph();
}

function wireFilterChangeHandlers() {
  for (const f of FAMILIES) $(`fam-${f}`).addEventListener("change", loadGraph);
  for (const p of PREDICATE_FAMILIES) $(`pred-${p}`).addEventListener("change", loadGraph);
}

document.addEventListener("DOMContentLoaded", () => {
  loadTheme();
  const state = loadState();
  buildFamilyFilters(state.families);
  buildPredicateFilters(state.predicateFamilies);
  if (state.minCent != null) $("min-cent").value = state.minCent;
  if (state.limit != null) $("limit").value = state.limit;
  if (state.search != null) $("search").value = state.search;

  wireFilterChangeHandlers();
  $("reload").addEventListener("click", loadGraph);
  $("close-panel").addEventListener("click", () => $("context-panel").classList.remove("open"));
  $("reset-view").addEventListener("click", () => graphInstance && graphInstance.centerAt(0, 0, 400));
  $("fit-all").addEventListener("click", () => graphInstance && graphInstance.zoomToFit(400, 60));
  $("freeze-layout").addEventListener("click", freezeLayout);
  $("screenshot").addEventListener("click", downloadScreenshot);
  $("reset-filters").addEventListener("click", resetFilters);
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("min-cent").addEventListener("change", loadGraph);
  $("limit").addEventListener("change", loadGraph);
  $("search").addEventListener("change", loadGraph);

  document.addEventListener("keydown", ev => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      openPalette();
      return;
    }
    if ($("palette-overlay").classList.contains("open")) {
      if (ev.key === "Escape") { ev.preventDefault(); closePalette(); }
      else if (ev.key === "ArrowDown") {
        ev.preventDefault();
        paletteCursor = Math.min(paletteMatches.length - 1, paletteCursor + 1);
        renderPaletteResults($("palette-input").value);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        paletteCursor = Math.max(0, paletteCursor - 1);
        renderPaletteResults($("palette-input").value);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        paletteAccept();
      }
    }
  });
  $("palette-input").addEventListener("input", ev => {
    paletteCursor = 0;
    renderPaletteResults(ev.target.value);
  });
  $("palette-overlay").addEventListener("click", ev => {
    if (ev.target === $("palette-overlay")) closePalette();
  });

  loadGraph();
});
