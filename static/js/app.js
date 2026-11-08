// FileFlip front-end: picking files, choosing a format, running conversions and PDF
// tools, and showing the results.

import { createEngine } from "./engine.js?v=1.1.0";
import { makeZip } from "./zip.js?v=1.1.0";

const $ = (id) => document.getElementById(id);

const BADGE = { pdf: "PDF", docx: "DOC", pptx: "PPT", xlsx: "XLS", csv: "CSV", txt: "TXT", md: "MD", html: "HTML", image: "IMG", jpg: "JPG", png: "PNG", webp: "WEBP", zip: "ZIP" };
const TOOL_VERB = { merge: "Merge", split: "Split", rotate: "Rotate", compress: "Compress" };
const TOOL_HINT = {
  merge: "Add two or more PDFs, then put them in order.",
  split: "Add one PDF to split into separate files.",
  rotate: "Add PDFs to rotate (all pages, or the ones you choose).",
  compress: "Add PDFs to make them smaller (mostly by shrinking pictures).",
};
const HINTS = {
  "pdf>docx": "Keeps headings, paragraphs, lists, tables and pictures so you can edit the text. Scanned PDFs have no text to extract.",
  "pdf>txt": "Just the text, ready to paste anywhere.",
  "pdf>md": "Text with headings, lists and tables in Markdown.",
  "docx>pdf": "Headings, lists, tables and pictures are kept. Fonts and spacing may differ slightly from Word.",
  "pptx>pdf": "Each slide becomes a page of notes: its title, text, pictures, tables and speaker notes.",
  "pptx>docx": "Turns your slides into editable study notes.",
  "xlsx>csv": "One CSV file per sheet.",
  "xlsx>pdf": "Every sheet as a neat table.",
  "csv>xlsx": "Numbers become real numbers, so formulas work.",
  "image>pdf": "Each picture on its own page, sized to fit.",
  "image>docx": "Pictures placed in a Word document.",
};

const state = {
  mode: "convert",
  engine: null,
  formats: null,
  convertFiles: [], // { id, file, kind, error }
  toolFiles: [], // { id, file, error }
  target: null,
  preferred: null, // { from, to } from a popular-conversion button
  results: [], // { name, kind, status, info, blob, url, error }
  busy: false,
  nextId: 1,
};
const engineReady = createEngine().then(async (engine) => {
  state.engine = engine;
  try {
    state.formats = await engine.formats();
  } catch {
    state.formats = null;
  }
  return engine;
});

// ------------------------------------------------------------------ helpers
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function extOf(name) {
  const m = /\.([^.]+)$/.exec(name.toLowerCase());
  return m ? `.${m[1]}` : "";
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3200);
}

function setStatus(kind, message) {
  for (const id of ["engineStatus", "engineStatus2"]) {
    const el = $(id);
    el.hidden = !message;
    el.className = `engine-status ${kind}`;
    el.textContent = message;
  }
}

function showError(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}

const ICON_X = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
const ICON_UP = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m18 15-6-6-6 6"/></svg>';
const ICON_DOWN = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

function kindOf(file) {
  const f = state.formats;
  const ext = extOf(file.name);
  if (f?.legacy?.[ext]) return { kind: null, error: f.legacy[ext] };
  const kind = f?.extensions?.[ext];
  if (!kind) return { kind: null, error: "This type of file can't be converted." };
  return { kind, error: null };
}

function kindLabel(kind) {
  return state.formats?.kinds?.[kind] || kind;
}

function outputLabel(target) {
  return state.formats?.outputs?.[target]?.label || target.toUpperCase();
}

// ------------------------------------------------------------------ theme
$("themeBtn").addEventListener("click", () => {
  const attr = document.documentElement.getAttribute("data-theme");
  const current = attr || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem("fileflip:theme", next);
  } catch {
    /* ignore */
  }
});

// ------------------------------------------------------------------ mode switch
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".mode").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $("convertPanel").hidden = mode !== "convert";
  $("toolsPanel").hidden = mode !== "tools";
}
document.querySelectorAll(".mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

// ------------------------------------------------------------------ file picking (shared)
function wireDrop(zoneId, inputId, onFiles) {
  const zone = $(zoneId);
  $(inputId).addEventListener("change", (e) => {
    onFiles([...e.target.files]);
    e.target.value = "";
  });
  ["dragenter", "dragover"].forEach((t) =>
    zone.addEventListener(t, (e) => {
      e.preventDefault();
      zone.classList.add("dragging");
    }),
  );
  ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("dragging")));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer?.files?.length) onFiles([...e.dataTransfer.files]);
  });
}
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

function tooBig(file) {
  const max = (state.formats?.max_upload_mb || 25) * 1024 * 1024;
  return file.size > max;
}

// ------------------------------------------------------------------ convert: files
function addConvertFiles(files) {
  if (state.busy) return;
  let skipped = 0;
  for (const file of files) {
    if (state.convertFiles.length >= 20) {
      skipped++;
      continue;
    }
    if (state.convertFiles.some((f) => f.file.name === file.name && f.file.size === file.size)) continue;
    let { kind, error } = kindOf(file);
    if (!error && tooBig(file)) error = `Over the ${state.formats?.max_upload_mb || 25} MB limit.`;
    state.convertFiles.push({ id: state.nextId++, file, kind, error });
  }
  if (skipped) toast("You can convert up to 20 files at a time.");
  renderConvert();
}

function removeConvertFile(id) {
  state.convertFiles = state.convertFiles.filter((f) => f.id !== id);
  renderConvert();
}

function usableConvertFiles() {
  return state.convertFiles.filter((f) => !f.error);
}

function commonTargets() {
  const files = usableConvertFiles();
  if (!files.length || !state.formats) return [];
  let targets = state.formats.targets[files[0].kind] || [];
  for (const f of files.slice(1)) {
    const t = state.formats.targets[f.kind] || [];
    targets = targets.filter((x) => t.includes(x));
  }
  return targets;
}

function fileRow(f, { reorder = false, index = 0, total = 0 } = {}) {
  const badgeKind = f.kind || "txt";
  const meta = f.error ? `<span class="file-error">${escapeHtml(f.error)}</span>` : `<span class="file-meta">${formatBytes(f.file.size)} · ${escapeHtml(kindLabel(f.kind || "pdf"))}</span>`;
  const moves = reorder
    ? `<button type="button" class="mini" data-up="${f.id}" aria-label="Move up" ${index === 0 ? "disabled" : ""}>${ICON_UP}</button>
       <button type="button" class="mini" data-down="${f.id}" aria-label="Move down" ${index === total - 1 ? "disabled" : ""}>${ICON_DOWN}</button>`
    : "";
  return `<li class="file-row">
    <span class="kind" data-kind="${badgeKind}">${BADGE[badgeKind] || "FILE"}</span>
    <span class="file-info"><span class="file-name" title="${escapeHtml(f.file.name)}">${escapeHtml(f.file.name)}</span>${meta}</span>
    <span class="row-actions">${moves}<button type="button" class="mini" data-remove="${f.id}" aria-label="Remove ${escapeHtml(f.file.name)}" ${state.busy ? "disabled" : ""}>${ICON_X}</button></span>
  </li>`;
}

function renderConvert() {
  $("convertFiles").innerHTML = state.convertFiles.map((f) => fileRow(f)).join("");
  $("convertDrop").classList.toggle("compact", state.convertFiles.length > 0);

  const targets = commonTargets();
  const usable = usableConvertFiles();
  $("convertOptions").hidden = !usable.length;
  if (!targets.includes(state.target)) {
    const wanted = state.preferred?.to;
    state.target = targets.includes(wanted) ? wanted : targets[0] || null;
  }
  $("targetChips").innerHTML = targets
    .map((t) => `<button type="button" class="chip" role="radio" data-target="${t}" aria-checked="${t === state.target}">${escapeHtml(outputLabel(t))}</button>`)
    .join("");

  const kinds = [...new Set(usable.map((f) => f.kind))];
  if (usable.length && !targets.length) {
    $("targetHint").textContent = "These files can't all become the same format. Convert them in separate batches.";
  } else {
    $("targetHint").textContent = kinds.length === 1 ? HINTS[`${kinds[0]}>${state.target}`] || "" : "";
  }
  const imagesToPdf = kinds.length === 1 && kinds[0] === "image" && state.target === "pdf";
  $("combineRow").hidden = !(imagesToPdf && usable.length > 1);
  $("pageSizeRow").hidden = !["pdf", "docx"].includes(state.target);

  const btn = $("convertBtn");
  btn.disabled = state.busy || !usable.length || !state.target;
  if (!state.busy) {
    let label = "Add files to start";
    if (usable.length && state.target) {
      const combine = imagesToPdf && usable.length > 1 && $("combineImages").checked;
      label = combine
        ? `Combine ${usable.length} images into one PDF`
        : `Convert ${usable.length} file${usable.length === 1 ? "" : "s"} to ${outputLabel(state.target)}`;
    } else if (usable.length) {
      label = "Choose a format";
    }
    btn.querySelector(".btn-label").textContent = label;
  }
  prepareLibraries();
}

function prepareLibraries() {
  // Browser version: start downloading what this conversion needs while the user decides.
  const engine = state.engine;
  if (!engine || engine.mode !== "browser" || !state.target) return;
  const cfg = engine.cfg;
  const kinds = new Set(usableConvertFiles().map((f) => f.kind));
  const packages = new Set(cfg.writer_packages[state.target] || []);
  kinds.forEach((k) => (cfg.reader_packages[k] || []).forEach((p) => packages.add(p)));
  engine.prepare([...packages], state.target === "pdf").catch(() => {});
}

$("convertFiles").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-remove]");
  if (btn) removeConvertFile(Number(btn.dataset.remove));
});
$("targetChips").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-target]");
  if (!chip) return;
  state.target = chip.dataset.target;
  state.preferred = null;
  document.querySelectorAll(".pop.active").forEach((p) => p.classList.remove("active"));
  renderConvert();
});
$("combineImages").addEventListener("change", renderConvert);
wireDrop("convertDrop", "convertInput", addConvertFiles);

document.querySelectorAll(".pop").forEach((pop) =>
  pop.addEventListener("click", () => {
    state.preferred = { from: pop.dataset.from, to: pop.dataset.to };
    document.querySelectorAll(".pop").forEach((p) => p.classList.toggle("active", p === pop));
    const exts = Object.entries(state.formats?.extensions || {})
      .filter(([, kind]) => kind === pop.dataset.from)
      .map(([ext]) => ext);
    const input = $("convertInput");
    input.accept = exts.join(",");
    if (usableConvertFiles().some((f) => f.kind === pop.dataset.from)) {
      state.target = pop.dataset.to;
      renderConvert();
    } else {
      input.click();
    }
    setTimeout(() => (input.accept = ""), 1000);
  }),
);

// ------------------------------------------------------------------ convert: run
$("convertBtn").addEventListener("click", async () => {
  const files = usableConvertFiles();
  if (!files.length || !state.target || state.busy) return;
  showError("convertError", "");
  const engine = await engineReady;
  const target = state.target;
  const pageSize = $("pageSize").value;
  const combine = files.every((f) => f.kind === "image") && target === "pdf" && files.length > 1 && $("combineImages").checked;

  startRun(combine ? [{ name: "images.pdf", kind: "pdf" }] : files.map((f) => ({ name: f.file.name, kind: target })));
  const btn = $("convertBtn");
  try {
    if (engine.mode === "browser") {
      btn.querySelector(".btn-label").textContent = "Getting ready…";
      await engine.warmUp();
    }
    if (combine) {
      btn.querySelector(".btn-label").textContent = "Combining…";
      await runOne(0, () => engine.tool("images-to-pdf", files.map((f) => f.file), { page_size: pageSize }));
    } else {
      for (let i = 0; i < files.length; i++) {
        btn.querySelector(".btn-label").textContent = files.length > 1 ? `Converting ${i + 1} of ${files.length}…` : "Converting…";
        await runOne(i, () => engine.convert(files[i].file, target, pageSize));
      }
    }
  } catch (err) {
    showError("convertError", err.message || "Couldn't start the conversion.");
  } finally {
    endRun();
    renderConvert();
  }
});

// ------------------------------------------------------------------ tools
function currentTool() {
  return document.querySelector('input[name="tool"]:checked').value;
}

function addToolFiles(files) {
  if (state.busy) return;
  for (const file of files) {
    if (state.toolFiles.length >= 20) break;
    if (state.toolFiles.some((f) => f.file.name === file.name && f.file.size === file.size)) continue;
    const isPdf = extOf(file.name) === ".pdf" || file.type === "application/pdf";
    const error = !isPdf ? "Not a PDF." : tooBig(file) ? `Over the ${state.formats?.max_upload_mb || 25} MB limit.` : null;
    state.toolFiles.push({ id: state.nextId++, file, kind: "pdf", error });
  }
  renderTools();
}

function renderTools() {
  const tool = currentTool();
  const usable = state.toolFiles.filter((f) => !f.error);
  $("toolFiles").innerHTML = state.toolFiles
    .map((f, i) => fileRow(f, { reorder: tool === "merge", index: i, total: state.toolFiles.length }))
    .join("");
  $("toolsDrop").classList.toggle("compact", state.toolFiles.length > 0);
  $("toolsHint").textContent = TOOL_HINT[tool];
  document.querySelectorAll(".tool-opts").forEach((el) => (el.hidden = el.dataset.for !== tool));
  $("rangesRow").hidden = document.querySelector('input[name="splitMode"]:checked').value !== "ranges";

  let problem = "";
  if (tool === "merge" && usable.length === 1) problem = "Add at least one more PDF to merge.";
  if (tool === "split" && usable.length > 1) problem = "Split works on one PDF at a time - remove the others.";
  const btn = $("toolsBtn");
  btn.disabled = state.busy || !usable.length || Boolean(problem);
  if (!state.busy) {
    btn.querySelector(".btn-label").textContent = !usable.length
      ? "Add PDFs to start"
      : problem
        ? TOOL_VERB[tool] + " PDF"
        : `${TOOL_VERB[tool]} ${usable.length === 1 ? "PDF" : `${usable.length} PDFs`}`;
  }
  showError("toolsError", problem);
}

$("toolFiles").addEventListener("click", (e) => {
  const remove = e.target.closest("[data-remove]");
  const up = e.target.closest("[data-up]");
  const down = e.target.closest("[data-down]");
  const list = state.toolFiles;
  if (remove) state.toolFiles = list.filter((f) => f.id !== Number(remove.dataset.remove));
  const move = (id, delta) => {
    const i = list.findIndex((f) => f.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
  };
  if (up) move(Number(up.dataset.up), -1);
  if (down) move(Number(down.dataset.down), 1);
  renderTools();
});
document.querySelectorAll('input[name="tool"], input[name="splitMode"]').forEach((el) => el.addEventListener("change", renderTools));
wireDrop("toolsDrop", "toolsInput", addToolFiles);

$("toolsBtn").addEventListener("click", async () => {
  const tool = currentTool();
  const files = state.toolFiles.filter((f) => !f.error).map((f) => f.file);
  if (!files.length || state.busy) return;
  const options = {
    split: { mode: document.querySelector('input[name="splitMode"]:checked').value, ranges: $("splitRanges").value },
    rotate: { angle: Number(document.querySelector('input[name="angle"]:checked').value), pages: $("rotatePages").value || "all" },
    compress: { level: document.querySelector('input[name="level"]:checked').value },
    merge: {},
  }[tool];
  showError("toolsError", "");
  const engine = await engineReady;
  startRun([{ name: tool === "merge" ? "merged.pdf" : files[0].name, kind: "pdf" }]);
  $("toolsBtn").querySelector(".btn-label").textContent = { merge: "Merging…", split: "Splitting…", rotate: "Rotating…", compress: "Compressing…" }[tool];
  try {
    await runOne(0, () => engine.tool(tool, files, options));
  } finally {
    endRun();
    renderTools();
  }
});

// ------------------------------------------------------------------ results
function startRun(items) {
  state.busy = true;
  clearResults();
  state.results = items.map((item) => ({ ...item, status: "waiting" }));
  for (const id of ["convertBtn", "toolsBtn"]) {
    $(id).classList.add("loading");
    $(id).disabled = true;
  }
  ["convertDrop", "toolsDrop"].forEach((id) => $(id).classList.add("disabled"));
  $("results").hidden = false;
  renderResults();
}

function endRun() {
  state.busy = false;
  for (const id of ["convertBtn", "toolsBtn"]) $(id).classList.remove("loading");
  ["convertDrop", "toolsDrop"].forEach((id) => $(id).classList.remove("disabled"));
  renderResults();
  $("results").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function runOne(index, job) {
  const item = state.results[index];
  item.status = "working";
  renderResults();
  try {
    const { info, blob } = await job();
    const ext = (info.filename.split(".").pop() || "").toLowerCase();
    Object.assign(item, { status: "done", info, blob, url: URL.createObjectURL(blob), kind: ext === "jpeg" ? "jpg" : ext });
  } catch (err) {
    Object.assign(item, { status: "error", error: err.message || "Something went wrong." });
  }
  renderResults();
}

function clearResults() {
  state.results.forEach((r) => r.url && URL.revokeObjectURL(r.url));
  state.results = [];
}

function renderResults() {
  const list = state.results;
  $("resultList").innerHTML = list
    .map((r) => {
      const kind = BADGE[r.kind] ? r.kind : "txt";
      let meta = "";
      let action = "";
      if (r.status === "waiting") meta = '<span class="status">Waiting…</span>';
      if (r.status === "working") meta = '<span class="status"><span class="spinner"></span>Working…</span>';
      if (r.status === "error") meta = `<span class="file-error">${escapeHtml(r.error)}</span>`;
      if (r.status === "done") {
        meta = `<span class="file-meta">${formatBytes(r.info.bytes)}</span>`;
        if (r.info.notes?.length) meta += `<ul class="file-notes">${r.info.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`;
        action = `<a class="btn primary sm" href="${r.url}" download="${escapeHtml(r.info.filename)}">Download</a>`;
      }
      const name = r.status === "done" ? r.info.filename : r.name;
      return `<li class="file-row">
        <span class="kind" data-kind="${kind}">${BADGE[kind]}</span>
        <span class="file-info"><span class="file-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>${meta}</span>
        <span class="row-actions">${action}</span>
      </li>`;
    })
    .join("");
  const done = list.filter((r) => r.status === "done");
  const failed = list.filter((r) => r.status === "error").length;
  $("zipBtn").hidden = done.length < 2 || state.busy;
  $("resultsSummary").textContent = state.busy
    ? "Working on it…"
    : `${done.length} file${done.length === 1 ? "" : "s"} ready${failed ? ` · ${failed} couldn't be converted` : ""}`;
}

$("zipBtn").addEventListener("click", async () => {
  const done = state.results.filter((r) => r.status === "done");
  const zip = await makeZip(done.map((r) => ({ name: r.info.filename, blob: r.blob })));
  const url = URL.createObjectURL(zip);
  const a = document.createElement("a");
  a.href = url;
  a.download = `fileflip-${new Date().toISOString().slice(0, 10)}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
});

// ------------------------------------------------------------------ init
(async function init() {
  const engine = await engineReady;
  document.querySelectorAll(".maxMb").forEach((el) => (el.textContent = state.formats?.max_upload_mb || 25));
  if (engine.mode === "browser") {
    $("privacyText").textContent = "Everything happens in your browser, so your files stay with you.";
    engine.onStatus = setStatus;
    engine.warmUp().catch(() => {});
  }
  if (!state.formats) setStatus("error", "Couldn't load the list of formats. Please reload the page.");
  renderConvert();
  renderTools();
})();
