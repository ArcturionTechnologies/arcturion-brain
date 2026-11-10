/* The Arcturion Brain: public galaxy exhibit.
   Draws invented records as a turning star field, labels every relationship,
   and animates how a question becomes a small context pack.
   Static and self-contained: no network, no storage, no remote assets. */
(() => {
  "use strict";
  const data = window.BRAIN_DEMO, model = window.BrainDemoModel;
  const $ = id => document.getElementById(id);

  function h(tag, attrs, kids) {
    const node = document.createElement(tag);
    if (attrs) for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "on") for (const name of Object.keys(value)) node.addEventListener(name, value[name]);
      else node.setAttribute(key, value === true ? (key.startsWith("aria-") ? "true" : "") : String(value));
    }
    if (kids != null) for (const kid of [].concat(kids)) if (kid != null) node.append(kid);
    return node;
  }
  function put(parent, ...kids) { for (const kid of kids) if (kid != null) parent.append(kid); }
  function fatal(message) {
    document.body.append(h("div", {class: "fatal", role: "alert", text: message}));
  }
  if (!data || !model) return fatal("The exhibit could not load its invented records.");
  const problems = model.validateData(data);
  if (problems.length) return fatal("The invented records failed their own checks: " + problems[0]);

  // ── Small maths ──────────────────────────────────────────
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = t => (t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t));
  const rgba = (c, a) => "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + clamp(a, 0, 1).toFixed(3) + ")";
  function hash01(text) {
    let x = 2166136261;
    for (let i = 0; i < text.length; i++) { x ^= text.charCodeAt(i); x = Math.imul(x, 16777619); }
    return ((x >>> 0) % 100000) / 100000;
  }
  function seeded(seed) {
    return () => {
      seed = (seed + 0x6D2B79F5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // ── Vocabulary ───────────────────────────────────────────
  const FAMILY = {
    agent: {label: "Agent roles", one: "Agent role", color: [255, 185, 100], lift: 95},
    document: {label: "Documents", one: "Document", color: [190, 214, 255], lift: 0},
    code: {label: "Code", one: "Code", color: [104, 226, 208], lift: -20},
    automation: {label: "Automations", one: "Automation", color: [178, 150, 255], lift: 30},
    concept: {label: "Concepts", one: "Concept", color: [255, 140, 176], lift: -70}
  };
  const PRED = {
    depends_on: [122, 178, 255], maintained_by: [255, 205, 130], cites: [178, 188, 208],
    supports: [120, 224, 160], supersedes: [255, 120, 120], part_of: [200, 164, 255]
  };
  const WHITE = [255, 255, 255], GOLD = [255, 205, 140];
  const predLabel = name => (data.predicates[name] && data.predicates[name].label) || name.replace(/_/g, " ");
  const predColor = name => PRED[name] || WHITE;

  // ── Graph in three dimensions ────────────────────────────
  // The reviewed 2-D layout becomes the galactic plane; height comes from a
  // stable hash so the sky is identical on every visit.
  const byId = new Map();
  const nodes = data.nodes.map((n, i) => {
    const fam = FAMILY[n.family] || FAMILY.document;
    const o = {n, i, fam, x: (n.x - 500) * 1.05, y: (hash01(n.id + "/height") - 0.5) * 200 + fam.lift, z: -(n.y - 325) * 1.3,
      edges: [], b: 1, lab: 0, sx: 0, sy: 0, k: 0, depth: 0, fade: 1, r: 4, old: n.status === "superseded"};
    byId.set(n.id, o);
    return o;
  });
  {
    let cx = 0, cz = 0;
    for (const o of nodes) { cx += o.x; cz += o.z; }
    cx /= nodes.length; cz /= nodes.length;
    for (const o of nodes) { o.x -= cx; o.z -= cz; }
  }
  const edgeKey = e => e.from + "|" + e.predicate + "|" + e.to;
  const edgeByKey = new Map();
  const edges = data.edges.map((e, i) => {
    const a = byId.get(e.from), b = byId.get(e.to);
    const o = {e, i, a, b, phase: hash01(edgeKey(e)), heat: 0.22, lab: 0, spot: 0.5, color: predColor(e.predicate), label: predLabel(e.predicate).toUpperCase()};
    a.edges.push(o); b.edges.push(o);
    edgeByKey.set(edgeKey(e), o);
    return o;
  });
  const RW = 520;

  // Dust: three spiral arms in the plane plus a faint halo, for scale.
  const dust = [];
  {
    const rand = seeded(20261110);
    for (let i = 0; i < 1300; i++) {
      const arm = i % 3, r = 70 + Math.pow(rand(), 0.75) * 900;
      const theta = arm * 2.0944 + r * 0.0058 + (rand() - 0.5) * (0.95 - r / 2400);
      const thick = (rand() + rand() + rand() - 1.5) * 46 * (1.1 - r / 1500);
      dust.push({x: Math.cos(theta) * r, y: thick, z: Math.sin(theta) * r, tone: r < 300 ? 0 : (rand() < 0.5 ? 1 : 2), size: rand() < 0.1 ? 2.1 : 1.25, a: 0.3 + rand() * 0.6});
    }
    for (let i = 0; i < 260; i++) {
      const r = 300 + rand() * 900, t = rand() * 6.283, p = Math.acos(rand() * 2 - 1);
      dust.push({x: Math.sin(p) * Math.cos(t) * r, y: Math.cos(p) * r * 0.55, z: Math.sin(p) * Math.sin(t) * r, tone: 2, size: 1, a: 0.12 + rand() * 0.3});
    }
  }
  const DUST_TONE = [[255, 214, 170], [170, 196, 255], [214, 220, 236]];
  const farStars = [];
  {
    const rand = seeded(471);
    for (let i = 0; i < 520; i++) {
      const t = rand() * 6.283, p = Math.acos(rand() * 2 - 1);
      farStars.push({x: Math.sin(p) * Math.cos(t), y: Math.cos(p), z: Math.sin(p) * Math.sin(t), a: 0.15 + rand() * 0.55, size: rand() < 0.07 ? 1.7 : 1, tw: rand() * 6.283});
    }
  }

  // ── Elements and state ───────────────────────────────────
  const stage = $("stage"), canvas = $("sky"), mid = $("mid"), tip = $("tip"), hint = $("hint");
  const ctx = canvas.getContext("2d");
  const reducedQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const stackedQuery = window.matchMedia("(max-width: 1099px)");
  let reduced = reducedQuery.matches;
  const S = {
    selected: null, selectedEdge: null, hover: null, hoverEdge: null,
    family: null, predicate: null, labels: true, drift: true,
    thought: null, budget: 800, radius: 2, owner: ""
  };
  const cam = {yaw: 0.55, tilt: 0.9, zoom: 1, zoomT: 1, fx: 0, fy: 0, fz: 0, tx: 0, ty: 0, tz: 0, dist: 1500, F: 1000, ox: 0, oy: 0, cy: 1, sn: 0, ct: 1, st: 0};
  const view = {w: 0, h: 0, fit: 600, dpr: 1};
  let dirty = true, lastFrame = 0, lastInput = -1e6, spin = 0;
  const started = performance.now();
  const invalidate = () => { dirty = true; };

  function sprite(color) {
    const size = 96, c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d"), grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, rgba(color, 1)); grad.addColorStop(0.16, rgba(color, 0.55));
    grad.addColorStop(0.45, rgba(color, 0.13)); grad.addColorStop(1, rgba(color, 0));
    g.fillStyle = grad; g.fillRect(0, 0, size, size);
    return c;
  }
  const glow = {white: sprite(WHITE), gold: sprite(GOLD), core: sprite([255, 190, 130])};
  for (const key of Object.keys(FAMILY)) glow[key] = sprite(FAMILY[key].color);

  function resize() {
    const box = stage.getBoundingClientRect(), free = mid.getBoundingClientRect();
    view.dpr = Math.min(window.devicePixelRatio || 1, 2);
    view.w = Math.max(1, box.width); view.h = Math.max(1, box.height);
    canvas.width = Math.round(view.w * view.dpr); canvas.height = Math.round(view.h * view.dpr);
    const x0 = free.left - box.left, x1 = free.right - box.left;
    const floor = Math.max(view.h * 0.5, $("dock").getBoundingClientRect().top - box.top - 8);
    view.fit = Math.max(200, Math.min((x1 - x0) * 1.04, floor * 1.5));
    cam.ox = (x0 + x1) / 2; cam.oy = floor * 0.5 + 6;
    invalidate();
  }

  const P = {sx: 0, sy: 0, k: 0, depth: 0};
  function project(x, y, z, out) {
    x -= cam.fx; y -= cam.fy; z -= cam.fz;
    const x1 = x * cam.cy - z * cam.sn, z1 = x * cam.sn + z * cam.cy;
    const y1 = y * cam.ct + z1 * cam.st, z2 = -y * cam.st + z1 * cam.ct;
    out.depth = cam.dist + z2;
    out.k = cam.F / Math.max(out.depth, 120);
    out.sx = cam.ox + x1 * out.k; out.sy = cam.oy - y1 * out.k;
    return out;
  }

  // ── The thought: a question travelling through the graph ─
  const T_SEED = 1100, T_HOP = 1150, T_SETTLE = 500;
  function think(spec, instant) {
    const options = {seedIds: spec.seedIds, radius: S.radius, budget: S.budget, owner: S.owner || undefined};
    const pack = model.selectContext(data, options);
    const reach = new Map(), tree = new Map();
    let maxHops = 0;
    for (const visit of pack.reach) {
      maxHops = Math.max(maxHops, visit.hops);
      const at = visit.hops === 0 ? 0 : T_SEED + visit.hops * T_HOP;
      reach.set(visit.id, {hops: visit.hops, at});
      if (visit.via) {
        const edge = edgeByKey.get(edgeKey(visit.via));
        if (edge) tree.set(edge.i, {start: at - T_HOP, at, from: byId.get(visit.via.traversedFrom), to: byId.get(visit.via.traversedTo)});
      }
    }
    const verdict = T_SEED + maxHops * T_HOP + T_SETTLE;
    S.thought = {spec, pack, reach, tree, kept: new Set(pack.items.map(item => item.node.id)), verdict, maxHops,
      t0: performance.now() - (instant || reduced ? verdict + 5000 : 0), stage: 0};
    let cx = 0, cy = 0, cz = 0;
    for (const id of reach.keys()) { const o = byId.get(id); cx += o.x; cy += o.y; cz += o.z; }
    const count = Math.max(1, reach.size);
    cam.tx = cx / count * 0.5; cam.ty = cy / count * 0.5; cam.tz = cz / count * 0.5;
    $("stepper").hidden = false;
    hint.classList.add("gone");
    renderPack();
    renderScenarioState();
    invalidate();
  }
  function clearThought() {
    S.thought = null;
    $("stepper").hidden = true;
    for (const id of ["step1", "step2", "step3"]) $(id).className = "";
    cam.tx = cam.ty = cam.tz = 0;
    if (S.selected) focusOn(byId.get(S.selected));
    renderPack(); renderScenarioState(); invalidate();
  }
  function thoughtTime(now) { return S.thought ? (reduced ? Infinity : now - S.thought.t0) : 0; }
  function updateStepper(tt) {
    const T = S.thought;
    const stageNow = tt < T_SEED ? 1 : tt < T.verdict ? 2 : 3;
    if (stageNow === T.stage) return;
    T.stage = stageNow;
    [1, 2, 3].forEach(n => { $("step" + n).className = n < stageNow ? "done" : n === stageNow ? "now" : ""; });
    const names = T.spec.seedIds.map(id => byId.get(id).n.title);
    let line;
    if (stageNow === 1) line = "Starting from " + list(names) + ".";
    else if (stageNow === 2) line = "Following threads " + S.radius + (S.radius === 1 ? " step" : " steps") + " out: " + T.reach.size + " records reached.";
    else {
      line = "A constellation of " + T.pack.items.length + (T.pack.items.length === 1 ? " record" : " records") + ", about " + T.pack.estimatedTokens + " of " + S.budget + " tokens.";
      if (T.pack.overBudget) line += " " + T.pack.overBudget + " did not fit.";
      if (T.pack.superseded) line += " " + T.pack.superseded + " superseded, left as history.";
      const fill = document.querySelector(".meter span");
      if (fill) fill.style.width = clamp(T.pack.estimatedTokens / Math.max(1, S.budget) * 100, 0, 100) + "%";
    }
    $("stepline").textContent = line;
  }
  function list(names) {
    if (names.length < 2) return names.join("");
    return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
  }

  // ── Emphasis: what glows, what recedes ───────────────────
  function emphasis(tt) {
    const T = S.thought, focus = S.selected ? byId.get(S.selected) : null;
    const hoverNode = S.hover, selEdge = S.selectedEdge != null ? edges[S.selectedEdge] : null;
    for (const o of nodes) {
      let b = 1;
      if (T) {
        const r = T.reach.get(o.n.id);
        if (!r || tt < r.at) b = 0.2;
        else if (tt < T.verdict) b = 1.25;
        else b = T.kept.has(o.n.id) ? 1.35 : o.old ? 0.36 : 0.5;
      } else if (focus) {
        b = o === focus ? 1.35 : 0.3;
      } else if (selEdge) {
        b = o === selEdge.a || o === selEdge.b ? 1.3 : 0.3;
      }
      if (S.family && o.n.family !== S.family) b = Math.min(b, 0.2);
      o.target = b;
    }
    for (const eo of edges) {
      let heat = 0.22;
      if (T) {
        const leg = T.tree.get(eo.i), bothKept = T.kept.has(eo.e.from) && T.kept.has(eo.e.to);
        if (leg) heat = tt < leg.start ? 0.05 : tt < T.verdict ? 0.95 : bothKept ? 0.95 : 0.3;
        else heat = tt >= T.verdict && bothKept ? 0.55 : 0.05;
      } else if (focus) {
        heat = eo.a === focus || eo.b === focus ? 1 : 0.06;
      } else if (selEdge) {
        heat = eo === selEdge ? 1 : 0.06;
      }
      if (S.predicate) heat = eo.e.predicate === S.predicate ? Math.max(heat, 0.9) : Math.min(heat, 0.04);
      if (S.family && eo.a.n.family !== S.family && eo.b.n.family !== S.family) heat = Math.min(heat, 0.05);
      if (focus && T && (eo.a === focus || eo.b === focus)) heat = Math.max(heat, 0.85);
      if (eo === selEdge || eo === S.hoverEdge) heat = 1;
      if (hoverNode && (eo.a === hoverNode || eo.b === hoverNode)) heat = Math.max(heat, 0.9);
      eo.target = heat;
    }
    if (!T && focus) for (const eo of focus.edges) { const other = eo.a === focus ? eo.b : eo.a; other.target = Math.max(other.target, S.family && other.n.family !== S.family ? 0.2 : 1.15); }
    if (S.predicate && !T && !focus) for (const o of nodes) if (!o.edges.some(eo => eo.e.predicate === S.predicate)) o.target = Math.min(o.target, 0.2);
    if (focus) focus.target = Math.max(focus.target, 1.35);
    if (hoverNode) hoverNode.target = Math.max(hoverNode.target, 1.4);
    const q = reduced ? 1 : 0.11;
    for (const o of nodes) o.b += (o.target - o.b) * q;
    for (const eo of edges) eo.heat += (eo.target - eo.heat) * q;
  }

  // ── Frame ────────────────────────────────────────────────
  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min(60, now - lastFrame); lastFrame = now;
    if (reduced && !dirty) return;
    dirty = false;

    const intro = reduced ? 1 : ease((now - started) / 2600);
    const q = reduced ? 1 : Math.min(1, dt * 0.005);
    cam.zoom += (cam.zoomT - cam.zoom) * q;
    cam.fx += (cam.tx - cam.fx) * q * 0.6; cam.fy += (cam.ty - cam.fy) * q * 0.6; cam.fz += (cam.tz - cam.fz) * q * 0.6;
    const wantSpin = S.drift && !reduced && !drag.active && !S.hover && !S.hoverEdge && now - lastInput > 1600 ? (S.selected || S.thought ? 0.35 : 1) : 0;
    spin += (wantSpin - spin) * 0.04;
    cam.yaw += spin * 0.000055 * dt + (1 - intro) * 0.0009 * dt;
    const tilt = cam.tilt - (1 - intro) * 0.45;
    cam.cy = Math.cos(cam.yaw); cam.sn = Math.sin(cam.yaw); cam.ct = Math.cos(tilt); cam.st = Math.sin(tilt);
    cam.F = cam.dist * view.fit / (2 * RW) * cam.zoom * (0.55 + 0.45 * intro);

    const tt = thoughtTime(now);
    if (S.thought) updateStepper(tt);
    emphasis(tt);
    for (const o of nodes) {
      project(o.x, o.y, o.z, P);
      o.sx = P.sx; o.sy = P.sy; o.k = P.k; o.depth = P.depth;
      o.fade = clamp(1.08 - (P.depth - (cam.dist - 520)) / 1500, 0.4, 1);
      const unit = Math.pow(clamp(P.k * 1.25, 0.2, 4), 0.62);
      o.r = Math.max(2.2, (3 + Math.sqrt(o.edges.length) * 1.3) * unit);
    }
    draw(now, tt, intro);
  }

  function draw(now, tt, intro) {
    const w = view.w, hgt = view.h, T = S.thought;
    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);
    ctx.globalCompositeOperation = "lighter";

    // Far stars turn with the sky; they stand in for everything not shown.
    const reach = Math.max(w, hgt) * 0.85;
    for (const s of farStars) {
      const x1 = s.x * cam.cy - s.z * cam.sn, z1 = s.x * cam.sn + s.z * cam.cy;
      const y1 = s.y * cam.ct + z1 * cam.st, z2 = -s.y * cam.st + z1 * cam.ct;
      if (z2 < 0.05) continue;
      const tw = reduced ? 1 : 0.75 + 0.25 * Math.sin(now * 0.0012 + s.tw);
      ctx.fillStyle = "rgba(214,224,255," + (s.a * tw * intro * (0.4 + z2 * 0.6)).toFixed(3) + ")";
      ctx.fillRect(w / 2 + x1 * reach, hgt / 2 - y1 * reach, s.size, s.size);
    }

    // Galactic core and dust.
    project(0, 0, 0, P);
    const coreW = 1150 * P.k, coreH = coreW * Math.max(0.3, Math.abs(cam.st));
    ctx.globalAlpha = 0.34 * intro;
    ctx.drawImage(glow.core, P.sx - coreW / 2, P.sy - coreH / 2, coreW, coreH);
    ctx.globalAlpha = 1;
    const calm = T || S.selected ? 0.55 : 1;
    for (const d of dust) {
      project(d.x, d.y, d.z, P);
      if (P.sx < -4 || P.sx > w + 4 || P.sy < -4 || P.sy > hgt + 4) continue;
      const size = d.size * clamp(P.k * 1.5, 0.7, 2.6);
      ctx.fillStyle = rgba(DUST_TONE[d.tone], d.a * intro * calm * clamp(1.2 - (P.depth - 900) / 1600, 0.25, 1));
      ctx.fillRect(P.sx, P.sy, size, size);
    }

    // Threads.
    ctx.lineCap = "round";
    for (const eo of edges) {
      const a = eo.a, b = eo.b, heat = eo.heat, fade = Math.min(a.fade, b.fade) * intro;
      if (heat > 0.5) {
        ctx.strokeStyle = rgba(eo.color, 0.13 * heat * fade); ctx.lineWidth = 6;
        ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
      }
      ctx.strokeStyle = rgba(eo.color, (0.07 + heat * 0.62) * fade); ctx.lineWidth = 0.6 + heat * 1.3;
      ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
      // The spark always runs from source to target.
      if (!reduced && heat > 0.1) {
        const u = (now * 0.00022 + eo.phase) % 1, size = 3 + heat * 7;
        ctx.globalAlpha = (0.25 + heat * 0.75) * fade * Math.sin(u * Math.PI);
        ctx.drawImage(glow.white, a.sx + (b.sx - a.sx) * u - size, a.sy + (b.sy - a.sy) * u - size, size * 2, size * 2);
        ctx.globalAlpha = 1;
      }
    }

    // The travelling thought.
    if (T && tt < T.verdict + 400) {
      for (const leg of T.tree.values()) {
        if (tt < leg.start || tt > leg.at + 120) continue;
        const u = ease((tt - leg.start) / T_HOP);
        for (let j = 0; j < 9; j++) {
          const v = u - j * 0.03;
          if (v < 0) break;
          const size = (15 - j * 1.4) * (j === 0 ? 1.3 : 1);
          ctx.globalAlpha = (1 - j / 9) * 0.9;
          ctx.drawImage(j === 0 ? glow.white : glow.gold, leg.from.sx + (leg.to.sx - leg.from.sx) * v - size, leg.from.sy + (leg.to.sy - leg.from.sy) * v - size, size * 2, size * 2);
        }
      }
      ctx.globalAlpha = 1;
    }

    // Stars.
    for (const o of nodes) {
      const b = o.b, a = clamp(b, 0, 1) * o.fade * intro;
      const halo = o.r * (5.5 + Math.max(0, b - 1) * 7);
      ctx.globalAlpha = clamp(0.6 * a * (b > 1 ? 1.4 : 1), 0, 1);
      ctx.drawImage(glow[o.n.family] || glow.white, o.sx - halo, o.sy - halo, halo * 2, halo * 2);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    const ordered = nodes.slice().sort((p, q) => q.depth - p.depth);
    for (const o of ordered) {
      const a = clamp(o.b, 0.16, 1) * o.fade * intro, c = o.fam.color;
      ctx.beginPath(); ctx.arc(o.sx, o.sy, o.r, 0, 6.2832);
      if (o.old) {
        ctx.fillStyle = "rgba(4,5,10," + a.toFixed(3) + ")"; ctx.fill();
        ctx.setLineDash([2.5, 2.5]); ctx.strokeStyle = rgba(c, a); ctx.lineWidth = 1.3; ctx.stroke(); ctx.setLineDash([]);
      } else {
        ctx.fillStyle = rgba([Math.round(c[0] * 0.45 + 140), Math.round(c[1] * 0.45 + 140), Math.round(c[2] * 0.45 + 140)], a); ctx.fill();
      }
      if (T) {
        const r = T.reach.get(o.n.id);
        if (r && tt >= r.at) {
          const since = tt - r.at;
          if (since < 900) { // arrival flash
            const p = since / 900;
            ctx.strokeStyle = rgba(GOLD, (1 - p) * 0.9); ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(o.sx, o.sy, o.r + 4 + p * 34, 0, 6.2832); ctx.stroke();
          }
          if (tt >= T.verdict && T.kept.has(o.n.id)) {
            const settle = ease((tt - T.verdict) / 700);
            ctx.strokeStyle = rgba(GOLD, 0.85 * settle); ctx.lineWidth = 1.4;
            ctx.beginPath(); ctx.arc(o.sx, o.sy, o.r + 5 + (1 - settle) * 10, 0, 6.2832); ctx.stroke();
            if (r.hops === 0) { ctx.strokeStyle = rgba(GOLD, 0.4 * settle); ctx.beginPath(); ctx.arc(o.sx, o.sy, o.r + 10, 0, 6.2832); ctx.stroke(); }
          }
        }
      }
      if (S.selected === o.n.id) {
        const turn = reduced ? 0 : now * 0.0006, R = o.r + 11;
        ctx.strokeStyle = "rgba(255,255,255,.95)"; ctx.lineWidth = 1.5;
        for (let j = 0; j < 4; j++) { ctx.beginPath(); ctx.arc(o.sx, o.sy, R, turn + j * 1.5708 + 0.25, turn + j * 1.5708 + 1.32); ctx.stroke(); }
      }
    }

    drawLabels(intro);
  }

  // ── Labels with collision avoidance ──────────────────────
  const NODE_FONT = "600 12px -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, sans-serif";
  const EDGE_FONT = "600 9.5px ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace";
  const widths = new Map();
  function measure(font, text) {
    const key = font + text;
    let value = widths.get(key);
    if (value == null) { ctx.font = font; value = ctx.measureText(text).width; widths.set(key, value); }
    return value;
  }
  function drawLabels(intro) {
    const placed = [], wanted = [], w = view.w, hgt = view.h;
    for (const o of nodes) if (o.b > 0.42) placed.push([o.sx, o.sy, o.r + 2, o.r + 2]);
    for (const o of nodes) {
      const hot = o === S.hover || S.selected === o.n.id;
      const show = o.b > 0.42 || hot;
      const tw = measure(NODE_FONT, o.n.title);
      wanted.push({node: o, show, alpha: hot ? 1 : clamp(o.b, 0, 1) * (0.5 + 0.5 * o.fade),
        pri: (hot ? 9 : 2) + o.b + o.edges.length * 0.03 + (o.lab > 0.5 ? 0.4 : 0) + (1 - (o.depth - 900) / 1400) * 0.3,
        cx: o.sx, cy: o.sy + o.r + 11, hw: tw / 2 + 5, hh: 8});
    }
    for (const eo of edges) {
      const dx = eo.b.sx - eo.a.sx, dy = eo.b.sy - eo.a.sy, len = Math.hypot(dx, dy);
      const hot = eo.heat > 0.5;
      const text = dx >= 0 ? eo.label + " →" : "← " + eo.label;
      const tw = measure(EDGE_FONT, text) + text.length * 0.7;
      const show = eo.heat > 0.15 && (hot || S.labels) && len > (hot ? tw * 0.55 : tw + 6);
      let angle = Math.atan2(dy, dx);
      if (angle > 1.5708) angle -= 3.1416; else if (angle < -1.5708) angle += 3.1416;
      const ca = Math.abs(Math.cos(angle)), sa = Math.abs(Math.sin(angle));
      const fade = Math.min(eo.a.fade, eo.b.fade);
      wanted.push({edge: eo, show, text, angle, alpha: hot ? 0.55 + eo.heat * 0.45 : 0.74 * fade,
        pri: 1 + eo.heat * 2.4 + (eo.lab > 0.5 ? 0.4 : 0) + fade * 0.3 + Math.min(len, 300) * 0.001,
        ax: eo.a.sx, ay: eo.a.sy, dx, dy, cx: eo.a.sx + dx * eo.spot, cy: eo.a.sy + dy * eo.spot, hw: ca * (tw / 2 + 4) + sa * 7, hh: sa * (tw / 2 + 4) + ca * 7});
    }
    wanted.sort((p, q) => q.pri - p.pri);
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
    for (const item of wanted) {
      const owner = item.node || item.edge;
      let ok = false;
      if (item.show) {
        // A thread label may slide along its thread to find clear sky; it tries its last spot first.
        const spots = item.edge ? [item.edge.spot, 0.5, 0.4, 0.6, 0.32, 0.68] : [0];
        for (const spot of spots) {
          const cx = item.edge ? item.ax + item.dx * spot : item.cx, cy = item.edge ? item.ay + item.dy * spot : item.cy;
          if (cx < -40 || cx > w + 40 || cy < 4 || cy > hgt - 4) continue;
          let clear = true;
          for (const p of placed) if (Math.abs(p[0] - cx) < p[2] + item.hw && Math.abs(p[1] - cy) < p[3] + item.hh) { clear = false; break; }
          if (!clear) continue;
          item.cx = cx; item.cy = cy; ok = true;
          if (item.edge) item.edge.spot = spot;
          placed.push([cx, cy, item.hw, item.hh]);
          break;
        }
      }
      const target = ok ? item.alpha : 0;
      owner.lab += (target - owner.lab) * (reduced ? 1 : 0.16);
      const alpha = owner.lab * intro;
      if (alpha < 0.03) continue;
      if (item.node) {
        ctx.font = NODE_FONT;
        ctx.lineWidth = 3.5; ctx.strokeStyle = "rgba(3,4,9," + (0.85 * alpha).toFixed(3) + ")";
        ctx.strokeText(item.node.n.title, item.cx, item.cy);
        ctx.fillStyle = "rgba(238,241,248," + alpha.toFixed(3) + ")";
        ctx.fillText(item.node.n.title, item.cx, item.cy);
      } else {
        ctx.save();
        ctx.translate(item.cx, item.cy); ctx.rotate(item.angle);
        ctx.font = EDGE_FONT;
        if ("letterSpacing" in ctx) ctx.letterSpacing = "0.7px";
        ctx.lineWidth = 3.5; ctx.strokeStyle = "rgba(3,4,9," + (0.9 * alpha).toFixed(3) + ")";
        ctx.strokeText(item.text, 0, -7);
        ctx.fillStyle = rgba(item.edge.color, alpha);
        ctx.fillText(item.text, 0, -7);
        ctx.restore();
      }
    }
  }

  // ── Picking and pointer input ────────────────────────────
  const drag = {active: false, moved: false, x: 0, y: 0, pointers: new Map(), pinch: 0};
  function localPoint(event) {
    const box = canvas.getBoundingClientRect();
    return {x: event.clientX - box.left, y: event.clientY - box.top};
  }
  function pick(x, y) {
    let best = null, bestD = Infinity;
    for (const o of nodes) {
      const d = Math.hypot(o.sx - x, o.sy - y), limit = Math.max(o.r + 9, 15);
      if (d < limit && d - (o.b > 0.42 ? 6 : 0) < bestD) { best = o; bestD = d; }
    }
    if (best) return {node: best};
    let bestEdge = null; bestD = 7;
    for (const eo of edges) {
      if (eo.heat < 0.1) continue;
      const ax = eo.a.sx, ay = eo.a.sy, dx = eo.b.sx - ax, dy = eo.b.sy - ay, len2 = dx * dx + dy * dy || 1;
      const u = clamp(((x - ax) * dx + (y - ay) * dy) / len2, 0.08, 0.92);
      const d = Math.hypot(ax + dx * u - x, ay + dy * u - y);
      if (d < bestD) { bestD = d; bestEdge = eo; }
    }
    return bestEdge ? {edge: bestEdge} : null;
  }
  function showTip(hit, x, y) {
    if (!hit) { tip.hidden = true; return; }
    tip.replaceChildren();
    if (hit.node) {
      const n = hit.node.n;
      tip.append(h("b", {text: n.title}), h("span", {class: "tip-meta", text: hit.node.fam.one + " · " + n.kind + (hit.node.old ? " · superseded" : "")}));
    } else {
      const e = hit.edge.e;
      tip.append(h("b", {text: byId.get(e.from).n.title + " → " + byId.get(e.to).n.title}),
        h("span", {class: "tip-meta c-" + e.predicate, text: predLabel(e.predicate)}), h("div", {text: e.explanation}));
    }
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.transform = "translate(" + Math.round(clamp(x + 16, 8, view.w - tw - 8)) + "px," + Math.round(clamp(y + 18, 8, view.h - th - 8)) + "px)";
  }
  function touched() { lastInput = performance.now(); hint.classList.add("gone"); invalidate(); }

  canvas.addEventListener("pointerdown", event => {
    canvas.setPointerCapture(event.pointerId);
    drag.pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    drag.active = true; drag.moved = false; drag.x = event.clientX; drag.y = event.clientY; drag.pinch = 0;
    canvas.classList.add("dragging"); tip.hidden = true; touched();
  });
  canvas.addEventListener("pointermove", event => {
    if (drag.active && drag.pointers.has(event.pointerId)) {
      drag.pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
      if (drag.pointers.size >= 2) {
        const pts = [...drag.pointers.values()], gap = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        if (drag.pinch) cam.zoomT = clamp(cam.zoomT * gap / drag.pinch, 0.45, 3.4);
        drag.pinch = gap; drag.moved = true;
      } else {
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
        if (drag.moved || Math.hypot(dx, dy) > 5) {
          drag.moved = true;
          cam.yaw -= dx * 0.006; cam.tilt = clamp(cam.tilt + dy * 0.005, 0.12, 1.5);
          drag.x = event.clientX; drag.y = event.clientY;
        }
      }
      touched();
      return;
    }
    if (event.pointerType === "touch") return;
    const point = localPoint(event), hit = pick(point.x, point.y);
    const node = hit && hit.node || null, edge = hit && hit.edge || null;
    if (node !== S.hover || edge !== S.hoverEdge) { S.hover = node; S.hoverEdge = edge; invalidate(); }
    canvas.classList.toggle("pointing", !!hit);
    showTip(hit, point.x, point.y);
  });
  function release(event) {
    if (!drag.pointers.has(event.pointerId)) return;
    drag.pointers.delete(event.pointerId);
    drag.pinch = 0;
    if (drag.pointers.size) { const rest = [...drag.pointers.values()][0]; drag.x = rest.x; drag.y = rest.y; return; }
    drag.active = false; canvas.classList.remove("dragging");
    if (!drag.moved && event.type === "pointerup") {
      const point = localPoint(event), hit = pick(point.x, point.y);
      if (hit && hit.node) select(hit.node.n.id, true);
      else if (hit && hit.edge) selectEdge(hit.edge.i);
      else deselect();
    }
    touched();
  }
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("pointerleave", () => { if (S.hover || S.hoverEdge) { S.hover = null; S.hoverEdge = null; invalidate(); } tip.hidden = true; canvas.classList.remove("pointing"); });
  canvas.addEventListener("wheel", event => {
    if (stackedQuery.matches && !event.ctrlKey) return; // stacked layout: the wheel scrolls the page; pinch still zooms
    event.preventDefault();
    cam.zoomT = clamp(cam.zoomT * Math.exp(-event.deltaY * 0.0014), 0.45, 3.4);
    touched();
  }, {passive: false});
  canvas.addEventListener("keydown", event => {
    const key = event.key;
    if (key === "ArrowLeft") cam.yaw += 0.12; else if (key === "ArrowRight") cam.yaw -= 0.12;
    else if (key === "ArrowUp") cam.tilt = clamp(cam.tilt + 0.08, 0.12, 1.5); else if (key === "ArrowDown") cam.tilt = clamp(cam.tilt - 0.08, 0.12, 1.5);
    else if (key === "+" || key === "=") cam.zoomT = clamp(cam.zoomT * 1.15, 0.45, 3.4); else if (key === "-" || key === "_") cam.zoomT = clamp(cam.zoomT / 1.15, 0.45, 3.4);
    else return;
    event.preventDefault(); touched();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (S.selected || S.selectedEdge != null) deselect(); else if (S.thought) clearThought();
  });

  // ── Selection and panels ─────────────────────────────────
  const tabs = ["record", "pack", "all"];
  function showTab(name, focus) {
    for (const t of tabs) {
      const on = t === name, button = $("tab-" + t);
      button.setAttribute("aria-selected", String(on)); button.tabIndex = on ? 0 : -1;
      $("view-" + t).hidden = !on;
      if (on && focus) button.focus();
    }
  }
  tabs.forEach((name, index) => {
    const button = $("tab-" + name);
    button.addEventListener("click", () => showTab(name));
    button.addEventListener("keydown", event => {
      const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      showTab(tabs[(index + step + tabs.length) % tabs.length], true);
    });
  });
  function focusOn(o) { cam.tx = o.x * 0.5; cam.ty = o.y * 0.5; cam.tz = o.z * 0.5; }
  function reveal() { if (stackedQuery.matches) $("panel").scrollIntoView({behavior: reduced ? "auto" : "smooth", block: "start"}); }
  function select(id, fromSky) {
    const o = byId.get(id);
    if (!o) return;
    S.selected = id; S.selectedEdge = null;
    if (!S.thought) focusOn(o);
    renderRecord(); renderAllState(); showTab("record");
    if (fromSky) reveal(); else $("view-record").focus({preventScroll: stackedQuery.matches ? false : true});
    hint.classList.add("gone"); invalidate();
  }
  function selectEdge(index) {
    S.selectedEdge = index; S.selected = null;
    renderRecord(); renderAllState(); showTab("record"); reveal(); invalidate();
  }
  function deselect() {
    if (!S.selected && S.selectedEdge == null) return;
    S.selected = null; S.selectedEdge = null;
    if (!S.thought) cam.tx = cam.ty = cam.tz = 0;
    renderRecord(); renderAllState(); invalidate();
  }
  const dot = family => h("span", {class: "dot c-" + family, "aria-hidden": true});
  function relationRow(eo, self) {
    const outgoing = eo.a === self, other = outgoing ? eo.b : eo.a;
    const line = h("span", {class: "rel-line"}, outgoing
      ? [h("span", {class: "self", text: "this"}), h("span", {class: "pred c-" + eo.e.predicate, text: "→ " + predLabel(eo.e.predicate) + " →"}), other.n.title]
      : [other.n.title, h("span", {class: "pred c-" + eo.e.predicate, text: "→ " + predLabel(eo.e.predicate) + " →"}), h("span", {class: "self", text: "this"})]);
    return h("li", null, h("button", {type: "button", class: "rel", on: {click: () => select(other.n.id)}}, [line, h("span", {class: "rel-why", text: eo.e.explanation})]));
  }
  function renderRecord() {
    const intro = $("record-intro"), body = $("record-body");
    body.replaceChildren();
    if (S.selectedEdge != null) {
      const eo = edges[S.selectedEdge], e = eo.e, meaning = data.predicates[e.predicate] || {};
      put(body, 
        h("p", {class: "eyebrow", text: "Relationship"}),
        h("h3", null, [eo.a.n.title, h("br"), h("span", {class: "c-" + e.predicate, text: "→ " + predLabel(e.predicate) + " →"}), h("br"), eo.b.n.title]),
        h("p", {class: "summary", text: e.explanation}),
        meaning.description ? h("blockquote", {text: "“" + predLabel(e.predicate) + "” means: " + meaning.description}) : null,
        h("p", {class: "section-title", text: "The two records"}),
        h("ul", {class: "rels"}, [eo.a, eo.b].map(o => h("li", null, h("button", {type: "button", class: "rel", on: {click: () => select(o.n.id)}},
          [h("span", {class: "rel-line", text: o.n.title}), h("span", {class: "rel-why", text: o.n.summary})]))))
      );
    } else if (S.selected) {
      const o = byId.get(S.selected), n = o.n;
      const out = o.edges.filter(eo => eo.a === o), incoming = o.edges.filter(eo => eo.b === o);
      put(body, 
        h("p", {class: "eyebrow", text: "Record"}),
        h("h3", {text: n.title}),
        h("div", {class: "tags"}, [
          h("span", {class: "tag"}, [dot(n.family), o.fam.one]),
          h("span", {class: "tag", text: n.kind}),
          h("span", {class: "tag", text: n.owner}),
          o.old ? h("span", {class: "tag old", text: "superseded"}) : null
        ]),
        h("p", {class: "summary", text: n.summary}),
        h("blockquote", {text: n.excerpt}),
        h("span", {class: "source", text: n.source + "  (invented path)"}),
        h("button", {type: "button", class: "action", on: {click: () => ask({seedIds: [n.id], question: "What surrounds “" + n.title + "”?", why: "Started from the record you chose, then followed its threads outward."})}}, "✦ Ask from this star"),
        out.length ? h("p", {class: "section-title", text: "It points to (" + out.length + ")"}) : null,
        out.length ? h("ul", {class: "rels"}, out.map(eo => relationRow(eo, o))) : null,
        incoming.length ? h("p", {class: "section-title", text: "Pointing at it (" + incoming.length + ")"}) : null,
        incoming.length ? h("ul", {class: "rels"}, incoming.map(eo => relationRow(eo, o))) : null
      );
    }
    const has = body.childNodes.length > 0;
    body.hidden = !has; intro.hidden = has;
    $("view-record").scrollTop = 0;
  }

  function pathLine(item) {
    if (!item.path.length) return h("span", {class: "path seed", text: "Starting point: it matched the question directly."});
    const line = h("span", {class: "path"}, byId.get(item.path[0].traversedFrom).n.title);
    for (const step of item.path) {
      const forward = step.from === step.traversedFrom;
      line.append(h("span", {class: "pp c-" + step.predicate, text: (forward ? " → " : " ← ") + predLabel(step.predicate) + (forward ? " → " : " ← ")}), byId.get(step.traversedTo).n.title);
    }
    return line;
  }
  function renderPack() {
    const body = $("pack-body"), T = S.thought;
    body.replaceChildren();
    if (!T) {
      put(body, h("p", {class: "eyebrow", text: "Context pack"}), h("h3", {text: "Nothing asked yet."}),
        h("p", {class: "quiet", text: "Ask a question on the left. The constellation of records the Brain would hand to an agent appears here, each with the trail of relationships that led to it."}));
      return;
    }
    const pack = T.pack, settled = T.stage === 3 || reduced || performance.now() - T.t0 > T.verdict;
    const fill = h("span");
    if (settled) fill.style.width = clamp(pack.estimatedTokens / Math.max(1, S.budget) * 100, 0, 100) + "%";
    const left = [];
    if (pack.overBudget) left.push(pack.overBudget + " reached but did not fit the budget");
    if (pack.superseded) left.push(pack.superseded + " superseded and kept only as history");
    put(body, 
      h("p", {class: "eyebrow", text: "Context pack"}),
      h("p", {class: "question", text: T.spec.question}),
      T.spec.why ? h("p", {class: "why", text: T.spec.why}) : null,
      h("div", {class: "meter", role: "img", "aria-label": "About " + pack.estimatedTokens + " of " + S.budget + " tokens used"}, fill),
      h("div", {class: "meter-row"}, [h("span", {text: pack.items.length + " of " + pack.considered + " reached records kept"}), h("span", {text: "~" + pack.estimatedTokens + " / " + S.budget + " tokens"})]),
      left.length ? h("p", {class: "leftout", text: left.join(" · ") + "."}) : null,
      pack.items.length ? null : h("p", {class: "empty", text: "Nothing fits. Raise the budget to let the first record through."}),
      h("ol", {class: "pack"}, pack.items.map(item => h("li", null, h("button", {type: "button", class: "pack-item", on: {click: () => select(item.node.id)}}, [
        h("span", {class: "pack-head"}, [dot(item.node.family), h("span", {class: "pack-title", text: item.node.title}),
          h("span", {class: "pack-meta", text: (item.hops === 0 ? "start" : item.hops + (item.hops === 1 ? " step" : " steps")) + " · ~" + item.estimatedTokens + " tok"})]),
        pathLine(item)
      ])))),
      h("p", {class: "quiet", text: "Token counts are rough estimates (characters ÷ 4). This is a teaching model of the idea, not a measurement of the production engine."})
    );
    $("view-pack").scrollTop = 0;
  }

  function renderAll() {
    const body = $("all-body"), query = $("filter").value.trim().toLowerCase();
    body.replaceChildren();
    let shown = 0;
    for (const family of Object.keys(FAMILY)) {
      const members = nodes.filter(o => o.n.family === family && (!query || o.n.title.toLowerCase().includes(query)));
      if (!members.length) continue;
      shown += members.length;
      put(body, h("p", {class: "group-title c-" + family}, [dot(family), FAMILY[family].label]),
        h("ul", {class: "all"}, members.map(o => h("li", null, h("button", {type: "button", "data-id": o.n.id, "aria-current": S.selected === o.n.id ? "true" : null, on: {click: () => select(o.n.id)}},
          [h("span", {text: o.n.title}), h("small", {text: o.old ? "superseded" : o.edges.length + " threads"})])))));
    }
    if (!shown) put(body, h("p", {class: "empty", text: "No record has that in its title."}));
  }
  function renderAllState() {
    for (const button of $("all-body").querySelectorAll("button[data-id]")) {
      if (button.getAttribute("data-id") === S.selected) button.setAttribute("aria-current", "true"); else button.removeAttribute("aria-current");
    }
  }
  $("filter").addEventListener("input", renderAll);

  // ── Asking ───────────────────────────────────────────────
  function ask(spec) {
    $("ask-note").hidden = true;
    S.selected = null; S.selectedEdge = null; renderRecord(); renderAllState();
    think(spec, false);
    showTab("pack");
    if (stackedQuery.matches) window.scrollTo({top: 0, behavior: reduced ? "auto" : "smooth"});
  }
  function renderScenarioState() {
    const active = S.thought && S.thought.spec.scenarioId;
    for (const button of $("scenarios").children) button.setAttribute("aria-pressed", String(button.getAttribute("data-id") === active));
  }
  for (const scenario of data.scenarios) {
    $("scenarios").append(h("button", {type: "button", class: "scenario", "data-id": scenario.id, "aria-pressed": "false",
      on: {click: () => ask({seedIds: scenario.seedIds, question: scenario.question, why: scenario.why, scenarioId: scenario.id})}},
      [h("b", {text: scenario.title}), h("span", {text: scenario.question})]));
  }
  $("askform").addEventListener("submit", event => {
    event.preventDefault();
    const question = $("ask-input").value.trim();
    if (!question) return;
    const seedIds = model.findSeeds(data, question), note = $("ask-note");
    if (!seedIds.length) {
      note.textContent = "Nothing in this small invented sky matches those words. Try “rollback”, “schema” or “queue”.";
      note.hidden = false;
      return;
    }
    ask({seedIds, question, why: "Started from the records whose titles and text best match your words, then followed their threads outward."});
  });
  $("clear-thought").addEventListener("click", clearThought);

  function rerun() { if (S.thought) { const stage3 = S.thought.spec; think(stage3, true); } }
  $("budget").addEventListener("input", event => { S.budget = Number(event.target.value); $("budget-out").textContent = S.budget + " tokens"; rerun(); });
  $("radius").addEventListener("input", event => { S.radius = Number(event.target.value); $("radius-out").textContent = S.radius + (S.radius === 1 ? " step" : " steps"); rerun(); });
  for (const owner of [...new Set(data.nodes.map(n => n.owner))].sort()) $("owner").append(h("option", {value: owner, text: owner}));
  $("owner").addEventListener("change", event => { S.owner = event.target.value; rerun(); });

  // ── Legend and view tools ────────────────────────────────
  function chipGroup(host, names, kind, labelOf, markClass, titleOf) {
    for (const name of names) {
      host.append(h("button", {type: "button", class: "chip", "aria-pressed": "false", "data-name": name, title: titleOf(name),
        on: {click: () => {
          S[kind] = S[kind] === name ? null : name;
          for (const chip of host.children) chip.setAttribute("aria-pressed", String(chip.getAttribute("data-name") === S[kind]));
          touched();
        }}}, [h("span", {class: markClass + " c-" + name, "aria-hidden": true}), labelOf(name)]));
    }
  }
  chipGroup($("family-chips"), Object.keys(FAMILY).filter(f => nodes.some(o => o.n.family === f)), "family", f => FAMILY[f].label, "dot",
    f => "Show only " + FAMILY[f].label.toLowerCase() + " (" + nodes.filter(o => o.n.family === f).length + ")");
  chipGroup($("pred-chips"), Object.keys(data.predicates).filter(p => edges.some(eo => eo.e.predicate === p)), "predicate", predLabel, "bar",
    p => data.predicates[p].description || predLabel(p));
  $("toggle-labels").addEventListener("click", event => { S.labels = !S.labels; event.currentTarget.setAttribute("aria-pressed", String(S.labels)); touched(); });
  $("toggle-drift").addEventListener("click", event => { S.drift = !S.drift; event.currentTarget.setAttribute("aria-pressed", String(S.drift)); lastInput = -1e6; invalidate(); });
  $("recenter").addEventListener("click", () => { cam.zoomT = 1; cam.tilt = 0.9; cam.tx = cam.ty = cam.tz = 0; touched(); });

  // ── Start ────────────────────────────────────────────────
  if (data.provenance) $("provenance").textContent = data.provenance;
  if (reduced) { $("toggle-drift").setAttribute("aria-pressed", "false"); S.drift = false; }
  reducedQuery.addEventListener("change", event => { reduced = event.matches; invalidate(); });
  window.addEventListener("resize", resize);
  if ("ResizeObserver" in window) new ResizeObserver(resize).observe(stage);
  renderAll();
  resize();
  requestAnimationFrame(frame);
})();
