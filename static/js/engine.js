// The conversion "engine" behind the UI. Two interchangeable implementations:
//
//  ServerEngine  - the FastAPI version: uploads files to /api/convert and /api/tools/*.
//  BrowserEngine - the GitHub Pages version: a Web Worker (worker.js) runs the same
//                  Python code with Pyodide. Files never leave the visitor's device.
//
// createEngine() picks one: the Pages build ships a browser-mode.json file, the
// server does not.

async function apiError(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join("; ");
  } catch {
    /* not JSON */
  }
  return `Something went wrong (HTTP ${res.status}).`;
}

async function fileResponse(res) {
  if (!res.ok) throw new Error(await apiError(res));
  const info = JSON.parse(res.headers.get("X-FileFlip-Info") || "{}");
  return { info, blob: await res.blob() };
}

class ServerEngine {
  constructor() {
    this.mode = "server";
    this.onStatus = () => {};
  }

  async formats() {
    const res = await fetch("api/formats");
    if (!res.ok) throw new Error(await apiError(res));
    return res.json();
  }

  warmUp() {
    return Promise.resolve();
  }

  prepare() {
    return Promise.resolve();
  }

  async convert(file, target, pageSize) {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("target", target);
    form.append("page_size", pageSize);
    return fileResponse(await fetch("api/convert", { method: "POST", body: form }));
  }

  async tool(tool, files, options) {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("options", JSON.stringify(options || {}));
    return fileResponse(await fetch(`api/tools/${tool}`, { method: "POST", body: form }));
  }
}

class BrowserEngine {
  constructor(cfg) {
    this.mode = "browser";
    this.cfg = { ...cfg, site_base: new URL(".", window.location.href).href };
    this.onStatus = () => {};
    this.worker = null;
    this.calls = new Map();
    this.seq = 0;
    this.ready = null;
  }

  async formats() {
    return { ...this.cfg.formats, mode: "browser" };
  }

  _worker() {
    if (this.worker) return this.worker;
    this.worker = new Worker(new URL(`./worker.js?v=${this.cfg.version}`, import.meta.url));
    this.worker.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === "status") return this.onStatus(msg.kind, msg.message);
      const call = this.calls.get(msg.id);
      if (!call) return;
      this.calls.delete(msg.id);
      if (msg.ok) call.resolve(msg.value);
      else call.reject(new Error(msg.error));
    };
    this.worker.onerror = (event) => {
      event.preventDefault();
      const error = new Error("The converter stopped unexpectedly. Please reload the page.");
      for (const call of this.calls.values()) call.reject(error);
      this.calls.clear();
      this.worker.terminate();
      this.worker = null;
      this.ready = null;
      this.onStatus("error", error.message);
    };
    return this.worker;
  }

  _call(type, payload = {}, transfer = []) {
    const worker = this._worker();
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      this.calls.set(id, { resolve, reject });
      worker.postMessage({ id, type, ...payload }, transfer);
    });
  }

  /** Start loading Python now (cached by the browser after the first visit). */
  warmUp() {
    if (!this.ready) {
      this.ready = this._call("init", { cfg: this.cfg }).catch((err) => {
        this.ready = null;
        this.onStatus("error", "Couldn't load the converter - check your connection and reload the page.");
        throw err;
      });
    }
    return this.ready;
  }

  /** Download the libraries a conversion needs before the user clicks Convert. */
  async prepare(packages, fonts = false) {
    await this.warmUp();
    return this._call("prepare", { packages, fonts });
  }

  async convert(file, target, pageSize) {
    await this.warmUp();
    const buffer = await file.arrayBuffer();
    const { info, data } = await this._call("convert", { name: file.name, buffer, target, pageSize }, [buffer]);
    return { info, blob: new Blob([data], { type: info.mime }) };
  }

  async tool(tool, files, options) {
    await this.warmUp();
    const buffers = await Promise.all(files.map((f) => f.arrayBuffer()));
    const { info, data } = await this._call(
      "tool",
      { tool, names: files.map((f) => f.name), buffers, options: options || {} },
      buffers,
    );
    return { info, blob: new Blob([data], { type: info.mime }) };
  }
}

export async function createEngine() {
  try {
    const res = await fetch("browser-mode.json", { cache: "no-cache" });
    if (res.ok) {
      const cfg = await res.json();
      if (cfg.mode === "browser") return new BrowserEngine(cfg);
    }
  } catch {
    /* fall through to the server engine */
  }
  return new ServerEngine();
}
