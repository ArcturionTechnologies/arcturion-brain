/* A deterministic teaching model over invented records; not the Python Brain engine. */
(function (root) {
  "use strict";
  const families = new Set(["agent", "document", "code", "automation", "concept"]);
  function estimateTokens(text) { return Math.max(1, Math.ceil(String(text || "").length / 4)); }
  function validateData(data) {
    const errors = [];
    if (!data || data.version !== 1 || !Array.isArray(data.nodes) || !Array.isArray(data.edges)) return ["Invalid demonstration schema"];
    const ids = new Set();
    for (const node of data.nodes) {
      if (!node.id || ids.has(node.id)) errors.push("Missing or duplicate node id: " + node.id);
      ids.add(node.id);
      if (!families.has(node.family)) errors.push("Unknown family: " + node.id);
      for (const key of ["title", "kind", "owner", "summary", "excerpt", "source"]) {
        if (typeof node[key] !== "string" || !node[key].trim()) errors.push("Missing " + key + ": " + node.id);
      }
      if (!["current", "superseded"].includes(node.status)) errors.push("Invalid status: " + node.id);
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y) || node.x < 0 || node.x > 1000 || node.y < 0 || node.y > 650) errors.push("Invalid position: " + node.id);
      if (typeof node.source !== "string" || !/^example\/[a-z0-9/_.-]+$/.test(node.source) || node.source.split("/").some(segment => !segment || segment === "." || segment === "..")) errors.push("Source must be an invented example path: " + node.id);
    }
    const triples = new Set();
    for (const edge of data.edges) {
      const triple = JSON.stringify([edge.from, edge.predicate, edge.to]);
      if (!ids.has(edge.from) || !ids.has(edge.to)) errors.push("Unresolved edge: " + triple);
      if (triples.has(triple)) errors.push("Duplicate edge: " + triple);
      triples.add(triple);
      if (!data.predicates || !Object.hasOwn(data.predicates, edge.predicate)) errors.push("Undeclared predicate: " + edge.predicate);
      if (typeof edge.explanation !== "string" || !edge.explanation.trim()) errors.push("Missing edge explanation: " + triple);
    }
    for (const scenario of data.scenarios || []) {
      if (!Array.isArray(scenario.seedIds) || !scenario.seedIds.length || scenario.seedIds.some(id => !ids.has(id))) errors.push("Invalid scenario seed: " + scenario.id);
    }
    return errors;
  }
  function findSeeds(data, query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return [];
    const stop = new Set(["what", "which", "the", "and", "for", "how", "does", "can", "before", "this", "with"]);
    const words = q.split(/[^a-z0-9]+/).filter(w => w.length > 1 && !stop.has(w));
    return data.nodes.map(node => {
      const title = node.title.toLowerCase(), body = (node.summary + " " + node.excerpt).toLowerCase();
      const score = (title.includes(q) ? 30 : 0) + words.reduce((sum, w) => sum + (title.includes(w) ? 4 : 0) + (body.includes(w) ? 1 : 0), 0);
      return {id: node.id, score};
    }).filter(row => row.score > 0).sort((a, b) => b.score - a.score || a.id.localeCompare(b.id)).slice(0, 3).map(row => row.id);
  }
  function selectContext(data, options) {
    const problems = validateData(data);
    if (problems.length) throw new Error(problems.join("; "));
    const opts = options || {};
    const radius = Math.min(3, Math.max(0, Math.floor(Number(opts.radius) || 0)));
    const budget = Math.max(0, Math.floor(Number(opts.budget) || 0));
    const byId = new Map(data.nodes.map(n => [n.id, n]));
    const seeds = [...new Set(opts.seedIds || [])].filter(id => byId.has(id)).sort();
    const adjacent = new Map(data.nodes.map(n => [n.id, []]));
    for (const edge of data.edges) {
      adjacent.get(edge.from).push({edge, next: edge.to});
      adjacent.get(edge.to).push({edge, next: edge.from});
    }
    for (const list of adjacent.values()) list.sort((a, b) => a.next.localeCompare(b.next) || a.edge.predicate.localeCompare(b.edge.predicate));
    const visited = new Map(seeds.map(id => [id, {hops: 0, path: []}]));
    const queue = seeds.slice();
    for (let pos = 0; pos < queue.length; pos++) {
      const from = queue[pos], previous = visited.get(from);
      if (previous.hops >= radius) continue;
      for (const {edge, next} of adjacent.get(from)) {
        if (visited.has(next)) continue;
        visited.set(next, {hops: previous.hops + 1, path: previous.path.concat([{from: edge.from, to: edge.to, predicate: edge.predicate, traversedFrom: from, traversedTo: next}])});
        queue.push(next);
      }
    }
    const candidates = [];
    let superseded = 0;
    for (const [id, reach] of visited) {
      const node = byId.get(id);
      if (node.status === "superseded") { superseded++; continue; }
      const score = (reach.hops === 0 ? 1000 : 100 / (reach.hops + 1)) + (opts.owner && node.owner === opts.owner ? 24 : 0) + (node.kind === "memory" ? 5 : 0);
      const estimatedTokens = estimateTokens(node.title + "\n" + node.summary + "\n" + node.excerpt);
      candidates.push({node, score, hops: reach.hops, path: reach.path, estimatedTokens});
    }
    candidates.sort((a, b) => b.score - a.score || a.node.id.localeCompare(b.node.id));
    const items = [];
    let estimatedTokens = 0, overBudget = 0;
    for (const item of candidates) {
      if (estimatedTokens + item.estimatedTokens > budget) { overBudget++; continue; }
      items.push(item);
      estimatedTokens += item.estimatedTokens;
    }
    const selected = new Set(items.map(item => item.node.id));
    return {
      items, edges: data.edges.filter(e => selected.has(e.from) && selected.has(e.to)),
      estimatedTokens, omitted: overBudget + superseded, considered: visited.size,
      overBudget, superseded,
      reach: [...visited].map(([id, r]) => ({id, hops: r.hops, via: r.path.length ? r.path[r.path.length - 1] : null}))
    };
  }
  const api = Object.freeze({estimateTokens, validateData, findSeeds, selectContext});
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.BrainDemoModel = api;
})(typeof window !== "undefined" ? window : null);
