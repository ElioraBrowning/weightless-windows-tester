/**
 * Antigravity Sidecar Web Server for Woolbur Appearance, Settings & Per-App Memory.
 *
 * Syncs atomically with woolbur_config.json and app_memory.json in DESKTOP_TESTER_DATA_DIR
 * (~/.local_desktop_agent by default) so edits take effect live in the Python MCP overlay
 * and MCP update_app_memory calls stream back into this sidebar via SSE.
 */

import http from "node:http";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const UI_HTML_PATH = path.join(__dirname, "ui.html");
const PREVIEW_WORKER_PATH = path.join(__dirname, "preview_worker.py");
const BIND_HOST = "127.0.0.1";
const PORT = Number(process.env.ANTIGRAVITY_SIDECAR_WEB_PORT || 0);
const UI_TOKEN = process.env.ANTIGRAVITY_SIDECAR_UI_TOKEN || "";

const DATA_DIR = process.env.DESKTOP_TESTER_DATA_DIR?.trim()
  ? path.resolve(process.env.DESKTOP_TESTER_DATA_DIR.trim())
  : path.join(os.homedir(), ".local_desktop_agent");
const CONFIG_PATH = path.join(DATA_DIR, "woolbur_config.json");
const MEMORY_PATH = path.join(DATA_DIR, "app_memory.json");

const VALID_MOODS = new Set(["auto", "nerd", "detective", "punk", "goth", "happy"]);
const VALID_ACCESSORY_SETS = new Set(["auto", "nerd", "detective", "punk", "goth", "happy", "none"]);

const DEFAULT_CONFIG = Object.freeze({
  appearance: {
    accessory_set: "auto",
    mood: "auto",
    scale: 1.0,
    opacity: 0.95,
    show_hud: true,
    show_woolbur: true,
    follow_cursor: true,
    accent_color: "#38bdf8",
    wool_color: "#fffbf5",
    horn_color: "#fde68a",
  },
  settings: {
    default_screenshot_scale: 0.5,
    default_screenshot_format: "jpeg",
    default_grid: true,
    force_fullscreen: true,
    triple_esc_window_seconds: 1.8,
    assist_move_threshold_px: 45.0,
    auto_save_app_memory: true,
  },
});

function clone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function clampFloat(val, lo, hi, fallback) {
  const n = Number(val);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(lo, Math.min(hi, n));
}

function normalizeHex(val, fallback) {
  const s = String(val ?? "").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return s.toLowerCase();
  if (/^#[0-9a-fA-F]{3}$/.test(s)) {
    return `#${s[1]}${s[1]}${s[2]}${s[2]}${s[3]}${s[3]}`.toLowerCase();
  }
  return fallback;
}

function normalizeConfig(raw) {
  const cfg = clone(DEFAULT_CONFIG);
  if (!raw || typeof raw !== "object") return cfg;

  const appIn = raw.appearance;
  if (appIn && typeof appIn === "object") {
    const accIn = String(appIn.accessory_set ?? "").trim().toLowerCase();
    const mood = String(appIn.mood ?? "auto").trim().toLowerCase();
    if (VALID_MOODS.has(mood)) cfg.appearance.mood = mood;
    if (VALID_ACCESSORY_SETS.has(accIn)) {
      cfg.appearance.accessory_set = accIn;
      if (!("mood" in appIn) && VALID_MOODS.has(accIn)) {
        cfg.appearance.mood = accIn;
      }
    } else if (VALID_ACCESSORY_SETS.has(mood)) {
      cfg.appearance.accessory_set = mood;
    }
    cfg.appearance.scale = Math.round(clampFloat(appIn.scale, 0.65, 1.6, 1.0) * 100) / 100;
    cfg.appearance.opacity = Math.round(clampFloat(appIn.opacity, 0.4, 1.0, 0.95) * 100) / 100;
    for (const k of ["show_hud", "show_woolbur", "follow_cursor"]) {
      if (k in appIn) cfg.appearance[k] = Boolean(appIn[k]);
    }
    cfg.appearance.accent_color = normalizeHex(appIn.accent_color, "#38bdf8");
    cfg.appearance.wool_color = normalizeHex(appIn.wool_color, "#fffbf5");
    cfg.appearance.horn_color = normalizeHex(appIn.horn_color, "#fde68a");
  }

  const setIn = raw.settings;
  if (setIn && typeof setIn === "object") {
    cfg.settings.default_screenshot_scale =
      Math.round(clampFloat(setIn.default_screenshot_scale, 0.25, 1.0, 0.5) * 100) / 100;
    const fmt = String(setIn.default_screenshot_format ?? "jpeg").trim().toLowerCase();
    cfg.settings.default_screenshot_format = fmt === "png" ? "png" : "jpeg";
    for (const k of ["default_grid", "force_fullscreen", "auto_save_app_memory"]) {
      if (k in setIn) cfg.settings[k] = Boolean(setIn[k]);
    }
    cfg.settings.triple_esc_window_seconds =
      Math.round(clampFloat(setIn.triple_esc_window_seconds, 0.6, 5.0, 1.8) * 100) / 100;
    cfg.settings.assist_move_threshold_px =
      Math.round(clampFloat(setIn.assist_move_threshold_px, 15.0, 200.0, 45.0) * 10) / 10;
  }
  return cfg;
}

function normalizeAppKey(name) {
  const s = String(name ?? "").trim().toLowerCase().replace(/\.exe$/i, "");
  return s.replace(/[^a-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "app";
}

function normalizeAppEntry(key, val) {
  const id = normalizeAppKey(key);
  const entry = {
    app_id: id,
    display_name: String(val?.display_name ?? key).trim() || id,
    process_names: [],
    title_patterns: [],
    notes: [],
    coordinates: {},
    hotkeys: {},
    settings: {},
    updated_at: String(val?.updated_at ?? new Date().toISOString()),
  };
  if (Array.isArray(val?.process_names)) {
    entry.process_names = val.process_names
      .map((p) => String(p ?? "").trim())
      .filter(Boolean);
  }
  if (Array.isArray(val?.title_patterns)) {
    entry.title_patterns = val.title_patterns
      .map((p) => String(p ?? "").trim())
      .filter(Boolean);
  }
  if (Array.isArray(val?.notes)) {
    entry.notes = val.notes
      .map((n) => String(n ?? "").trim())
      .filter(Boolean);
  }
  if (val?.coordinates && typeof val.coordinates === "object" && !Array.isArray(val.coordinates)) {
    for (const [ck, cv] of Object.entries(val.coordinates)) {
      const ckClean = String(ck ?? "").trim();
      if (!ckClean) continue;
      if (Array.isArray(cv) && cv.length >= 2) {
        entry.coordinates[ckClean] = { x: Math.round(Number(cv[0]) || 0), y: Math.round(Number(cv[1]) || 0) };
      } else if (cv && typeof cv === "object") {
        const cleanCoord = {};
        if ("x" in cv && "y" in cv) {
          cleanCoord.x = Math.round(Number(cv.x) || 0);
          cleanCoord.y = Math.round(Number(cv.y) || 0);
        }
        if (cv.description) cleanCoord.description = String(cv.description).trim();
        if (cv.label) cleanCoord.label = String(cv.label).trim();
        entry.coordinates[ckClean] = Object.keys(cleanCoord).length ? cleanCoord : clone(cv);
      }
    }
  }
  if (val?.hotkeys && typeof val.hotkeys === "object" && !Array.isArray(val.hotkeys)) {
    for (const [hk, hv] of Object.entries(val.hotkeys)) {
      const hkClean = String(hk ?? "").trim();
      const hvClean = String(hv ?? "").trim();
      if (hkClean && hvClean) entry.hotkeys[hkClean] = hvClean;
    }
  }
  if (val?.settings && typeof val.settings === "object" && !Array.isArray(val.settings)) {
    entry.settings = clone(val.settings);
  }
  return entry;
}

function normalizeMemory(raw) {
  const apps = {};
  const src = raw?.apps;
  if (src && typeof src === "object" && !Array.isArray(src)) {
    for (const [k, v] of Object.entries(src)) {
      if (!v || typeof v !== "object") continue;
      const entry = normalizeAppEntry(v.app_id || v.app_key || k, v);
      apps[entry.app_id] = entry;
    }
  }
  return { apps };
}

function readJsonSafe(filePath) {
  try {
    if (!fs.existsSync(filePath)) return null;
    return JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch {
    return null;
  }
}

function atomicWriteJson(filePath, data) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`;
  try {
    fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2), "utf-8");
    fs.renameSync(tmpPath, filePath);
  } finally {
    if (fs.existsSync(tmpPath)) {
      try {
        fs.unlinkSync(tmpPath);
      } catch {
        // Ignore cleanup failure
      }
    }
  }
}

function loadConfig() {
  return normalizeConfig(readJsonSafe(CONFIG_PATH));
}

function saveConfigPatch(patch) {
  const cur = loadConfig();
  const merged = {
    appearance: {
      ...cur.appearance,
      ...(patch?.appearance && typeof patch.appearance === "object" ? patch.appearance : {}),
    },
    settings: {
      ...cur.settings,
      ...(patch?.settings && typeof patch.settings === "object" ? patch.settings : {}),
    },
  };
  const norm = normalizeConfig(merged);
  atomicWriteJson(CONFIG_PATH, norm);
  return norm;
}

function loadMemory() {
  return normalizeMemory(readJsonSafe(MEMORY_PATH));
}

function saveMemoryApp(payload) {
  const data = loadMemory();
  const rawName = String(payload?.app_id || payload?.app_key || payload?.app_name || payload?.display_name || "").trim();
  if (!rawName) {
    throw new Error("app_id or display_name is required");
  }
  const key = normalizeAppKey(rawName);
  const existing = data.apps[key] || { app_id: key };
  const merged = normalizeAppEntry(key, {
    ...existing,
    ...payload,
    app_id: key,
    updated_at: new Date().toISOString(),
  });
  data.apps[key] = merged;
  atomicWriteJson(MEMORY_PATH, data);
  return { app: merged, memory: data };
}

function deleteMemoryApp(appKey) {
  const data = loadMemory();
  const key = normalizeAppKey(appKey);
  if (!(key in data.apps)) {
    const fallback = Object.keys(data.apps).find(
      (k) => k.toLowerCase() === String(appKey ?? "").trim().toLowerCase(),
    );
    if (fallback) {
      delete data.apps[fallback];
      atomicWriteJson(MEMORY_PATH, data);
      return { deleted: fallback, memory: data };
    }
    return { deleted: null, memory: data };
  }
  delete data.apps[key];
  atomicWriteJson(MEMORY_PATH, data);
  return { deleted: key, memory: data };
}

function getStateSnapshot() {
  return {
    config: loadConfig(),
    defaults: DEFAULT_CONFIG,
    memory: loadMemory(),
    data_dir: DATA_DIR,
    updated_at: new Date().toISOString(),
  };
}

function validateToken(req, url) {
  if (!UI_TOKEN) return true;
  const headerToken = req.headers["x-sidecar-token"];
  const queryToken = url.searchParams.get("token");
  return headerToken === UI_TOKEN || queryToken === UI_TOKEN;
}

function sendJson(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function readJsonBody(req, maxBytes = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > maxBytes) {
        reject(new Error("Request body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf-8").trim();
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        reject(new Error("Invalid JSON body"));
      }
    });
    req.on("error", reject);
  });
}

function resolvePreloadScript() {
  const candidates = [
    path.resolve(__dirname, "..", "..", "..", "lib", "node_modules", "sidecar_sdk", "preload.js"),
    path.resolve(os.homedir(), ".gemini", "antigravity", "lib", "node_modules", "sidecar_sdk", "preload.js"),
    path.resolve(os.homedir(), ".gemini", "config", "node_modules", "sidecar_sdk", "preload.js"),
  ];
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) {
        return fs.readFileSync(candidate, "utf-8");
      }
    } catch {
      // Ignore inaccessible paths
    }
  }
  return `
window.addEventListener("message", (event) => {
  if (event.data?.type === "theme-change" && event.data.payload?.cssVariables) {
    for (const [k, v] of Object.entries(event.data.payload.cssVariables)) {
      document.documentElement.style.setProperty(k, v);
    }
  }
});
window.sidecar = window.sidecar || {};
`;
}

const PRELOAD_JS = resolvePreloadScript();
const sseClients = new Set();

function broadcastState() {
  if (sseClients.size === 0) return;
  const payload = `event: state\ndata: ${JSON.stringify(getStateSnapshot())}\n\n`;
  for (const client of sseClients) {
    try {
      client.write(payload);
    } catch {
      sseClients.delete(client);
    }
  }
}

fs.mkdirSync(DATA_DIR, { recursive: true });
let watchTimer = null;
let dirWatcher = null;
try {
  dirWatcher = fs.watch(DATA_DIR, { persistent: false }, (_eventType, filename) => {
    const name = String(filename || "");
    if (!name || name.endsWith(".tmp")) return;
    if (name === "woolbur_config.json" || name === "app_memory.json") {
      if (watchTimer) clearTimeout(watchTimer);
      watchTimer = setTimeout(() => {
        watchTimer = null;
        broadcastState();
      }, 90);
    }
  });
} catch {
  // Polling fallback isn't needed in normal setups, SSE keeps working on mutations
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${BIND_HOST}:${PORT || 80}`);
  const pathname = url.pathname;

  if (req.method === "GET" && (pathname === "/" || pathname === "/index.html" || pathname === "/ui.html")) {
    try {
      const html = fs.readFileSync(UI_HTML_PATH, "utf-8");
      res.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(html);
    } catch (err) {
      res.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      res.end(`Failed to load ui.html: ${err.message}`);
    }
    return;
  }

  if (req.method === "GET" && pathname === "/preload.js") {
    res.writeHead(200, {
      "Content-Type": "application/javascript; charset=utf-8",
      "Cache-Control": "no-store",
    });
    res.end(PRELOAD_JS);
    return;
  }

  if (req.method === "GET" && pathname === "/health") {
    sendJson(res, 200, { ok: true, data_dir: DATA_DIR });
    return;
  }

  if (pathname.startsWith("/api/")) {
    if (!validateToken(req, url)) {
      sendJson(res, 401, { error: "unauthorized" });
      return;
    }

    if (req.method === "GET" && pathname === "/api/state") {
      sendJson(res, 200, getStateSnapshot());
      return;
    }

    if (req.method === "GET" && pathname === "/api/preview.png") {
      try {
        const cur = loadConfig().appearance;
        const size = Math.max(64, Math.min(256, Math.round(Number(url.searchParams.get("size") || 168))));
        const acc = String(url.searchParams.get("accessory_set") || cur.accessory_set || "auto").trim().toLowerCase();
        const mood = String(url.searchParams.get("mood") || "normal").trim().toLowerCase();
        const frame = Math.max(0, Math.min(7, Math.round(Number(url.searchParams.get("frame") || 0))));
        const woolColor = normalizeHex(url.searchParams.get("wool_color"), cur.wool_color);
        const hornColor = normalizeHex(url.searchParams.get("horn_color"), cur.horn_color);
        const pngBuf = await renderWoolburPreviewPng({
          size,
          accessory_set: VALID_ACCESSORY_SETS.has(acc) ? acc : cur.accessory_set,
          mood,
          frame,
          wool_color: woolColor,
          horn_color: hornColor,
        });
        res.writeHead(200, {
          "Content-Type": "image/png",
          "Cache-Control": "no-store",
        });
        res.end(pngBuf);
      } catch (err) {
        sendJson(res, 500, { error: err.message });
      }
      return;
    }

    if (req.method === "GET" && pathname === "/api/events") {
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      });
      res.write(`event: state\ndata: ${JSON.stringify(getStateSnapshot())}\n\n`);
      sseClients.add(res);
      const keepAlive = setInterval(() => {
        try {
          res.write(": ping\n\n");
        } catch {
          clearInterval(keepAlive);
          sseClients.delete(res);
        }
      }, 20000);
      req.on("close", () => {
        clearInterval(keepAlive);
        sseClients.delete(res);
      });
      return;
    }

    if (req.method === "POST" && pathname === "/api/config") {
      try {
        const body = await readJsonBody(req);
        const config = saveConfigPatch(body);
        broadcastState();
        sendJson(res, 200, { ok: true, config, state: getStateSnapshot() });
      } catch (err) {
        sendJson(res, 400, { error: err.message });
      }
      return;
    }

    if (req.method === "POST" && pathname === "/api/config/reset") {
      try {
        const body = await readJsonBody(req);
        const scope = String(body?.scope || "all");
        const cur = loadConfig();
        const next = {
          appearance: scope === "appearance" || scope === "all" ? clone(DEFAULT_CONFIG.appearance) : cur.appearance,
          settings: scope === "settings" || scope === "all" ? clone(DEFAULT_CONFIG.settings) : cur.settings,
        };
        atomicWriteJson(CONFIG_PATH, next);
        broadcastState();
        sendJson(res, 200, { ok: true, config: next, state: getStateSnapshot() });
      } catch (err) {
        sendJson(res, 400, { error: err.message });
      }
      return;
    }

    if (req.method === "POST" && pathname === "/api/memory") {
      try {
        const body = await readJsonBody(req);
        const { app, memory } = saveMemoryApp(body);
        broadcastState();
        sendJson(res, 200, { ok: true, app, memory, state: getStateSnapshot() });
      } catch (err) {
        sendJson(res, 400, { error: err.message });
      }
      return;
    }

    if (req.method === "POST" && pathname === "/api/memory/delete") {
      try {
        const body = await readJsonBody(req);
        const key = body?.app_id || body?.app_key || body?.app_name;
        const { deleted, memory } = deleteMemoryApp(key);
        broadcastState();
        sendJson(res, 200, { ok: true, deleted, memory, state: getStateSnapshot() });
      } catch (err) {
        sendJson(res, 400, { error: err.message });
      }
      return;
    }
  }

  sendJson(res, 404, { error: "not_found", path: pathname });
});

server.listen(PORT, BIND_HOST, () => {
  const addr = server.address();
  const actualPort = typeof addr === "object" && addr ? addr.port : PORT;
  console.log(`[sidecar] Woolbur listening on ${BIND_HOST}:${actualPort}`);
});

function resolvePythonExe() {
  const candidates = [
    path.join(DATA_DIR, ".venv", "Scripts", "python.exe"),
    path.join(os.homedir(), ".local_desktop_agent", ".venv", "Scripts", "python.exe"),
    "python",
  ];
  for (const cand of candidates) {
    if (cand === "python" || fs.existsSync(cand)) return cand;
  }
  return "python";
}

const previewCache = new Map();
const pendingPreviewReqs = new Map();
let previewWorker = null;
let previewStdoutBuf = "";
let previewReqSeq = 1;

function resetPreviewWorker(errMsg = "Preview worker exited") {
  for (const entry of pendingPreviewReqs.values()) {
    clearTimeout(entry.timer);
    entry.reject(new Error(errMsg));
  }
  pendingPreviewReqs.clear();
  previewWorker = null;
  previewStdoutBuf = "";
}

function ensurePreviewWorker() {
  if (previewWorker && !previewWorker.killed) return previewWorker;
  const pyExe = resolvePythonExe();
  const child = spawn(pyExe, [PREVIEW_WORKER_PATH], {
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  child.stdout.setEncoding("utf-8");
  child.stdout.on("data", (chunk) => {
    previewStdoutBuf += chunk;
    let nlIdx;
    while ((nlIdx = previewStdoutBuf.indexOf("\n")) >= 0) {
      const line = previewStdoutBuf.slice(0, nlIdx).trim();
      previewStdoutBuf = previewStdoutBuf.slice(nlIdx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        const pending = pendingPreviewReqs.get(msg.id);
        if (!pending) continue;
        pendingPreviewReqs.delete(msg.id);
        clearTimeout(pending.timer);
        if (msg.error) pending.reject(new Error(msg.error));
        else pending.resolve(Buffer.from(msg.png_b64, "base64"));
      } catch {
        // Ignore unparseable line
      }
    }
  });
  child.on("exit", (code) => resetPreviewWorker(`Preview worker exited (${code})`));
  child.on("error", (err) => resetPreviewWorker(err.message));
  previewWorker = child;
  return child;
}

async function renderWoolburPreviewPng(params) {
  const key = `${params.size}|${params.accessory_set}|${params.mood}|${params.frame}|${params.wool_color}|${params.horn_color}`;
  const cached = previewCache.get(key);
  if (cached) return cached;

  const child = ensurePreviewWorker();
  const id = previewReqSeq++;
  const buf = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingPreviewReqs.delete(id);
      reject(new Error("Preview render timeout"));
    }, 4000);
    pendingPreviewReqs.set(id, { resolve, reject, timer });
    child.stdin.write(JSON.stringify({ id, ...params }) + "\n");
  });

  if (previewCache.size >= 128) {
    const firstKey = previewCache.keys().next().value;
    if (firstKey) previewCache.delete(firstKey);
  }
  previewCache.set(key, buf);
  return buf;
}

function shutdown() {
  if (watchTimer) clearTimeout(watchTimer);
  if (dirWatcher) {
    try {
      dirWatcher.close();
    } catch {
      // Ignore close error
    }
  }
  if (previewWorker && !previewWorker.killed) {
    try {
      previewWorker.kill();
    } catch {
      // Ignore kill error
    }
  }
  for (const client of sseClients) {
    try {
      client.end();
    } catch {
      // Ignore close error
    }
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 500).unref();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
