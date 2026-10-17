// index.js — Petabyte Render bridge plugin for Premiere Pro (UXP, official `premierepro` API).
// Polls the local bridge (premiere_adapter.py, 127.0.0.1:47811) for one verb at a time and reports
// results. The token comes from pb_token.txt in this plugin's own data folder, written by the
// adapter before Premiere launches. No buyer string is ever evaluated; everything is data.
/* global require, fetch, document, setTimeout */
const ppro = require("premierepro");
const uxp = require("uxp");

const BASE = "http://127.0.0.1:47811";
const OPEN_OPTS = { showConvertProjectDialog: false, showLocateFileDialog: false, showWarningDialog: false,
                    addToMRUList: false };
let token = null, project = null, projectPath = null, started = false;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const status = (t) => { const el = document.getElementById("s"); if (el) el.textContent = String(t); };

async function readToken() {
  const dir = await uxp.storage.localFileSystem.getDataFolder();
  const f = await dir.getEntry("pb_token.txt");
  return (await f.read()).trim();
}

async function api(path, body) {
  const r = await fetch(BASE + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-PB-Token": token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 204) return null;
  if (!r.ok) throw new Error("bridge " + r.status);
  return r.json();
}

const plog = (m) => api("/log", { message: String(m).slice(0, 300) }).catch(() => {});

async function openProject(path) {
  if (project && projectPath === path) return project;
  project = await ppro.Project.open(path, OPEN_OPTS);
  if (!project) throw new Error("project_open_failed");
  projectPath = path;
  return project;
}

const ticksOf = (t) => {
  if (t === null || t === undefined) return null;
  if (typeof t === "string" || typeof t === "number") return String(t);
  if (t.ticks !== undefined) return String(t.ticks);
  if (t.ticksNumber !== undefined) return String(t.ticksNumber);
  return String(t);
};

async function describe(seq) {
  const size = await seq.getFrameSize();
  const tb = await seq.getTimebase();
  const end = await seq.getEndTime();
  let has_audio = null;
  try {
    const n = await seq.getAudioTrackCount();
    has_audio = false;
    for (let i = 0; i < n && !has_audio; i++) {
      const track = await seq.getAudioTrack(i);
      const items = await track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
      if (items && items.length) has_audio = true;
    }
  } catch (e) { has_audio = null; }
  return { guid: seq.guid.toString(), name: seq.name, width: size.width, height: size.height,
           timebase_ticks: ticksOf(tb), end_ticks: ticksOf(end), has_audio };
}

async function offlineItems(p, out, limitations) {
  try {
    const root = await p.getRootItem();
    const walk = async (folder, depth) => {
      if (depth > 32 || out.length >= 200) return;
      const items = await folder.getItems();
      for (const it of items) {
        if (typeof it.getItems === "function") { await walk(it, depth + 1); continue; }
        if (typeof it.isOffline === "function" && await it.isOffline()) out.push(String(it.name).slice(0, 120));
      }
    };
    await walk(root, 0);
  } catch (e) {
    limitations.push("offline-media check unavailable in this host (" + String(e && e.message || e).slice(0, 60) + ")");
  }
}

function eventNames() {
  const EM = ppro.EncoderManager;
  if (!ppro.EventManager || !EM || !EM.EVENT_RENDER_COMPLETE) return null;
  return { progress: EM.EVENT_RENDER_PROGRESS, complete: EM.EVENT_RENDER_COMPLETE,
           error: EM.EVENT_RENDER_ERROR, cancel: EM.EVENT_RENDER_CANCEL };
}

async function inspect(job) {
  const p = await openProject(job.project);
  const seqs = await p.getSequences();
  const out = { host_version: uxp.host.version, sequences: [], offline: [], limitations: [], events: !!eventNames() };
  for (const s of seqs.slice(0, 500)) out.sequences.push(await describe(s));
  await offlineItems(p, out.offline, out.limitations);
  return out;
}

function subscribe(names, id) {
  let resolve;
  const done = new Promise((r) => { resolve = r; });
  const subs = [];
  const on = (name, fn) => {
    if (!name) return;
    subs.push([name, fn]);
    ppro.EventManager.addGlobalEventListener(name, fn);
  };
  on(names.progress, (e) => {
    let pct = e && (e.progress !== undefined ? e.progress : e.percent);
    if (typeof pct === "number") pct = pct <= 1 ? pct * 100 : pct;
    api("/progress", { id, percent: typeof pct === "number" ? pct : null }).catch(() => {});
  });
  on(names.complete, () => resolve("complete"));
  on(names.error, () => resolve("error"));
  on(names.cancel, () => resolve("cancel"));
  return { done, off() { for (const [n, f] of subs) { try { ppro.EventManager.removeGlobalEventListener(n, f); } catch (e) {} } } };
}

async function render(job) {
  const p = await openProject(job.project);
  const seqs = await p.getSequences();
  const seq = seqs.find((s) => s.guid.toString().replace(/[{}]/g, "") === String(job.guid).replace(/[{}]/g, ""));
  if (!seq) throw new Error("sequence_not_found");
  let exportFull = true;
  if (job.range) {
    const tb = BigInt(ticksOf(await seq.getTimebase()));
    const inT = ppro.TickTime.createWithTicks(String(tb * BigInt(job.range.start)));
    const outT = ppro.TickTime.createWithTicks(String(tb * BigInt(job.range.end + 1)));
    const setRange = () => p.executeTransaction((cu) => {
      cu.addAction(seq.createSetInPointAction(inT));
      cu.addAction(seq.createSetOutPointAction(outT));
    }, "Petabyte range");
    if (typeof p.lockedAccess === "function") await p.lockedAccess(setRange); else await setRange();
    exportFull = false;
  }
  const em = await ppro.EncoderManager.getManager();
  const names = eventNames();
  const sub = names ? subscribe(names, job.id) : null;
  try {
    const accepted = await em.exportSequence(seq, ppro.Constants.ExportType.IMMEDIATELY, job.output, job.preset, exportFull);
    if (accepted === false) throw new Error("export_rejected");
    let state = "resolved";                      // promise alone is NOT completion; python verifies the file
    if (sub) {
      state = await sub.done;
      if (state !== "complete") throw new Error("export_" + state);
    }
    return { accepted: accepted, events: !!sub, status: state };
  } finally {
    if (sub) sub.off();
  }
}

async function loop() {
  if (started) return;
  started = true;
  for (;;) {
    try {
      if (!token) { token = await readToken(); await api("/hello", { host_version: uxp.host.version, plugin: "1.0.0" }); }
      const job = await api("/job");
      if (job) {
        status("job " + job.id + " " + job.verb);
        let res;
        try {
          const fn = job.verb === "inspect" ? inspect : job.verb === "render" ? render : null;
          if (!fn) throw new Error("unknown_verb");
          res = { id: job.id, ok: true, result: await fn(job) };
        } catch (e) {
          res = { id: job.id, ok: false, error: String(e && e.message ? e.message : e).slice(0, 300) };
        }
        await api("/result", res);
        status("idle");
      }
    } catch (e) {
      status("bridge: " + String(e && e.message || e));
      token = null;                              // re-read the token / re-handshake when the bridge returns
    }
    await sleep(500);
  }
}

uxp.entrypoints.setup({
  plugin: { create() { loop().catch((e) => status("fatal: " + e)); }, destroy() {} },
  panels: { pbpanel: { create() { loop().catch((e) => status("fatal: " + e)); }, show() {}, hide() {}, destroy() {} } },
});
