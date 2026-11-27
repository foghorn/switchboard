// Switchboard Inspector. Every value from a request is inserted with textContent,
// never as HTML: prompts routinely contain markup.
"use strict";

const POLL_MS = 2000;
const KEEP = 2000;
const KEY = "switchboard-inspector-key";

const S = {
  key: null, seq: 0, entries: [], active: [], clients: [], blocks: [],
  selected: null, detail: null, paused: false, timer: null, filterClient: "", capturedOnly: false,
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;  // CSSOM, which the page's CSP allows (style attributes it blocks)
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const fmtInt = (n) => (n === null || n === undefined ? "–" : Math.round(n).toLocaleString());
function fmtMs(ms) {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  return `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`;
}
const fmtTime = (ts) => (ts ? ts.slice(11, 19) : "");
const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "");

function statusPill(e) {
  const s = e.status;
  if (s === 200) return h("span", { class: "pill ok" }, "200");
  if (s === 499) return h("span", { class: "pill warn" }, "499 left");
  return h("span", { class: "pill bad" }, String(s ?? "?"));
}

// --- auth -----------------------------------------------------------------

function loadKey() {
  try { return sessionStorage.getItem(KEY); } catch { return null; }
}
function saveKey(k) {
  try { if (k) sessionStorage.setItem(KEY, k); else sessionStorage.removeItem(KEY); } catch { /* private mode */ }
}

async function api(path, payload) {
  const opts = { headers: { authorization: `Bearer ${S.key}` }, cache: "no-store" };
  if (payload !== undefined) {
    opts.method = "POST";
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(payload);
  }
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const err = new Error((body && body.error) || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return body;
}

function showLogin(message) {
  stopPolling();
  $("app").hidden = true;
  $("controls").hidden = true;
  $("capture-panel").hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !message;
  $("login-error").textContent = message || "";
  $("key").focus();
}

async function signIn(key) {
  S.key = key;
  try {
    const data = await api("/inspector/api/feed?after=0");
    saveKey(key);
    $("login").hidden = true;
    $("app").hidden = false;
    $("controls").hidden = false;
    S.seq = 0; S.entries = [];
    ingest(data);
    startPolling();
    const fromHash = decodeURIComponent(location.hash.slice(1));
    if (fromHash) select(fromHash);
  } catch (e) {
    S.key = null;
    saveKey(null);
    showLogin(e.status === 401 || e.status === 403 ? e.message : `Couldn't reach Switchboard: ${e.message}`);
  }
}

// --- feed -------------------------------------------------------------------

function setConn(state, text) {
  const c = $("conn");
  c.className = `conn ${state}`;
  c.lastElementChild.textContent = text;
}

function ingest(data) {
  $("version").textContent = `v${data.version}`;
  const wasActive = S.active.map((a) => a.id);
  S.active = data.active;
  S.clients = data.clients;
  S.captureAll = data.capture_all;
  S.blocks = data.blocks;
  renderCapture();
  if (data.entries.length) {
    S.entries.push(...data.entries);
    if (S.entries.length > KEEP) S.entries.splice(0, S.entries.length - KEEP);
  }
  S.seq = data.seq;
  renderClientFilter();
  renderList();
  // The selected request just finished: load its final view once.
  if (S.selected && wasActive.includes(S.selected) && !S.active.some((a) => a.id === S.selected)) {
    loadDetail(S.selected);
  } else if (S.selected && S.detail && S.detail.state === "active") {
    const live = S.active.find((a) => a.id === S.selected);
    if (live) { S.detail.entry = live; renderDetail(); }
  }
}

async function poll() {
  if (S.paused || document.hidden) return;
  try {
    ingest(await api(`/inspector/api/feed?after=${S.seq}`));
    setConn("live", "Live");
  } catch (e) {
    if (e.status === 401 || e.status === 403) { saveKey(null); showLogin(e.message); return; }
    if (e.status === 404) { setConn("down", "Inspector turned off"); return; }
    setConn("down", "Can't reach Switchboard");
  }
}
function startPolling() { stopPolling(); S.timer = setInterval(poll, POLL_MS); setConn("live", "Live"); }
function stopPolling() { if (S.timer) clearInterval(S.timer); S.timer = null; }

function renderClientFilter() {
  const sel = $("client-filter");
  const names = [...new Set([...S.clients.map((c) => c.name), ...S.entries.map((e) => e.client)])].sort();
  const have = [...sel.options].map((o) => o.value).slice(1);
  if (have.join("|") === names.join("|")) return;
  sel.replaceChildren(h("option", { value: "" }, "All clients"));
  for (const n of names) {
    const c = S.clients.find((x) => x.name === n);
    sel.append(h("option", { value: n }, c && c.capture ? `${n} (capturing)` : n));
  }
  sel.value = names.includes(S.filterClient) ? S.filterClient : "";
}

function visible(e) {
  if (S.filterClient && e.client !== S.filterClient) return false;
  if (S.capturedOnly && !(e.capture || e.captured)) return false;
  return true;
}

function row(e, isActive) {
  const li = h("li", { tabindex: "0", "data-id": e.id, class: e.id === S.selected ? "sel" : null,
    onclick: () => select(e.id), onkeydown: (ev) => { if (ev.key === "Enter") select(e.id); } });
  const captured = e.capture || e.captured;
  li.append(h("div", { class: "r1" },
    captured ? h("span", { class: "dot", title: "Captured: full prompt available" }) : null,
    h("span", { class: "who" }, e.client),
    h("span", { class: "when" }, fmtTime(e.ts))));
  const tokens = e.tokens_in !== undefined ? `${fmtInt(e.tokens_in)} in / ${fmtInt(e.tokens_out)} out` : null;
  if (isActive) {
    const phase = e.phase === "streaming" ? "answering" : e.phase === "waiting" ? "waiting for first token" : "starting";
    li.append(h("div", { class: "r2" },
      h("span", { class: "pill info pulse" }, e.block || "…"),
      h("span", null, phase),
      h("span", { class: "num" }, fmtMs(e.elapsed_ms)),
      h("span", { class: "num" }, `~${fmtInt(e.est_tokens)} tok`)));
  } else {
    const failed = (e.attempts || []).filter((a) => a.error && a.error !== "client disconnected").length;
    li.append(h("div", { class: "r2" },
      statusPill(e),
      h("span", null, e.served_by || "no block"),
      failed ? h("span", { class: "pill warn" }, `${failed} failover${failed > 1 ? "s" : ""}`) : null,
      h("span", { class: "num" }, fmtMs(e.ms)),
      tokens ? h("span", { class: "num" }, tokens) : null));
  }
  return li;
}

function renderList() {
  const act = S.active.filter(visible);
  $("active-wrap").hidden = act.length === 0;
  $("active").replaceChildren(...act.map((e) => row(e, true)));
  const fin = S.entries.filter(visible).slice(-500).reverse();
  $("finished").replaceChildren(...fin.map((e) => row(e, false)));
  $("count").textContent = fin.length ? `(${fin.length})` : "";
  $("empty").hidden = fin.length + act.length > 0;
}

// --- capture switches ---------------------------------------------------------

function renderCapture() {
  const on = S.clients.filter((c) => c.capture).length;
  const btn = $("capture-btn");
  btn.textContent = S.captureAll ? "Capture: everything" : `Capture: ${on} of ${S.clients.length}`;
  btn.classList.toggle("on", S.captureAll || on > 0);
  if (document.activeElement && $("capture-panel").contains(document.activeElement)
      && document.activeElement.type === "checkbox") return;  // don't redraw under the cursor
  $("capture-all").checked = !!S.captureAll;
  $("capture-clients").replaceChildren(...S.clients.map((c) => {
    const box = h("input", { type: "checkbox", id: `cap-${c.name}`, disabled: S.captureAll || null });
    box.checked = c.capture;
    box.addEventListener("change", () => setCapture(c.name, box.checked));
    const src = S.captureAll ? "via capture everything"
      : c.override === null || c.override === undefined ? "from switchboard.yml" : "set here";
    return h("li", null,
      h("label", { for: `cap-${c.name}` }, box, h("span", null, c.name)),
      h("span", { class: "src" }, src),
      c.override !== null && c.override !== undefined && !S.captureAll
        ? h("button", { type: "button", class: "linkbtn", onclick: () => setCapture(c.name, null),
            title: `Go back to ${c.name}'s setting in switchboard.yml (${c.config ? "on" : "off"})` }, "reset")
        : null);
  }));
}

async function setCapture(client, on) {
  $("capture-error").hidden = true;
  try {
    await api("/inspector/api/capture", { client, on });
  } catch (e) {
    $("capture-error").textContent = `Couldn't change capture: ${e.message}`;
    $("capture-error").hidden = false;
  }
  if (document.activeElement) document.activeElement.blur();
  const keep = S.paused;
  S.paused = false;
  await poll();
  S.paused = keep;
}

// --- detail -----------------------------------------------------------------

function select(id) {
  S.selected = id;
  history.replaceState(null, "", `#${encodeURIComponent(id)}`);
  for (const li of document.querySelectorAll(".rows li")) li.classList.toggle("sel", li.dataset.id === id);
  loadDetail(id);
}

async function loadDetail(id) {
  try {
    const d = await api(`/inspector/api/request/${encodeURIComponent(id)}`);
    if (S.selected !== id) return;
    S.detail = d;
    renderDetail();
  } catch (e) {
    if (S.selected !== id) return;
    S.detail = null;
    $("detail").replaceChildren(h("div", { class: "placeholder" },
      h("h2", null, "Can't show that request"), h("p", { class: "muted" }, e.message)));
  }
}

function renderDetail() {
  const d = S.detail;
  const e = d.entry;
  const root = h("div");
  const active = d.state === "active";
  const where = active ? (e.block || "choosing a block") : (e.served_by || "no block answered");
  root.append(h("div", { class: "dhead" },
    h("h2", null, `${e.client} → ${where}`),
    h("div", { class: "sub" }, `${e.ts} · route ${e.route} · ${e.api} API${e.stream ? " · streaming" : ""} · id ${e.id}`
      + (e.capture ? ` · capture ${e.capture}` : ""))));

  const facts = h("div", { class: "facts" });
  const fact = (v, label) => facts.append(h("div", { class: "fact" }, h("b", null, v), h("span", null, label)));
  if (active) {
    fact(fmtMs(e.elapsed_ms), "elapsed so far");
    fact(e.phase === "streaming" ? "answering" : "waiting", "status");
    if (e.ttft_ms !== undefined) fact(fmtMs(e.ttft_ms), "time to first token");
    fact(`~${fmtInt(e.est_tokens)}`, "prompt tokens (estimate)");
  } else {
    facts.append(h("div", { class: "fact" }, h("b", null, statusPill(e)), h("span", null, e.note || "status")));
    fact(fmtMs(e.ms), "total time");
    if (e.ttft_ms !== undefined) fact(fmtMs(e.ttft_ms), "time to first token");
    if (e.tokens_in !== undefined) fact(fmtInt(e.tokens_in), "input tokens");
    if (e.tokens_out !== undefined) fact(fmtInt(e.tokens_out), "output tokens");
    if (e.tokens_out && e.ttft_ms !== undefined && e.ms > e.ttft_ms) fact(
      `${(e.tokens_out / ((e.ms - e.ttft_ms) / 1000)).toFixed(1)}`, "output tokens/s");
    if (e.cost_usd) fact(`$${e.cost_usd.toFixed(4)}`, "cost");
  }
  root.append(facts);
  root.append(timeline(e, active));

  if (d.anatomy) {
    root.append(composition(d.anatomy, active));
    if (d.anatomy.tool_groups.length) root.append(toolsSection(d.anatomy));
    if (d.anatomy.documents.length) root.append(docsSection(d.anatomy));
    root.append(messagesSection(d.anatomy));
    if (d.response) root.append(responseSection(d.response));
    if (!active) root.append(rawSection(e, d.upstream_blocks || []));
  } else if (d.capture_missing) {
    root.append(h("div", { class: "sec notice" }, "This request was captured, but the capture file is gone (deleted by the retention settings, or the volume was reset)."));
  } else {
    const c = S.clients.find((x) => x.name === e.client);
    root.append(h("div", { class: "sec notice" },
      h("p", null, h("strong", null, "The prompt for this request wasn't captured. "),
        c && c.capture
          ? "Capture is on for this client now; its next requests will show the full breakdown."
          : `To see what ${e.client} sends, set capture: true on its entry in switchboard.yml. Switchboard picks the change up within a few seconds, and that client's next requests will show here in full.`)));
  }
  $("detail").replaceChildren(root);
}

function timeline(e, active) {
  const sec = h("div", { class: "sec" }, h("h3", null, "Where the time went"));
  const attempts = (e.attempts || []).filter((a) => !a.skipped);
  const skipped = (e.attempts || []).filter((a) => a.skipped);
  const failedMs = attempts.filter((a) => a.error).reduce((t, a) => t + (a.ms || 0), 0);
  const total = Math.max(active ? e.elapsed_ms : e.ms, 1);
  const tl = h("div", { class: "timeline" });
  let offset = 0;
  for (const a of attempts.filter((x) => x.error)) {
    const w = ((a.ms || 0) / total) * 100;
    tl.append(h("div", { class: "tl-row" }, h("span", { class: "lbl", title: a.block }, a.block),
      h("div", { class: "track" }, h("div", { class: "seg fail", style: `left:${(offset / total) * 100}%;width:${w}%` }))));
    tl.append(h("div", { class: "tl-note" }, `Failed after ${fmtMs(a.ms)}: ${a.error}`));
    offset += a.ms || 0;
  }
  const served = active ? e.block : e.served_by;
  if (served) {
    const start = (offset / total) * 100;
    const wait = e.ttft_ms !== undefined ? e.ttft_ms : (active ? total - failedMs : null);
    const track = h("div", { class: "track" });
    if (wait !== null) {
      const ww = Math.min((wait / total) * 100, 100 - start);
      track.append(h("div", { class: `seg wait${active && e.phase !== "streaming" ? " pulse" : ""}`, style: `left:${start}%;width:${ww}%` }));
      if (!active || e.phase === "streaming") {
        track.append(h("div", { class: `seg gen${active ? " pulse" : ""}`, style: `left:${start + ww}%;width:${Math.max(100 - start - ww, 0)}%` }));
      }
    } else {
      track.append(h("div", { class: "seg gen", style: `left:${start}%;width:${100 - start}%` }));
    }
    tl.append(h("div", { class: "tl-row" }, h("span", { class: "lbl", title: served }, served), track));
  }
  sec.append(tl);
  sec.append(h("div", { class: "legend-inline" },
    h("span", null, h("i", { class: "seg-key", style: "background:var(--bad)" }), "failed attempt"),
    h("span", null, h("i", { style: "background:var(--other)" }), "waiting for first token (prompt read)"),
    h("span", null, h("i", { style: "background:var(--ok)" }), e.stream ? "answering" : "whole request (not streamed)")));
  if (skipped.length) {
    sec.append(h("p", { class: "muted", style: "margin-top:8px" },
      "Skipped: " + skipped.map((a) => `${a.block} (${a.skipped})`).join("; ")));
  }
  return sec;
}

function composition(an, active) {
  const total = an.segments.reduce((t, s) => t + s.tokens, 0) || 1;
  const sec = h("div", { class: "sec" },
    h("h3", null, `What the prompt is made of: ${active || !an.tokens_measured ? "~" : ""}${fmtInt(an.tokens)} tokens`),
    h("p", { class: "muted" }, an.tokens_measured
      ? "Token counts per part share out the upstream's reported input tokens by each part's size, so they're estimates."
      : "No token count from the upstream yet; these assume about 4 characters per token."));
  const bar = h("div", { class: "stack", role: "img", "aria-label": an.segments.map((s) => `${s.label} ${pct(s.tokens, total)}`).join(", ") });
  for (const s of an.segments) bar.append(h("div", { class: `c-${s.key}`, style: `flex:${s.chars}`, title: `${s.label}: ~${fmtInt(s.tokens)} tokens` }));
  const legend = h("div", { class: "legend" });
  for (const s of an.segments) {
    legend.append(h("div", null, h("i", { class: `c-${s.key}` }), h("span", null, s.label),
      h("span", { class: "v" }, `~${fmtInt(s.tokens)} · ${pct(s.tokens, total)}`)));
  }
  sec.append(bar, legend);
  return sec;
}

function toolsSection(an) {
  const max = Math.max(...an.tool_groups.map((g) => g.chars), 1);
  const sec = h("div", { class: "sec" }, h("h3", null, `Tool definitions: ${an.tool_count} tools`),
    h("p", { class: "muted" }, "Grouped by name prefix (an MCP server's tools share one). Click a group to see its tools. Shared schema definitions ($defs) copied into each tool are a common cause of bloat."));
  const tbody = h("tbody");
  for (const g of an.tool_groups) {
    const caret = h("span", { class: "caret" }, "▸");
    const subs = g.tools.map((t) => h("tr", { class: "sub", hidden: true },
      h("td", null, t.name),
      h("td", { class: "r" }, `~${fmtInt(t.tokens)}`),
      h("td", { class: "r" }, fmtInt(t.description_chars)),
      h("td", { class: "r" }, fmtInt(t.schema_chars)),
      h("td", { class: "r" }, t.defs_chars ? fmtInt(t.defs_chars) : "–")));
    const tr = h("tr", { class: "grp", tabindex: "0",
      onclick: () => { const open = subs[0] && subs[0].hidden; subs.forEach((s) => { s.hidden = !open; }); caret.textContent = open ? "▾" : "▸"; },
      onkeydown: (ev) => { if (ev.key === "Enter") ev.currentTarget.click(); } },
      h("td", null, caret, `${g.name} `, h("span", { class: "muted" }, `(${g.count})`)),
      h("td", { class: "r" }, `~${fmtInt(g.tokens)}`),
      h("td", { colspan: "3" }, h("div", { class: "bar", style: `width:${(g.chars / max) * 100}%` })));
    tbody.append(tr, ...subs);
  }
  sec.append(h("div", { class: "tbl" }, h("table", null,
    h("thead", null, h("tr", null, h("th", null, "Group / tool"), h("th", { class: "r" }, "Tokens"),
      h("th", { class: "r" }, "Description chars"), h("th", { class: "r" }, "Schema chars"), h("th", { class: "r" }, "$defs chars"))),
    tbody)));
  return sec;
}

function docsSection(an) {
  const sec = h("div", { class: "sec" }, h("h3", null, `Documents: ${an.documents.length}`),
    h("p", { class: "muted" }, "Files the client put into the prompt (RAG <source> blocks and document attachments). Compare the size with the original file to tell whole documents from retrieved chunks."));
  sec.append(h("div", { class: "tbl" }, h("table", null,
    h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Where"), h("th", { class: "r" }, "Characters"), h("th", { class: "r" }, "Tokens"))),
    h("tbody", null, an.documents.map((d) => h("tr", null, h("td", { class: "mono" }, d.name), h("td", null, d.where),
      h("td", { class: "r" }, fmtInt(d.chars)), h("td", { class: "r" }, `~${fmtInt(d.tokens)}`)))))));
  return sec;
}

const CAT_LABEL = { system: "system", history: "earlier", latest: "latest", turn: "this turn" };

function messagesSection(an) {
  const msgs = an.messages;
  const sec = h("div", { class: "sec" }, h("h3", null, `Messages: ${msgs.filter((m) => m.index !== null).length}`));
  for (const m of msgs) {
    const preview = m.text.replace(/\s+/g, " ").slice(0, 160) ||
      (m.tool_calls.length ? `calls ${m.tool_calls.map((c) => c.name).join(", ")}` : "(empty)");
    const det = h("details", { class: `msg b-${m.category}` },
      h("summary", null,
        h("span", { class: "role" }, m.index === null ? "system" : `${m.index} · ${m.role}`),
        h("span", { class: "chip" }, CAT_LABEL[m.category] || m.category),
        h("span", { class: "prev" }, preview),
        h("span", { class: "size" }, `~${fmtInt(m.tokens)} tok`)));
    det.addEventListener("toggle", () => {
      if (!det.open || det.dataset.filled) return;
      det.dataset.filled = "1";
      const body = h("div", { class: "body" });
      if (m.documents.length) body.append(h("div", { class: "chips" }, m.documents.map((n) => h("span", { class: "chip" }, `document: ${n}`))));
      if (m.tool_call_id) body.append(h("div", { class: "chips" }, h("span", { class: "chip" }, `result for ${m.tool_call_id}`)));
      if (m.text) body.append(h("pre", null, m.text));
      if (m.tool_calls.length) body.append(h("ul", { class: "calls" }, m.tool_calls.map((c) =>
        h("li", null, h("strong", null, c.name), " ", h("code", null, c.arguments || "")))));
      det.append(body);
    });
    sec.append(det);
  }
  const params = Object.keys(an.params || {});
  if (params.length) sec.append(h("details", { class: "msg" }, h("summary", null, h("span", { class: "role" }, "parameters"),
    h("span", { class: "prev" }, params.join(", "))), h("div", { class: "body" }, h("pre", null, JSON.stringify(an.params, null, 2)))));
  return sec;
}

function responseSection(r) {
  const sec = h("div", { class: "sec" }, h("h3", null, `Response${r.finish ? ` (${r.finish})` : ""}`));
  if (r.reasoning) sec.append(h("details", { class: "msg" }, h("summary", null, h("span", { class: "role" }, "reasoning"),
    h("span", { class: "prev" }, r.reasoning.replace(/\s+/g, " ").slice(0, 160))), h("div", { class: "body" }, h("pre", null, r.reasoning))));
  if (r.text) sec.append(h("pre", null, r.text));
  if (r.tool_calls.length) sec.append(h("ul", { class: "calls" }, r.tool_calls.map((c) =>
    h("li", null, h("strong", null, c.name), " ", h("code", null, c.arguments || "")))));
  if (!r.text && !r.tool_calls.length && !r.reasoning) sec.append(h("p", { class: "muted" }, "(empty)"));
  return sec;
}

function rawSection(e, blocks) {
  const out = h("div");
  const show = async (label, path) => {
    out.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    try {
      const data = await api(path);
      out.replaceChildren(h("p", { class: "muted" }, label), h("pre", null, JSON.stringify(data, null, 2)));
    } catch (err) {
      out.replaceChildren(h("p", { class: "error" }, err.message));
    }
  };
  const base = `/inspector/api/request/${encodeURIComponent(e.id)}/raw`;
  return h("div", { class: "sec" }, h("h3", null, "Raw JSON"),
    h("div", { class: "rawbar" },
      h("button", { type: "button", class: "btn", onclick: () => show("The whole capture, as the client sent it.", base) }, "Whole capture"),
      blocks.map((b) => h("button", { type: "button", class: "btn",
        onclick: () => show(`Exactly what ${b} was sent.`, `${base}?block=${encodeURIComponent(b)}`) }, `As sent to ${b}`))),
    out);
}

// --- wiring -----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", (ev) => { ev.preventDefault(); signIn($("key").value.trim()); });
  $("signout").addEventListener("click", () => { saveKey(null); S.key = null; showLogin(); });
  $("pause").addEventListener("click", () => {
    S.paused = !S.paused;
    $("pause").textContent = S.paused ? "Resume" : "Pause";
    setConn(S.paused ? "paused" : "live", S.paused ? "Paused" : "Live");
    if (!S.paused) poll();
  });
  $("capture-btn").addEventListener("click", () => {
    const p = $("capture-panel");
    p.hidden = !p.hidden;
    $("capture-btn").setAttribute("aria-expanded", String(!p.hidden));
  });
  $("capture-all").addEventListener("change", (ev) => setCapture(null, ev.target.checked));
  $("client-filter").addEventListener("change", (ev) => { S.filterClient = ev.target.value; renderList(); });
  $("captured-only").addEventListener("change", (ev) => { S.capturedOnly = ev.target.checked; renderList(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  const k = loadKey();
  if (k) signIn(k); else showLogin();
});
