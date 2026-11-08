// FileFlip browser engine (GitHub Pages version), running in a Web Worker so the page
// stays responsive. Pyodide (Python compiled to WebAssembly) runs the same Python code
// as the server. Libraries are downloaded only when a conversion needs them, and the
// browser caches them for next time.
//
// Messages in:  init | prepare {packages, fonts} | convert {name, buffer, target, pageSize}
//               | tool {tool, names, buffers, options}
// Messages out: { id, ok, value | error }  and  { type: "status", kind, message }

let cfg = null;
let py = null;
let api = null;
let ready = null;
let fontsLoaded = null;
let counter = 0;
const installed = new Set();

const READY_TEXT = "Ready - your files never leave this device.";
const FRIENDLY = {
  "pdfminer.six": "the PDF reader",
  pypdf: "the PDF tools",
  "python-docx": "the Word tools",
  "python-pptx": "the PowerPoint reader",
  openpyxl: "the Excel tools",
  fpdf2: "the PDF writer",
  markdown: "the Markdown reader",
};
const status = (kind, message) => postMessage({ type: "status", kind, message });

async function init() {
  status("loading", "Loading the converter (first visit only)…");
  importScripts(`${cfg.pyodide_base}pyodide.js`);
  py = await self.loadPyodide({ indexURL: cfg.pyodide_base });
  await py.loadPackage(["micropip", "Pillow"]);
  await Promise.all(
    cfg.modules.map(async (path) => {
      const res = await fetch(`${cfg.site_base}py/${path}?v=${cfg.version}`); // version: never reuse old code after a deploy
      if (!res.ok) throw new Error(`Missing module ${path}`);
      const dir = `/home/pyodide/${path.split("/").slice(0, -1).join("/")}`;
      py.FS.mkdirTree(dir);
      py.FS.writeFile(`/home/pyodide/${path}`, await res.text());
    }),
  );
  py.runPython("import sys\nsys.path.insert(0, '/home/pyodide')");
  api = py.pyimport("app.browser");
  status("ready", READY_TEXT);
}

function ensureReady() {
  if (!ready) ready = init().catch((err) => ((ready = null), Promise.reject(err)));
  return ready;
}

async function ensurePackages(names) {
  const missing = names.filter((n) => !installed.has(n));
  if (!missing.length) return;
  status("loading", `Getting ${missing.map((n) => FRIENDLY[n] || n).join(" and ")} ready (first time only)…`);
  const micropip = py.pyimport("micropip");
  const pins = py.toPy(missing.map((n) => cfg.packages[n] || n));
  try {
    await micropip.install(pins);
  } finally {
    pins.destroy();
    micropip.destroy();
  }
  missing.forEach((n) => installed.add(n));
  status("ready", READY_TEXT);
}

function ensureFonts() {
  if (!fontsLoaded) {
    fontsLoaded = (async () => {
      py.FS.mkdirTree("/home/pyodide/app/fonts");
      await Promise.all(
        cfg.fonts.map(async (name) => {
          const res = await fetch(`${cfg.site_base}py/app/fonts/${name}?v=${cfg.version}`);
          if (!res.ok) throw new Error(`Missing font ${name}`);
          py.FS.writeFile(`/home/pyodide/app/fonts/${name}`, new Uint8Array(await res.arrayBuffer()));
        }),
      );
    })().catch((err) => ((fontsLoaded = null), Promise.reject(err)));
  }
  return fontsLoaded;
}

function check(json) {
  const out = JSON.parse(json);
  if (!out.ok) throw new Error(out.error);
  return out;
}

function readOutput(path) {
  const data = py.FS.readFile(path);
  py.FS.unlink(path);
  return data;
}

async function convert({ name, buffer, target, pageSize }) {
  await ensureReady();
  const n = ++counter;
  const src = `/tmp/in-${n}`;
  const out = `/tmp/out-${n}`;
  py.FS.writeFile(src, new Uint8Array(buffer));
  let info;
  try {
    info = check(api.inspect(src, name));
    await ensurePackages(info.packages[target] || []);
    if (target === "pdf") await ensureFonts();
  } catch (err) {
    try {
      py.FS.unlink(src);
    } catch {
      /* already gone */
    }
    throw err;
  }
  const { result } = check(api.convert_file(src, name, target, pageSize || "A4", out));
  return { info: result, data: readOutput(out) };
}

async function tool({ tool: name, names, buffers, options }) {
  await ensureReady();
  await ensurePackages(JSON.parse(api.tool_packages(name)));
  const paths = buffers.map((buffer, i) => {
    const path = `/tmp/tool-${++counter}-${i}`;
    py.FS.writeFile(path, new Uint8Array(buffer));
    return path;
  });
  const out = `/tmp/out-${++counter}`;
  const { result } = check(api.run_tool_files(name, JSON.stringify(paths), JSON.stringify(names), JSON.stringify(options), out));
  return { info: result, data: readOutput(out) };
}

async function handle(msg) {
  const { id, type } = msg;
  try {
    let value = null;
    let transfer = [];
    if (type === "init") {
      cfg = msg.cfg;
      await ensureReady();
    } else if (type === "prepare") {
      await ensureReady();
      await ensurePackages(msg.packages || []);
      if (msg.fonts) await ensureFonts();
    } else if (type === "convert" || type === "tool") {
      value = type === "convert" ? await convert(msg) : await tool(msg);
      transfer = [value.data.buffer];
    } else {
      throw new Error(`Unknown request ${type}`);
    }
    postMessage({ id, ok: true, value }, transfer);
  } catch (err) {
    if (api) status("ready", READY_TEXT);
    else status("error", "Couldn't load the converter - check your connection and reload the page.");
    postMessage({ id, ok: false, error: (err && err.message) || String(err) });
  }
}

// One job at a time, in order.
let queue = Promise.resolve();
self.onmessage = (event) => {
  queue = queue.then(() => handle(event.data));
};
