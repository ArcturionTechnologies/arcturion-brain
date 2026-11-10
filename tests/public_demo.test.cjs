"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const model = require("../demo/model.js");
function fixture() {
  function n(id, status = "current") { return {id, title:id, family:"document",kind:"knowledge",owner:"Reviewer",summary:"Invented reference",excerpt:"Only invented teaching content.",source:"example/" + id + ".md",status,x:100,y:100}; }
  return {version:1,nodes:[n("workflow"),n("decision","superseded"),n("revision"),n("evidence"),n("unrelated")],edges:[
    {from:"workflow",to:"decision",predicate:"cites",explanation:"Invented citation."},
    {from:"revision",to:"decision",predicate:"supersedes",explanation:"Replaces old guidance."},
    {from:"evidence",to:"revision",predicate:"supports",explanation:"A report supports guidance."}
  ],predicates:{cites:{},supersedes:{},supports:{}},scenarios:[]};
}
test("walks incoming relationships while preserving their directed meaning", () => {
  const pack = model.selectContext(fixture(),{seedIds:["workflow"],radius:3,budget:1000});
  const evidence = pack.items.find(item => item.node.id === "evidence");
  assert.ok(evidence);
  assert.deepEqual(evidence.path.map(edge => [edge.from,edge.predicate,edge.to]),[
    ["workflow","cites","decision"],["revision","supersedes","decision"],["evidence","supports","revision"]
  ]);
  assert.equal(evidence.path[1].traversedFrom,"decision");
  assert.equal(evidence.path[1].traversedTo,"revision");
});
test("omits superseded text but can traverse it to a replacement", () => {
  const pack = model.selectContext(fixture(),{seedIds:["workflow"],radius:2,budget:1000});
  assert.ok(pack.items.some(item => item.node.id === "revision"));
  assert.ok(!pack.items.some(item => item.node.id === "decision"));
  assert.ok(!pack.items.some(item => item.node.id === "evidence"));
  assert.equal(pack.superseded,1);
});
test("budget bounds estimated node text and empty budget returns no records", () => {
  const pack = model.selectContext(fixture(),{seedIds:["workflow"],radius:3,budget:40});
  assert.ok(pack.estimatedTokens <= 40);
  assert.equal(pack.estimatedTokens,pack.items.reduce((sum,item)=>sum+item.estimatedTokens,0));
  assert.ok(pack.overBudget > 0);
  assert.equal(model.selectContext(fixture(),{seedIds:["workflow"],radius:3,budget:0}).items.length,0);
});
test("scope cannot invent disconnected records; unknown seeds yield empty pack", () => {
  const pack = model.selectContext(fixture(),{seedIds:["workflow"],radius:3,budget:1000});
  assert.ok(!pack.items.some(item => item.node.id === "unrelated"));
  assert.equal(model.selectContext(fixture(),{seedIds:["missing"],radius:3,budget:1000}).items.length,0);
  assert.deepEqual(model.findSeeds(fixture(),"zzzzunknown"),[]);
});
test("invalid endpoint and fictional-path violations fail validation", () => {
  const data = fixture();
  data.edges[0].to = "missing";
  data.nodes[0].source = "/Users/private.md";
  assert.ok(model.validateData(data).some(error=>error.includes("Unresolved")));
  assert.ok(model.validateData(data).some(error=>error.includes("invented")));
});
test("shipped corpus is valid, invented, bounded and every question produces a pack", () => {
  const context = {window:{}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../demo/data.js"),"utf8"),context);
  const data = JSON.parse(JSON.stringify(context.window.BRAIN_DEMO));
  assert.deepEqual(model.validateData(data),[]);
  assert.ok(data.nodes.length >= 25 && data.nodes.length <= 38);
  assert.ok(data.edges.length >= 40 && data.edges.length <= 65);
  assert.equal(data.scenarios.length,3);
  for (const scenario of data.scenarios) {
    const pack = model.selectContext(data,{seedIds:scenario.seedIds,radius:2,budget:800});
    assert.ok(pack.items.length > 1,scenario.id);
    assert.ok(pack.estimatedTokens <= 800,scenario.id);
  }
});
test("demo publication consists only of reviewed static assets with no runtime network calls", () => {
  const dir = path.join(__dirname,"../demo");
  const expected = ["app.js","data.js","index.html","model.js","styles.css"];
  assert.deepEqual(fs.readdirSync(dir).sort(),expected);
  for (const file of expected) {
    const full = path.join(dir,file);
    assert.ok(fs.lstatSync(full).isFile() && !fs.lstatSync(full).isSymbolicLink(),file);
    const text = fs.readFileSync(full,"utf8");
    assert.ok(!/\/Users\/|127\.0\.0\.1|localhost|brain\.sqlite|\.env\b/i.test(text),file + ": private-service/path marker");
    if (file.endsWith(".js")) {
      assert.ok(!/\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|\bimport\s*\(/.test(text),file + ": runtime network code");
      assert.ok(!/\binnerHTML\b|\bouterHTML\b|\bdocument\.write\b|\beval\s*\(/.test(text),file + ": unsafe content insertion");
    }
  }
  const html = fs.readFileSync(path.join(dir,"index.html"),"utf8");
  assert.match(html,/connect-src\s+'none'/);
  assert.match(html,/default-src\s+'none'/);
  assert.ok(!/https?:\/\/[^"']+["']\s*(?:integrity|crossorigin)/.test(html));
  const srcs = [...html.matchAll(/<script[^>]+src=["']([^"']+)["']/g)].map(m=>m[1].replace(/^\.\//,""));
  assert.deepEqual(srcs,["data.js","model.js","app.js"]);
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>[\s\S]*?\S[\s\S]*?<\/script>/i.test(html),"No inline script");
});
