importScripts("reel_store.js");
/**
 * Ghost Bridge — Background Service Worker
 *
 * Connects to the Ghost bridge over an authenticated WebSocket.
 * Receives commands, executes them via Chrome APIs, returns results.
 */

const DEFAULT_PORT = 9377; // GHOST on a phone keypad
const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_DELAY = 30000;

let ws = null;
let reconnectDelay = RECONNECT_DELAY;
let reconnectTimer = null;
let connected = false;
let port = DEFAULT_PORT;
let token = "";
let intentionallyDisconnected = false;

function randomHex(bytes = 32) {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  return [...data].map(value => value.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, message) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return [...new Uint8Array(signature)].map(value => value.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) return false;
  let different = 0;
  for (let index = 0; index < left.length; index += 1) {
    different |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return different === 0;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

function getStatus() {
  return { connected, paired: Boolean(token), helper, installer_url: INSTALLER_URL, port, version: chrome.runtime.getManifest().version };
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}

// ---------------------------------------------------------------------------
// WebSocket connection to mia-browser-use daemon
// ---------------------------------------------------------------------------

const NATIVE_HOST = "com.ghost.bridge";
// Where the Mac installer lives. The extension can't install software itself (Chrome
// doesn't allow it), so this is the one download a person makes; the installer puts the
// helper in place and the extension connects by itself a few seconds later.
const INSTALLER_URL = "https://github.com/luislozanogmia/mia-browser-use/releases/latest/download/Mia-Browser-Use.pkg";
const SETUP_RETRY_DELAY = 5000;
let pairing = null;
let bridgeJustStarted = false;
// What the last pairing attempt learned about the helper on this computer:
// "unknown" (never asked), "ok", "missing" (installer never ran), "outdated" (an
// older helper that doesn't know this extension), "failed" (it ran but didn't answer).
let helper = "unknown";

function classifyHelperError(message) {
  const text = String(message || "").toLowerCase();
  if (text.includes("not found")) return "missing";
  if (text.includes("forbidden")) return "outdated";
  return "failed";
}

// Ask the local Ghost install for the token (see native_host.py). It also
// starts the bridge when it isn't running. Chrome only lets this extension
// reach that program, and the token never enters a page.
function pairAutomatically() {
  pairing ??= new Promise(resolve => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: "pair" }, reply => {
        const ok = !chrome.runtime.lastError && reply?.ok && typeof reply.token === "string" && reply.token.length >= 32;
        helper = ok ? "ok" : classifyHelperError(chrome.runtime.lastError?.message || reply?.error);
        bridgeJustStarted = Boolean(ok && reply.bridge === "started");
        if (ok && reply.token !== token) {
          token = reply.token;
          if (Number.isInteger(reply.port)) port = reply.port;
          chrome.storage.local.set({ port, token });
        }
        resolve(Boolean(ok));
      });
    } catch {
      resolve(false);
    }
  }).finally(() => { pairing = null; });
  return pairing;
}

async function connect() {
  if (!token && !(await pairAutomatically())) {
    setBadge("PAIR", "#f59e0b");
    // Keep asking every few seconds: the person is probably running the installer
    // right now, and the panel should turn green on its own when it finishes.
    reconnectDelay = SETUP_RETRY_DELAY;
    scheduleReconnect();
    return;
  }
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  intentionallyDisconnected = false;
  let socket;
  let authState = null;
  try {
    socket = new WebSocket(`ws://127.0.0.1:${port}/ghost-bridge`);
    ws = socket;
  } catch (err) {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    const clientNonce = randomHex();
    authState = { clientNonce, serverNonce: "", serverVerified: false };
    socket.send(JSON.stringify({ type: "auth_init", client_nonce: clientNonce }));
  };

  socket.onmessage = async (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "auth_challenge" && authState && !connected) {
      const serverNonce = typeof msg.server_nonce === "string" ? msg.server_nonce : "";
      if (!/^[0-9a-f]{64}$/.test(serverNonce)) {
        socket.close(4003, "invalid server challenge");
        return;
      }
      const expected = await hmacHex(
        token,
        `ghost-ws-server-v1:${authState.clientNonce}:${serverNonce}`,
      );
      if (!constantTimeEqual(msg.server_proof, expected)) {
        socket.close(4003, "untrusted bridge server");
        // The bridge may have a new token; take it from the local install.
        pairAutomatically();
        return;
      }
      authState.serverNonce = serverNonce;
      authState.serverVerified = true;
      const clientProof = await hmacHex(
        token,
        `ghost-ws-client-v1:${authState.clientNonce}:${serverNonce}`,
      );
      socket.send(JSON.stringify({ type: "auth_response", client_proof: clientProof }));
      return;
    }
    if (msg.type === "authenticated") {
      if (!authState?.serverVerified) {
        socket.close(4003, "bridge server was not authenticated");
        return;
      }
      connected = true;
      reconnectDelay = RECONNECT_DELAY;
      setBadge("ON", "#22c55e");
      socket.send(JSON.stringify({ type: "hello", source: "ghost-bridge", version: chrome.runtime.getManifest().version }));
      return;
    }
    if (!connected) return;
    if (msg.type === "shared_pages") {
      await setSharedPages(msg.urls);
      shareOpenTabs();
      return;
    }
    if (!msg.id || !msg.command) return;

    const args = msg.args || {};
    try {
      const result = await handleCommand(msg.command, args);
      socket.send(JSON.stringify({ id: msg.id, result, meta: await tabMeta(args.tab_id ?? result?.tab_id ?? result?.id) }));
    } catch (err) {
      socket.send(JSON.stringify({ id: msg.id, error: err.message || String(err) }));
    }
  };

  socket.onclose = async () => {
    if (ws !== socket) return;
    ws = null;
    const wasConnected = connected;
    connected = false;
    setBadge(token ? "OFF" : "PAIR", token ? "#ef4444" : "#f59e0b");
    console.log("[ghost-bridge] disconnected");
    if (intentionallyDisconnected) return;
    // No bridge answered: have the local install start it, then retry right away.
    if (!wasConnected && await pairAutomatically() && bridgeJustStarted) {
      reconnectDelay = RECONNECT_DELAY;
      connect();
      return;
    }
    scheduleReconnect();
  };

  socket.onerror = () => {
    // onclose will fire after this
  };
}

function disconnect({ forgetToken = false } = {}) {
  intentionallyDisconnected = true;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  const current = ws;
  ws = null;
  if (current) current.close();
  connected = false;
  if (forgetToken) {
    token = "";
    chrome.storage.local.remove("token");
  }
  setBadge(token ? "OFF" : "PAIR", token ? "#ef4444" : "#f59e0b");
}

function scheduleReconnect() {
  if (intentionallyDisconnected) return;
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
}

// ---------------------------------------------------------------------------
// Command router — maps mia-browser-use tool names to Chrome APIs
// ---------------------------------------------------------------------------

async function routeCommand(command, args) {
  switch (command) {
    case "ping":
      return { pong: true, ts: Date.now() };

    case "ghost_tab_list":
      return tabList(args);

    case "ghost_tab_open":
      return tabOpen(args);

    case "ghost_tab_switch":
      return tabSwitch(args);

    case "ghost_tab_close":
      return tabClose(args);

    case "ghost_navigate":
    case "ghost_vacuum":
      return navigate(args);

    case "ghost_read":
      return readPage(args);

    case "ghost_pdf_fetch":
      return fetchPdf(args);

    case "ghost_click":
      return click(args);

    case "ghost_fill":
      return fill(args);

    case "ghost_key":
      return sendKey(args);

    case "ghost_eval":
      return evaluate(args);

    case "ghost_extract":
      return extract(args);

    case "ghost_screenshot":
      return screenshot(args);

    case "ghost_sheet_append":
      return sheetAppend(args);
    case "ghost_scroll":
      return scroll(args);

    case "ghost_wait":
      return wait(args);

    case "ghost_show":
      return showPresence(args);

    case "ghost_suggest":
      return suggestHere(args);

    // Internal: the bridge draws suggestions that arrive from the room.
    case "ghost_suggestion":
      return drawSuggestion(args);

    // Internal: a bot's report for this browser's reel.
    case "ghost_reel_add":
      return reelAddReport(args);

    // Internal: the words for the reel's PDF, written by the bridge's model.
    case "ghost_reel_story":
      chrome.runtime.sendMessage({ type: "reel-story-done", id: args.id, story: args.story, error: args.error }).catch(() => {});
      return { ok: true };

    // Internal: the bridge's chat (messages, tasks, approvals) for Mia's side panel.
    case "ghost_chat_state":
      chatState = args;
      chrome.runtime.sendMessage({ type: "chat-state", state: args }).catch(() => {});
      return { ok: true };

    default:
      throw new Error(`Unknown command: ${command}`);
  }
}

// ---------------------------------------------------------------------------
// Multiplayer rules — several bots and humans share this browser
// ---------------------------------------------------------------------------

const DEFAULT_ACTOR = "_local";
const ACTOR_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
// Commands an actor may send without naming a tab.
const TABLESS_COMMANDS = new Set(["ping", "ghost_tab_list", "ghost_tab_open", "ghost_status"]);

function isActorCall(args) {
  return typeof args.actor_id === "string" && args.actor_id !== "";
}

function actorOf(args) {
  return isActorCall(args) ? args.actor_id : DEFAULT_ACTOR;
}

function typedError(code, message) {
  return new Error(`${code}: ${message}`);
}

// A human is looking at a tab when it is the selected tab of the focused window.
async function humanIsViewing(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active) return false;
  const win = await chrome.windows.get(tab.windowId).catch(() => null);
  return Boolean(win && win.focused);
}

async function refuseIfHumanViewing(args, tabId, code, what) {
  if (isActorCall(args) && !args.human_ok && await humanIsViewing(tabId)) {
    throw typedError(code, `A human is viewing tab ${tabId}; ${what}`);
  }
}

// Chrome reports tab lifecycle problems as plain messages; give callers typed errors.
function lifecycleError(err, tabId) {
  const message = err && err.message ? err.message : String(err);
  if (/^[A-Z_]+: /.test(message)) return err;
  if (/No tab with id/i.test(message)) return typedError("TAB_NOT_FOUND", `Tab ${tabId ?? ""} does not exist or was closed`);
  if (/tab was closed|Tabs cannot be edited/i.test(message)) return typedError("TAB_CLOSED", message);
  if (/Frame with ID \d+ (was removed|is showing error page)|document was replaced/i.test(message)) {
    return typedError("TAB_NAVIGATED", "The page changed during the call; read it again");
  }
  return err;
}

async function handleCommand(command, args) {
  if (isActorCall(args)) {
    if (!ACTOR_RE.test(args.actor_id)) throw typedError("INVALID_ACTOR", "actor_id must be 1-64 of A-Z a-z 0-9 _ . : -");
    if (!TABLESS_COMMANDS.has(command) && !Number.isInteger(args.tab_id)) {
      throw typedError("TAB_REQUIRED", `${command} needs tab_id when called by an actor`);
    }
    if (command === "ghost_tab_switch") {
      throw typedError("FORBIDDEN_FOR_ACTOR", "Actors never change which tab a human sees");
    }
  }
  try {
    return await routeCommand(command, args);
  } catch (err) {
    throw lifecycleError(err, args.tab_id);
  }
}

// ---------------------------------------------------------------------------
// Tab management
// ---------------------------------------------------------------------------

async function tabList() {
  const tabs = await chrome.tabs.query({});
  const focusedWindow = await chrome.windows.getLastFocused().catch(() => null);
  return {
    tabs: tabs.map((t, i) => ({
      index: i,
      id: t.id,
      url: t.url,
      title: t.title,
      active: t.active,
      focused: Boolean(focusedWindow && t.windowId === focusedWindow.id),
      windowId: t.windowId,
    })),
  };
}

async function tabOpen(args) {
  // Actors open tabs in the background so the human's view never changes.
  const active = isActorCall(args) ? false : args.active !== false;
  const tab = await chrome.tabs.create({ url: args.url || "about:blank", active });
  return { id: tab.id, url: tab.url, title: tab.title };
}

async function tabSwitch(args) {
  let tabId;
  if (args.tab_id) {
    tabId = args.tab_id;
  } else if (args.tab_index !== undefined) {
    const tabs = await chrome.tabs.query({});
    const tab = tabs[args.tab_index];
    if (!tab) throw new Error(`Tab index ${args.tab_index} not found (${tabs.length} tabs open)`);
    tabId = tab.id;
  } else {
    throw new Error("Provide tab_id or tab_index");
  }
  const tab = await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return { id: tab.id, url: tab.url, title: tab.title };
}

async function tabClose(args) {
  if (args.tab_id) {
    await chrome.tabs.remove(args.tab_id);
  } else if (args.tab_index !== undefined) {
    const tabs = await chrome.tabs.query({});
    const tab = tabs[args.tab_index];
    if (!tab) throw new Error(`Tab index ${args.tab_index} not found`);
    await chrome.tabs.remove(tab.id);
  } else {
    throw new Error("Provide tab_id or tab_index");
  }
  return { closed: true };
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

async function getActiveTabId(args) {
  if (args.tab_id) return args.tab_id;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("No active tab");
  return tab.id;
}

async function navigate(args) {
  const url = args.url;
  if (!url) throw new Error("url is required");
  // These download a file into the person's Downloads folder. Reading the sheet's own tab returns its cells.
  if (isSheetExport(url)) {
    throw new Error("NO_DOWNLOADS: export links download a file. Open the sheet itself and ghost_read it: the read includes its cells.");
  }

  let tabId;
  if (args.tab_id) {
    tabId = args.tab_id;
  } else {
    // Use active tab or create new one
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    tabId = active ? active.id : (await chrome.tabs.create({ url })).id;
  }

  const tab0 = await chrome.tabs.get(tabId);
  if (tab0.url !== url) {
    await refuseIfHumanViewing(args, tabId, "HUMAN_VIEWING", "ask them before navigating it away");
    // Only a local, single-user call brings the tab to the front.
    await chrome.tabs.update(tabId, isActorCall(args) ? { url } : { url, active: true });
    await waitForTabLoad(tabId, args.timeout || 30000);
  }

  const tab = await chrome.tabs.get(tabId);

  // If vacuum-style, also read the page
  if (args.command === "ghost_vacuum" || args.limit) {
    const read = await readTabContent(tabId, args.limit || 30, args.selector, actorOf(args));
    return { id: tab.id, url: tab.url, title: tab.title, content: await withSheetCells(tab, read.text, args.limit || 30), snapshot: read.snapshot };
  }

  return { id: tab.id, url: tab.url, title: tab.title };
}

// -- Google Sheets: the grid is drawn on a canvas, so the page has no cell text to read ----------

const SHEET_RE = /^https:\/\/docs\.google\.com\/spreadsheets\/(?:u\/\d+\/)?d\/([A-Za-z0-9_-]{20,})/;

const SHEET_CHARS = 30000;  // a whole tracker tab, so a bot can check names against it in one read

function isSheetExport(url) {
  return /^https:\/\/docs\.google\.com\/spreadsheets\//.test(url) && /\/export\b|\/gviz\/|[?&]output=csv|[?&]format=(csv|xlsx|pdf|ods|tsv)/.test(url);
}

// The open sheet's cells as CSV, fetched with the person's own Google session (nothing is downloaded).
async function sheetCells(url, maxChars) {
  const match = SHEET_RE.exec(url || "");
  if (!match) return "";
  const gid = /[#&?]gid=(\d+)/.exec(url)?.[1];
  const response = await fetch(`https://docs.google.com/spreadsheets/d/${match[1]}/export?format=csv${gid ? `&gid=${gid}` : ""}`,
    { credentials: "include", cache: "no-store", redirect: "follow" });
  const type = response.headers.get("content-type") || "";
  if (!response.ok || type.includes("html")) return "";
  const reader = response.body.getReader();
  let text = "", decoder = new TextDecoder();
  while (text.length < maxChars) {
    const { done, value } = await reader.read();
    if (done) break;
    text += decoder.decode(value, { stream: true });
  }
  reader.cancel().catch(() => {});
  return text.slice(0, maxChars);
}

// -- Adding a row to a Google Sheet: read it, paste the row after the last one, read it back ---------

function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else if (c !== "\r") cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

// A link the same however it was copied: no protocol, www, query, # part or trailing slash, any case.
function sameKey(text) {
  return decodeURIComponent(String(text || "").trim()).toLowerCase()
    .replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/[?#].*$/, "").replace(/\/+$/, "");
}

async function sheetRows(id, gid) {
  const csv = await sheetCells(`https://docs.google.com/spreadsheets/d/${id}/edit#gid=${gid}`, 5_000_000);
  if (!csv) throw new Error("SHEET_UNREADABLE: couldn't read the sheet; is this Chrome signed in to a Google account that can edit it?");
  return parseCsv(csv);
}

async function sheetColumns(link) {
  const match = SHEET_RE.exec(link);
  if (!match) throw new Error("Put a Google Sheets link in the spreadsheet box first");
  const rows = await sheetRows(match[1], /[#&?]gid=(\d+)/.exec(link)?.[1] || "0");
  const width = Math.min(26, Math.max(0, ...rows.map(r => r.length)));
  const headers = Array.from({ length: width }, (_, i) => (rows[0]?.[i] || "").trim());
  // Each column's different values, most used first.
  const values = headers.map((_, i) => {
    const count = new Map();
    for (const row of rows.slice(1)) {
      const v = (row[i] || "").trim();
      if (v) count.set(v, (count.get(v) || 0) + 1);
    }
    return [...count].sort((a, b) => b[1] - a[1]).map(([v]) => v).slice(0, 200);
  });
  return { headers, values };
}

// The tab the person named, found by name in the open sheet: the link's gid often points at
// whichever tab was open when the link was copied. Returns the gid of the named tab.
async function sheetTabGid(tabId, id, linkGid, tabName) {
  const want = String(tabName || "").trim();
  if (!want) return linkGid;
  await chrome.tabs.update(tabId, { url: `https://docs.google.com/spreadsheets/d/${id}/edit#gid=${linkGid}` });
  await waitForTabLoad(tabId, 20000);
  let names = [], back = null;
  for (let attempt = 0; attempt < 30; attempt++) {
    await new Promise(r => setTimeout(r, 500));
    const [run] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (want) => {
        const tabs = [...document.querySelectorAll(".docs-sheet-tab")];
        const nameOf = t => t.querySelector(".docs-sheet-tab-name")?.textContent?.trim() || "";
        const names = tabs.map(nameOf).filter(Boolean);
        if (!names.length) return { names, hidden: document.hidden };
        const open = document.querySelector(".docs-sheet-active-tab");
        const gid = /gid=(\d+)/.exec(location.hash)?.[1] || "";
        if (open && nameOf(open).toLowerCase() === want.toLowerCase()) return { names, gid };
        const target = tabs.find(t => nameOf(t).toLowerCase() === want.toLowerCase());
        if (!target) return { names, missing: true };
        // Sheets switches tabs on the mouse going down on the tab's name.
        const el = target.querySelector(".docs-sheet-tab-name") || target;
        for (const type of ["mousedown", "mouseup", "click"]) {
          el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0 }));
        }
        return { names, switching: true, hidden: document.hidden };
      },
      args: [want],
    });
    const result = run?.result || {};
    names = result.names || names;
    if (result.gid) {
      if (back && back.id !== tabId) chrome.tabs.update(back.id, { active: true }).catch(() => {});
      return result.gid;
    }
    if (result.missing) {
      throw new Error(`SHEET_NO_SUCH_TAB: the spreadsheet has no tab called “${want}”; its tabs are ${names.map(n => `“${n}”`).join(", ")}. Nothing was written.`);
    }
    if (attempt === 8 && result.hidden) {
      // Sheets doesn't draw, or switch tabs, while hidden: show it briefly, then go back.
      [back] = await chrome.tabs.query({ active: true, windowId: (await chrome.tabs.get(tabId)).windowId });
      await chrome.tabs.update(tabId, { active: true });
    }
  }
  throw new Error(`SHEET_NOT_READY: couldn't open the tab “${want}” (saw tabs ${names.map(n => `“${n}”`).join(", ") || "none"}); nothing was written`);
}

async function sheetAppend(args) {
  const match = SHEET_RE.exec(String(args.sheet || ""));
  if (!match) throw new Error("sheet must be a Google Sheets link (https://docs.google.com/spreadsheets/d/…)");
  const id = match[1];
  const linkGid = /[#&?]gid=(\d+)/.exec(args.sheet)?.[1] || "0";
  // One line per row: a tab or line break inside a value would spill into other cells.
  const values = (Array.isArray(args.row) ? args.row : []).slice(0, 26).map(v => String(v ?? "").replace(/[\t\r\n]+/g, " ").trim());
  if (!values.some(Boolean)) throw new Error("row has no values");
  const width = values.length;
  const tabId = args.tab_id ?? (await chrome.tabs.create({ url: "about:blank", active: false })).id;
  const own = args.tab_id === undefined;
  try {
    const gid = await sheetTabGid(tabId, id, linkGid, args.tab_name);
    const rows = await sheetRows(id, gid);
    if (args.unique) {
      const key = sameKey(args.unique);
      const at = rows.findIndex(r => r.some(cell => cell && sameKey(cell) === key));
      if (at >= 0) return { added: false, already: true, row: at + 1 };
    }
    // The row after the last one with anything in the row's columns (a gap higher up isn't the end).
    let last = rows.length;
    while (last > 0 && !rows[last - 1].slice(0, width).some(cell => cell.trim())) last--;
    const target = last + 1;
    // The sheet with the cursor on the first cell of the new row.
    await chrome.tabs.update(tabId, { url: `https://docs.google.com/spreadsheets/d/${id}/edit#gid=${gid}&range=A${target}` });
    await waitForTabLoad(tabId, 20000);
    let pasted = "", seen = {}, back = null;
    for (let attempt = 0; attempt < 30 && !pasted; attempt++) {
      await new Promise(r => setTimeout(r, 500));
      if (attempt === 8 && seen.hidden) {
        // Chrome doesn't draw hidden tabs, and Sheets needs to draw before it takes a paste: show the
        // sheet for a moment, then go back to the tab the person was on.
        [back] = await chrome.tabs.query({ active: true, windowId: (await chrome.tabs.get(tabId)).windowId });
        await chrome.tabs.update(tabId, { active: true });
      }
      const [run] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (tsv, tabName) => {
          const name = document.querySelector("#t-name-box");
          const frame = document.querySelector(".docs-texteventtarget-iframe");
          // Sheets takes typing and pastes in its cell editor (older Sheets: a hidden editable frame);
          // pasting there is a ⌘V on the selected cell.
          const box = document.querySelector("#waffle-rich-text-editor")
            || frame?.contentDocument?.querySelector("[contenteditable='true']") || frame?.contentDocument?.body;
          const open = document.querySelector(".docs-sheet-active-tab .docs-sheet-tab-name")?.textContent?.trim() || "";
          const seen = { cell: name?.value || "", editor: !!box, tab: open, hidden: document.hidden };
          if (!box || !/^A\d+$/.test(seen.cell)) return { seen };
          // The open tab must be the one the person named: the link's gid could point at another.
          if (tabName && open && open.toLowerCase() !== tabName.toLowerCase()) return { seen, wrongTab: open };
          box.focus();
          const data = new DataTransfer();
          data.setData("text/plain", tsv);
          box.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
          return { seen, pasted: seen.cell };
        },
        args: [values.join("\t"), String(args.tab_name || "").trim()],
      });
      const result = run?.result || {};
      seen = result.seen || seen;
      if (result.wrongTab) {
        throw new Error(`SHEET_WRONG_TAB: the sheet opened on the tab “${result.wrongTab}”, not “${args.tab_name}”; nothing was written`);
      }
      pasted = result.pasted || "";
    }
    if (back && back.id !== tabId) chrome.tabs.update(back.id, { active: true }).catch(() => {});
    if (!pasted) throw new Error(`SHEET_NOT_READY: the sheet didn't open on the new row (saw cell “${seen.cell || "none"}”, editor ${seen.editor ? "found" : "missing"}, tab “${seen.tab || "?"}”${seen.hidden ? ", tab hidden" : ""})`);
    if (pasted !== `A${target}`) throw new Error(`SHEET_WRONG_CELL: the sheet opened on ${pasted}, not A${target}; nothing was checked`);
    // Sheets saves in the background: read the row back until it's there.
    for (let attempt = 0; attempt < 16; attempt++) {
      await new Promise(r => setTimeout(r, 750));
      const now = (await sheetRows(id, gid))[target - 1] || [];
      if (values.every((v, i) => (now[i] || "").trim() === v)) return { added: true, row: target, values };
    }
    throw new Error(`SHEET_NOT_SAVED: pasted into row ${target} but reading it back didn't show the row; check the sheet`);
  } finally {
    if (own) chrome.tabs.remove(tabId).catch(() => {});
  }
}

async function withSheetCells(tab, text, maxChars) {
  if (!SHEET_RE.test(tab.url || "")) return text;
  let cells = "";
  try { cells = await sheetCells(tab.url, SHEET_CHARS); } catch {}
  return cells
    ? `Cells of the open sheet tab, as CSV (first row is usually the header)${cells.length >= SHEET_CHARS ? ", cut off: the tab is bigger" : ""}:\n${cells}\n\nThe page around it:\n${text}`
    : `${text}\n\n(The sheet's cells are drawn on a canvas and couldn't be fetched: the person may not have access.)`;
}

function waitForTabLoad(tabId, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(); // Don't fail on timeout, page may be usable
    }, timeout);

    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        // Small delay for JS to settle
        setTimeout(resolve, 200);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ---------------------------------------------------------------------------
// Page reading
// ---------------------------------------------------------------------------

async function readPage(args) {
  const tabId = await getActiveTabId(args);
  const read = await readTabContent(tabId, args.max_chars || 4000, args.selector, actorOf(args));
  const tab = await chrome.tabs.get(tabId);
  return { url: tab.url, title: tab.title, content: await withSheetCells(tab, read.text, args.max_chars || 4000), snapshot: read.snapshot };
}

async function fetchPdf(args) {
  const tabId = await getActiveTabId(args);
  const tab = await chrome.tabs.get(tabId);
  const sourceUrl = new URL(tab.url || "");
  if (!['http:', 'https:'].includes(sourceUrl.protocol)) {
    throw new Error(`UNSUPPORTED_PDF_SOURCE: ${sourceUrl.protocol || 'unknown'} URLs are not supported`);
  }

  const uploadUrl = new URL(args.upload_url || "");
  const expectedPort = String(port + 1);
  const expectedPath = `/pdf-upload/${args.upload_token}`;
  if (
    uploadUrl.protocol !== "http:" ||
    uploadUrl.hostname !== "127.0.0.1" ||
    uploadUrl.port !== expectedPort ||
    uploadUrl.pathname !== expectedPath ||
    uploadUrl.search ||
    uploadUrl.hash
  ) {
    throw new Error("INVALID_UPLOAD_URL: PDF bytes may only be uploaded to the Ghost loopback bridge");
  }

  const maxBytes = Number(args.max_bytes) || 50 * 1024 * 1024;
  const response = await fetch(sourceUrl.href, {
    credentials: "include",
    cache: "no-store",
    redirect: "follow",
  });
  if (!response.ok) {
    throw new Error(`PDF_FETCH_FAILED: HTTP ${response.status} ${response.statusText}`);
  }
  const declaredLength = Number(response.headers.get("content-length")) || 0;
  if (declaredLength > maxBytes) {
    throw new Error(`PDF_TOO_LARGE: content-length ${declaredLength} exceeds ${maxBytes} bytes`);
  }

  const chunks = [];
  let byteLength = 0;
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    byteLength += value.byteLength;
    if (byteLength > maxBytes) {
      await reader.cancel();
      throw new Error(`PDF_TOO_LARGE: download exceeds ${maxBytes} bytes`);
    }
    chunks.push(value);
  }

  const pdfBytes = new Uint8Array(byteLength);
  let offset = 0;
  for (const chunk of chunks) {
    pdfBytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let signatureOffset = 0;
  while (signatureOffset < pdfBytes.length && [9, 10, 12, 13, 32].includes(pdfBytes[signatureOffset])) {
    signatureOffset += 1;
  }
  const signature = String.fromCharCode(...pdfBytes.slice(signatureOffset, signatureOffset + 5));
  if (signature !== "%PDF-") {
    throw new Error("NOT_A_PDF: downloaded content does not have a PDF signature");
  }

  const upload = await fetch(uploadUrl.href, {
    method: "POST",
    headers: {
      "Content-Type": "application/pdf",
      "X-Ghost-PDF-Token": args.upload_token,
    },
    body: pdfBytes,
  });
  if (!upload.ok) {
    const detail = await upload.text();
    throw new Error(`PDF_UPLOAD_FAILED: HTTP ${upload.status} ${detail.slice(0, 300)}`);
  }

  return {
    id: tab.id,
    url: tab.url,
    title: tab.title,
    content_type: response.headers.get("content-type") || "application/pdf",
    bytes: byteLength,
  };
}

// A new id each time the extension is installed or reloaded. Pages keep the
// scripts an older copy injected; the new scripts see the id change and replace them.
chrome.runtime.onInstalled.addListener(details => {
  buildId = Promise.resolve(String(Date.now()));
  chrome.storage.local.set({ build: String(Date.now()) });
  // First install (from the store or unpacked): show the one remaining step.
  if (details?.reason === "install") chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html") });
});
let buildId = chrome.storage.local.get("build").then(data => data.build || "0");

async function injectScripts(tabId, files) {
  const build = await buildId;
  await chrome.scripting.executeScript({ target: { tabId }, func: b => { globalThis.__ghostBuild = b; }, args: [build] });
  await chrome.scripting.executeScript({ target: { tabId }, files });
}

async function injectPageHelpers(tabId) {
  await injectScripts(tabId, ["ghost_page.js", "mote_image.js", "overlay.js"]);
}

// Run a function in the page's isolated world after the Ghost helpers are
// loaded. Chrome serializes `func`; it must catch its own errors and return
// {value} or {error}, because MV3 isolated worlds do not allow eval.
async function runInPage(tabId, func, args) {
  await injectPageHelpers(tabId);
  const [result] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  if (!result) throw new Error("The page did not answer");
  if (result.result?.error) throw new Error(result.result.error);
  return result.result?.value;
}

async function readTabContent(tabId, maxChars, selector, actor) {
  return runInPage(
    tabId,
    (actor, max, sel) => {
      try { return { value: globalThis.__ghostPage.enumerate(actor, max, sel) }; } catch (err) { return { error: err.message }; }
    },
    [actor, maxChars, selector || null],
  );
}

// ---------------------------------------------------------------------------
// Click
// ---------------------------------------------------------------------------

async function click(args) {
  const tabId = await getActiveTabId(args);
  if (args.choice === undefined && !args.selector) throw new Error("Provide choice (number) or selector");

  const result = await runInPage(
    tabId,
    (actor, choice, selector, announce) => {
      try {
        const done = globalThis.__ghostPage.click(actor, choice, selector);
        // Each actor action also moves that actor's ring to what it touched.
        if (announce) try { globalThis.__ghostOverlay.show({ actor_id: actor, choice, selector, status: "working" }); } catch {}  // the marker never fails the action
        return { value: done };
      } catch (err) {
        return { error: err.message };
      }
    },
    [actorOf(args), args.choice ?? null, args.selector || null, isActorCall(args)],
  );

  // Wait for potential navigation
  if (args.wait) {
    await new Promise(r => setTimeout(r, args.wait === "networkidle" ? 2000 : (parseInt(args.wait) || 1000)));
  }
  return result;
}

// ---------------------------------------------------------------------------
// Fill / Type
// ---------------------------------------------------------------------------

async function fill(args) {
  const tabId = await getActiveTabId(args);
  const { selector, choice, value } = args;
  if (!value && value !== "") throw new Error("value is required");
  if (choice === undefined && !selector) throw new Error("Provide choice (number) or selector");

  return runInPage(
    tabId,
    (actor, choice, selector, value, announce) => {
      try {
        // Focus-free: sets the value directly, so a human typing elsewhere keeps their cursor.
        const done = globalThis.__ghostPage.fill(actor, choice, selector, value);
        if (announce) try { globalThis.__ghostOverlay.show({ actor_id: actor, choice, selector, status: "working" }); } catch {}  // the marker never fails the action
        return { value: done };
      } catch (err) {
        return { error: err.message };
      }
    },
    [actorOf(args), choice ?? null, selector || null, value, isActorCall(args)],
  );
}

// ---------------------------------------------------------------------------
// Keyboard
// ---------------------------------------------------------------------------

async function sendKey(args) {
  const tabId = await getActiveTabId(args);
  const targeted = args.choice !== undefined || Boolean(args.selector);

  // Text aimed at one element is written there without focus, like fill.
  if (args.text && targeted) {
    return runInPage(
      tabId,
      (actor, choice, selector, text, announce) => {
        try {
          const done = globalThis.__ghostPage.typeInto(actor, choice, selector, text);
          if (announce) try { globalThis.__ghostOverlay.show({ actor_id: actor, choice, selector, status: "working" }); } catch {}  // the marker never fails the action
          return { value: done };
        } catch (err) {
          return { error: err.message };
        }
      },
      [actorOf(args), args.choice ?? null, args.selector || null, args.text, isActorCall(args)],
    );
  }

  // A key aimed at one element is pressed there, without moving focus; Enter submits its form.
  if (args.key && targeted) {
    return runInPage(
      tabId,
      (actor, choice, selector, key, announce) => {
        try {
          const el = globalThis.__ghostPage.resolve(actor, choice, selector);
          const opts = { key, code: key.length === 1 ? undefined : key, bubbles: true, cancelable: true };
          const go = el.dispatchEvent(new KeyboardEvent("keydown", opts));
          el.dispatchEvent(new KeyboardEvent("keyup", opts));
          // A page that handles Enter itself cancels the keydown; otherwise the form is sent.
          if (go && key === "Enter" && el.form) el.form.requestSubmit();
          if (announce) try { globalThis.__ghostOverlay.show({ actor_id: actor, choice, selector, status: "working" }); } catch {}  // the marker never fails the action
          return { value: { key, pressed: true, anchor: globalThis.__ghostPage.anchorOf(el) } };
        } catch (err) {
          return { error: err.message };
        }
      },
      [actorOf(args), args.choice ?? null, args.selector || null, args.key, isActorCall(args)],
    );
  }

  // Untargeted keys go to whatever has focus, which may be the human's cursor.
  await refuseIfHumanViewing(args, tabId, "HUMAN_ACTIVE", "target an element with choice or selector instead of the focused one");

  // If it's text to type, use a different approach
  if (args.text) {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (text) => {
        const el = document.activeElement || document.body;
        if (el.isContentEditable) {
          // A rich editor (LinkedIn, Gmail, Slack): paste at the cursor like the person would, so
          // every line break and blank line stays; editors that ignore a paste get the lines typed.
          const before = el.innerText.length;
          const data = new DataTransfer();
          data.setData("text/plain", text);
          el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
          if (el.innerText.length <= before) {
            text.split("\n").forEach((line, i) => {
              if (i) document.execCommand("insertParagraph");
              if (line) document.execCommand("insertText", false, line);
            });
          }
          return;
        }
        for (const char of text) {
          el.dispatchEvent(new KeyboardEvent("keydown", { key: char, bubbles: true }));
          el.dispatchEvent(new KeyboardEvent("keypress", { key: char, bubbles: true }));
          if (el.value !== undefined) el.value += char;
          el.dispatchEvent(new InputEvent("input", { data: char, inputType: "insertText", bubbles: true }));
          el.dispatchEvent(new KeyboardEvent("keyup", { key: char, bubbles: true }));
        }
      },
      args: [args.text],
    });
    return { typed: true, characters: args.text.length };
  }

  // Single key press
  const key = args.key;
  if (!key) throw new Error("Provide key or text");

  await chrome.scripting.executeScript({
    target: { tabId },
    func: (key) => {
      const el = document.activeElement || document.body;
      const opts = { key, bubbles: true, cancelable: true };

      // Map special keys
      if (key === "Enter") opts.code = "Enter";
      else if (key === "Escape") opts.code = "Escape";
      else if (key === "Tab") opts.code = "Tab";
      else if (key === "Backspace") opts.code = "Backspace";
      else if (key === "ArrowDown") opts.code = "ArrowDown";
      else if (key === "ArrowUp") opts.code = "ArrowUp";

      el.dispatchEvent(new KeyboardEvent("keydown", opts));
      el.dispatchEvent(new KeyboardEvent("keyup", opts));

      // Enter on forms should submit
      if (key === "Enter" && el.form) el.form.requestSubmit();
    },
    args: [key],
  });

  return { key, pressed: true };
}

// ---------------------------------------------------------------------------
// Eval — run arbitrary JS in page context
// ---------------------------------------------------------------------------

async function evaluate(args) {
  const tabId = await getActiveTabId(args);
  const script = args.script;
  if (!script) throw new Error("script is required");

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (code) => {
      try {
        const fn = new Function(`return (${code})()`);
        const result = fn();
        return { value: result };
      } catch (err) {
        return { error: err.message };
      }
    },
    args: [script],
  });

  if (!results || !results[0]) throw new Error("Eval failed");
  if (results[0].result?.error) throw new Error(results[0].result.error);
  return results[0].result;
}

// ---------------------------------------------------------------------------
// Extract — structured extraction recipes
// ---------------------------------------------------------------------------

async function extract(args) {
  const tabId = await getActiveTabId(args);
  const recipe = args.recipe || "page_links";

  const recipes = {
    page_links: () => {
      return [...document.querySelectorAll("a[href]")].map(a => ({
        text: a.textContent.trim().slice(0, 100),
        href: a.href,
      })).filter(l => l.text && l.href.startsWith("http"));
    },
    page_images: () => {
      return [...document.querySelectorAll("img[src]")].map(img => ({
        src: img.src,
        alt: img.alt || "",
        width: img.naturalWidth,
        height: img.naturalHeight,
      }));
    },
    page_forms: () => {
      return [...document.querySelectorAll("form")].map(form => ({
        action: form.action,
        method: form.method,
        fields: [...form.elements].map(el => ({
          tag: el.tagName.toLowerCase(),
          type: el.type || "",
          name: el.name || "",
          id: el.id || "",
          placeholder: el.placeholder || "",
        })),
      }));
    },
    page_headings: () => {
      return [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")].map(h => ({
        level: parseInt(h.tagName[1]),
        text: h.textContent.trim(),
      }));
    },
    page_meta: () => {
      return {
        title: document.title,
        description: document.querySelector('meta[name="description"]')?.content || "",
        url: location.href,
        canonical: document.querySelector('link[rel="canonical"]')?.href || "",
        og: Object.fromEntries(
          [...document.querySelectorAll('meta[property^="og:"]')]
            .map(m => [m.getAttribute("property").slice(3), m.content])
        ),
      };
    },
  };

  const fn = recipes[recipe];
  if (!fn) throw new Error(`Unknown recipe: ${recipe}. Available: ${Object.keys(recipes).join(", ")}`);

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: fn,
  });

  if (!results || !results[0]) throw new Error("Extract failed");
  return { recipe, data: results[0].result };
}

// ---------------------------------------------------------------------------
// Screenshot
// ---------------------------------------------------------------------------

async function screenshot(args) {
  const tab = args.tab_id
    ? await chrome.tabs.get(args.tab_id)
    : (await chrome.tabs.query({ active: true, currentWindow: true }))[0];

  if (!tab) throw new Error("No tab to screenshot");
  if (!tab.active) {
    // captureVisibleTab only sees the selected tab; capturing another would mean switching the human's view.
    throw typedError("BACKGROUND_CAPTURE_UNSUPPORTED", `Tab ${tab.id} is not showing; Chrome can only capture the visible tab. Use ghost_read instead.`);
  }

  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: args.format || "png",
    quality: args.quality || 80,
  });

  return { dataUrl, width: tab.width, height: tab.height };
}

const MAX_CROP_CHARS = 1024 * 1024;
const MAX_CROP_SIDE = 1280;

/** A JPEG of one area of the tab the human is looking at; rect is in CSS pixels of the viewport. */
async function cropVisible(tab, rect) {
  const nums = ["x", "y", "w", "h", "vw"].map(k => Number(rect[k]));
  if (nums.some(n => !Number.isFinite(n)) || nums[2] < 1 || nums[3] < 1 || nums[4] < 1) return null;
  const [x, y, w, h, vw] = nums;
  const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  const bitmap = await createImageBitmap(await (await fetch(shot)).blob());
  const ratio = bitmap.width / vw; // device pixels per CSS pixel
  const sx = Math.max(0, Math.round(x * ratio));
  const sy = Math.max(0, Math.round(y * ratio));
  const sw = Math.min(bitmap.width - sx, Math.round(w * ratio));
  const sh = Math.min(bitmap.height - sy, Math.round(h * ratio));
  if (sw < 1 || sh < 1) return null;
  const scale = Math.min(1, MAX_CROP_SIDE / Math.max(sw, sh));
  const canvas = new OffscreenCanvas(Math.max(1, Math.round(sw * scale)), Math.max(1, Math.round(sh * scale)));
  canvas.getContext("2d").drawImage(bitmap, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  for (const quality of [0.85, 0.6, 0.4]) {
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const url = `data:image/jpeg;base64,${btoa(binary)}`;
    if (url.length <= MAX_CROP_CHARS) return url;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Scroll
// ---------------------------------------------------------------------------

async function scroll(args) {
  const tabId = await getActiveTabId(args);
  await refuseIfHumanViewing(args, tabId, "HUMAN_ACTIVE", "scrolling would move their view");
  const direction = args.direction || "down";
  const amount = args.amount || 500;

  await chrome.scripting.executeScript({
    target: { tabId },
    func: (dir, amt) => {
      const el = document.scrollingElement || document.documentElement;
      if (dir === "down") el.scrollTop += amt;
      else if (dir === "up") el.scrollTop -= amt;
      else if (dir === "top") el.scrollTop = 0;
      else if (dir === "bottom") el.scrollTop = el.scrollHeight;
      return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight };
    },
    args: [direction, amount],
  });

  return { scrolled: direction, amount };
}

// ---------------------------------------------------------------------------
// Wait
// ---------------------------------------------------------------------------

async function wait(args) {
  const tabId = await getActiveTabId(args);
  const selector = args.selector;
  const timeout = args.timeout || 10000;

  if (!selector) {
    await new Promise(r => setTimeout(r, args.ms || 1000));
    return { waited: args.ms || 1000 };
  }

  const start = Date.now();
  while (Date.now() - start < timeout) {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel) => !!(globalThis.__ghostPage?.deepQuery ? globalThis.__ghostPage.deepQuery(sel) : document.querySelector(sel)),
      args: [selector],
    });
    if (results?.[0]?.result) return { found: true, selector, elapsed: Date.now() - start };
    await new Promise(r => setTimeout(r, 200));
  }

  throw new Error(`Timeout waiting for "${selector}" after ${timeout}ms`);
}

// ---------------------------------------------------------------------------
// Presence overlay — show what an actor is working on, without editing
// ---------------------------------------------------------------------------

async function showPresence(args) {
  const tabId = await getActiveTabId(args);
  const spec = {
    actor_id: args.actor_id,
    label: args.label,
    color: args.color,
    owner_color: args.owner_color,
    kind: args.kind === "human" ? "human" : "bot",
    status: args.status,
    ttl_ms: args.ttl_ms,
    choice: args.choice,
    selector: args.selector,
    text: args.text,
    rect: args.rect,
    anchor: args.anchor,
    pointer: args.pointer,
  };
  const value = await runInPage(
    tabId,
    (spec, clear) => {
      try {
        const overlay = globalThis.__ghostOverlay;
        if (clear) return { value: overlay.clear(spec.actor_id) };
        return { value: { ...overlay.show(spec), showing: overlay.list() } };
      } catch (err) {
        return { error: err.message };
      }
    },
    [spec, Boolean(args.clear)],
  );
  return { tab_id: tabId, ...value };
}

async function suggestHere(args) {
  const tabId = await getActiveTabId(args);
  if (!isActorCall(args)) throw typedError("ACTOR_REQUIRED", "Suggestions come from an actor; set --actor or GHOST_ACTOR_ID");
  const id = typeof args.id === "string" && args.id ? args.id : `${args.actor_id}-${Date.now().toString(36)}`;
  const value = await runInPage(
    tabId,
    (spec) => {
      try {
        const overlay = globalThis.__ghostOverlay;
        overlay.onResolve ??= (id, decision) => chrome.runtime.sendMessage({ type: "resolve", id, decision });
        return { value: overlay.suggest(spec) };
      } catch (err) {
        return { error: err.message };
      }
    },
    [{
      id, actor_id: args.actor_id, actor: { id: args.actor_id, name: args.actor_id },
      title: args.title, body: args.body, choice: args.choice, selector: args.selector, text: args.text, rect: args.rect,
      anchor: args.anchor, kind: args.kind, reply_to: args.reply_to,
      thread: args.thread, href: args.href, question: args.question,
    }],
  );
  return { tab_id: tabId, ...value };
}

async function drawSuggestion(args) {
  const tabId = args.tab_id;
  // A conversation the human closed (and saved) stays closed when the page redraws.
  if (!args.clear && args.thread && closedThreads.has(args.thread)) return { skipped: "closed" };
  if (reelOn && !args.clear && args.kind === "note" && args.question) {
    // An answer: capture it once the card is on the page.
    chrome.tabs.get(tabId).then(tab => setTimeout(() => reelCapture(tab, {
      kind: "answer", question: args.question, answer: { title: args.title || "", body: args.body || "" },
      by: args.actor?.name || args.actor?.id || "bot",
    }), 700)).catch(() => {});
  }
  return runInPage(
    tabId,
    (spec, clear) => {
      try {
        const overlay = globalThis.__ghostOverlay;
        overlay.onResolve ??= (id, decision) => chrome.runtime.sendMessage({ type: "resolve", id, decision });
        if (clear) return { value: { removed: overlay.unsuggest(spec.id) } };
        return { value: overlay.suggest(spec) };
      } catch (err) {
        return { error: err.message };
      }
    },
    [{
      id: args.id, actor: args.actor, title: args.title, body: args.body, anchor: args.anchor,
      kind: args.kind, question: args.question, href: args.href,
      thread: args.thread, reply_to: args.reply_to, text: args.text,
    }, Boolean(args.clear)],
  );
}

// ---------------------------------------------------------------------------
// Shared pages: every open http(s) tab is shared with the local relay while it
// is open (one bot per tab); it is unshared when its tab closes. The relay's
// list is mirrored here so the page scripts only run where asking works.
// ---------------------------------------------------------------------------

let sharedPages = new Set();

// Same rule as ghost_room.page_key: origin + path, no query or fragment.
function pageKey(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return `${u.protocol}//${u.host}${u.pathname.replace(/\/+$/, "") || "/"}`;
  } catch {
    return null;
  }
}

function isShared(url) {
  const key = pageKey(url);
  return Boolean(key && sharedPages.has(key));
}

async function tabMeta(tabId) {
  if (!Number.isInteger(tabId)) return null;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return tab ? { tab_id: tab.id, url: tab.url, title: tab.title } : null;
}

async function trackHuman(tabId) {
  await injectScripts(tabId, ["ghost_page.js", "mote_image.js", "overlay.js", "human_presence.js"]).catch(() => {});
  await applyModes(tabId);
}

let modes = { immersive: false, skip: false };
// The language bots answer this person in.
let language = "English";

function cleanLanguage(value) {
  const text = typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, 40) : "";
  return /^[\p{L}][\p{L}\p{M} ()-]*$/u.test(text) ? text : "English";
}

async function applyModes(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId }, func: m => globalThis.__ghostOverlay?.setModes?.(m), args: [modes],
  }).catch(() => {});
}

async function untrackHuman(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      globalThis.__ghostHumanStop?.();
      globalThis.__ghostOverlay?.reset?.();
    },
  }).catch(() => {});
}

async function setSharedPages(urls) {
  const before = sharedPages;
  sharedPages = new Set(Array.isArray(urls) ? urls : []);
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    const key = pageKey(tab.url);
    if (key && before.has(key) && !sharedPages.has(key)) {
      // No longer shared: clean the page.
      await untrackHuman(tab.id);
    } else if (isShared(tab.url)) {
      await trackHuman(tab.id);
      ws?.send(JSON.stringify({ type: "tab_ready", tab_id: tab.id, url: tab.url }));
    }
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== "complete" || !connected || !ws) return;
  ws.send(JSON.stringify({ type: "tab_ready", tab_id: tabId, url: tab.url }));
  shareTab(tab);
  if (isShared(tab.url)) await trackHuman(tabId);
});

// ---------------------------------------------------------------------------
// Automatic sharing: the page in each open tab, for as long as the tab is open
// ---------------------------------------------------------------------------

const tabPages = new Map(); // tab id -> page key it currently shares

function shareTab(tab) {
  if (!tab || !Number.isInteger(tab.id)) return;
  const key = pageKey(tab.url);
  const before = tabPages.get(tab.id);
  if (before && before !== key) leavePage(tab.id, before);
  if (!key) return;
  tabPages.set(tab.id, key);
  if (!sharedPages.has(key)) toBridge({ type: "share", tab_id: tab.id, url: tab.url, title: tab.title });
}

// A tab left a page (closed or moved on): unshare it unless another tab still shows it.
function leavePage(tabId, key) {
  tabPages.delete(tabId);
  for (const other of tabPages.values()) if (other === key) return;
  toBridge({ type: "unshare", url: key });
}

async function shareOpenTabs() {
  if (!connected) return;
  for (const tab of await chrome.tabs.query({})) shareTab(tab);
}

chrome.tabs.onRemoved.addListener(tabId => {
  const key = tabPages.get(tabId);
  if (key) leavePage(tabId, key);
});

// ---------------------------------------------------------------------------
// Reel mode: a screenshot of every shared page the human visits and of every
// answer a bot gives there, stacked in reel.html in the order they happened
// ---------------------------------------------------------------------------

let reelOn = false;
let lastShot = 0;
const lastPageShot = new Map(); // page -> time, so reloads don't repeat it

async function reelCapture(tab, extra) {
  if (!reelOn || !tab?.active || !isShared(tab.url)) return;
  // Chrome allows about two captures a second.
  const wait = lastShot + 600 - Date.now();
  if (wait > 0) await new Promise(resolve => setTimeout(resolve, wait));
  lastShot = Date.now();
  try {
    const fresh = await chrome.tabs.get(tab.id);
    if (!fresh.active || pageKey(fresh.url) !== pageKey(tab.url)) return; // the human moved on
    const image = await chrome.tabs.captureVisibleTab(fresh.windowId, { format: "jpeg", quality: 70 });
    await reelAdd({ ts: Date.now(), url: fresh.url, title: fresh.title || "", image, ...extra });
    chrome.runtime.sendMessage({ type: "reel-added" }).catch(() => {});
  } catch {}
}

const closedThreads = new Set();
chrome.storage.session.get("closedThreads").then(data => (data.closedThreads || []).forEach(t => closedThreads.add(t))).catch(() => {});

async function reelAddReport(args) {
  if (args.kind !== "report" || typeof args.markdown !== "string") throw new Error("Only reports can be added");
  const id = await reelAdd({
    ts: Date.now(), kind: "report", title: String(args.title || "Session report").slice(0, 200),
    markdown: args.markdown.slice(0, 200000), by: String(args.by || "").slice(0, 64), url: "", image: null,
  });
  chrome.runtime.sendMessage({ type: "reel-added" }).catch(() => {});
  return { added: id };
}

// A conversation the human closed: saved to the reel with a picture of it.
async function saveConversation(tab, conversation) {
  closedThreads.add(conversation.thread);
  chrome.storage.session.set({ closedThreads: [...closedThreads].slice(-500) }).catch(() => {});
  let image = null;
  try {
    if (tab.active) image = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 });
  } catch {}
  const turns = (Array.isArray(conversation.turns) ? conversation.turns : []).slice(0, 100).map(t => ({
    q: String(t?.q || "").slice(0, 600), by: String(t?.by || "").slice(0, 80),
    a: t?.a ? { title: String(t.a.title || "").slice(0, 120), body: String(t.a.body || "").slice(0, 600), by: String(t.a.by || "").slice(0, 80) } : null,
  }));
  await reelAdd({ ts: Date.now(), kind: "conversation", url: tab.url, title: tab.title || "", image, turns });
  chrome.runtime.sendMessage({ type: "reel-added" }).catch(() => {});
}

function reelPage(tab) {
  const key = pageKey(tab.url);
  if (!reelOn || !key || Date.now() - (lastPageShot.get(key) || 0) < 30000) return;
  lastPageShot.set(key, Date.now());
  // Give the page a moment to draw.
  setTimeout(() => reelCapture(tab, { kind: "page" }), 1500);
}

chrome.tabs.onUpdated.addListener((_id, info, tab) => { if (info.status === "complete" && tab.active && isShared(tab.url)) reelPage(tab); });
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (tab && tab.status === "complete" && isShared(tab.url)) reelPage(tab);
});

// ---------------------------------------------------------------------------
// Message handler for the side panel and content scripts
// ---------------------------------------------------------------------------

function toBridge(message) {
  if (connected && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

// A closed tab takes its bot off Mia's list, whether or not the panel is open.
chrome.tabs.onRemoved.addListener(tabId => toBridge({ type: "chat", chat: { action: "tab_closed", tab: tabId } }));

// ---------------------------------------------------------------------------
// Mia's chat: the side panel talks to the bridge through here
// ---------------------------------------------------------------------------

let chatState = null;
const CHAT_ACTIONS = new Set(["automation_choices", "automation_full_access", "automation_step_delete", "claude_setup", "sync", "send", "approve", "reject", "stop", "stop_all", "close", "new", "open_chat", "delete_chat",
  "play", "automation_pause", "automation_resume", "automation_delete", "automation_reset"]);

// What the person is looking at: the tab, the text they selected and the links inside it.
async function chatTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return tab ? { id: null, url: tab.url || "", title: tab.title || "" } : {};
  let picked = {};
  try {
    const [run] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const sel = getSelection();
        const text = String(sel || "").slice(0, 3000);
        const links = [];
        for (let i = 0; i < sel.rangeCount && links.length < 30; i++) {
          const box = sel.getRangeAt(i).cloneContents();
          for (const a of box.querySelectorAll("a[href]")) {
            if (links.length >= 30) break;
            links.push({ text: (a.textContent || "").trim().slice(0, 100), href: a.href.slice(0, 500) });
          }
        }
        return { text, links };
      },
    });
    picked = run?.result || {};
  } catch {}
  return { id: tab.id, url: tab.url, title: tab.title || "", selection: picked.text || "", links: picked.links || [] };
}

async function chatFromPanel(msg) {
  if (!CHAT_ACTIONS.has(msg.action)) return { ok: false, error: "Unknown action" };
  if (!(connected && ws && ws.readyState === WebSocket.OPEN)) return { ok: false, error: "Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome" };
  const chat = { action: msg.action, task: typeof msg.task === "string" ? msg.task.slice(0, 32) : undefined,
                 agent: typeof msg.agent === "string" ? msg.agent.slice(0, 64) : undefined,
                 chat: typeof msg.chat === "string" ? msg.chat.slice(0, 40) : undefined,
                 automation: typeof msg.automation === "string" ? msg.automation.slice(0, 32) : undefined };
  if (msg.action === "automation_step_delete" && Number.isInteger(msg.step)) chat.step = msg.step;
  if (msg.action === "automation_full_access") chat.on = msg.on === true;
  if (msg.action === "automation_choices" && Array.isArray(msg.choices)) {
    // A drop-down's saved values: a few short strings.
    chat.input = String(msg.input || "").slice(0, 31);
    chat.choices = msg.choices.slice(0, 50).filter(c => typeof c === "string").map(c => c.slice(0, 100));
  }
  if (msg.action === "play" && msg.inputs && typeof msg.inputs === "object") {
    // What the person typed in a Play Automation's boxes: a few short strings, nothing else.
    chat.inputs = Object.fromEntries(Object.entries(msg.inputs).slice(0, 5)
      .filter(([k, v]) => /^[A-Za-z_][A-Za-z0-9_]{0,30}$/.test(k) && typeof v === "string")
      .map(([k, v]) => [k, v.slice(0, 2000)]));
  }
  if (msg.action === "send") {
    Object.assign(chat, {
      // The panel has no Ask/Do switch: Mia decides. A mode sent explicitly still binds her.
      text: String(msg.text || "").slice(0, 2000), mode: ["ask", "do"].includes(msg.mode) ? msg.mode : "auto",
      run: msg.run === "queue" ? "queue" : "parallel", model: String(msg.model || "").slice(0, 60),
      tab: await chatTab(), language,
    });
  }
  toBridge({ type: "chat", chat });
  return { ok: true };
}

// The toolbar icon opens Mia's side panel, which also holds the settings.
chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true }).catch(() => {});

// Keyboard shortcuts: crop an area, ask about the selected text, or open Mia's chat.
chrome.commands?.onCommand.addListener(async (command, tab) => {
  if (command === "open-chat") {
    const windowId = tab?.windowId ?? (await chrome.windows.getLastFocused()).id;
    chrome.sidePanel.open({ windowId }).catch(() => {});
    return;
  }
  tab = tab || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
  if (!tab || !["crop-ask", "text-ask"].includes(command)) return;
  const shared = isShared(tab.url);
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (mode, ok) => {
        const overlay = globalThis.__ghostOverlay;
        if (!ok) return overlay?.toast?.("Mia can't ask about this page yet. Reload it and try again");
        if (mode === "crop-ask") overlay?.startCrop();
        else overlay?.askSelection();
      },
      args: [command, shared],
    });
  } catch {}
});
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get-status") {
    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
      sendResponse({
        ...getStatus(), reel: reelOn, modes, language, tab_shared: Boolean(tab && isShared(tab.url)),
      });
    });
    return true;
  }
  // A drop-down in the panel listing one column of a spreadsheet: its headers and each column's values.
  if (msg.type === "sheet-columns" && sender.url?.startsWith(chrome.runtime.getURL("sidepanel.html"))) {
    sheetColumns(String(msg.sheet || "")).then(sendResponse, error => sendResponse({ error: String(error?.message || error) }));
    return true;
  }
  // A human clicked Accept or Reject on a suggestion card.
  if (msg.type === "resolve" && sender.tab && typeof msg.id === "string") {
    toBridge({ type: "resolve", id: msg.id, decision: msg.decision });
    return false;
  }
  // A human selected text on a shared page and asked the bots about it.
  if (msg.type === "ask" && sender.tab && isShared(sender.tab.url) && typeof msg.question === "string") {
    const image = typeof msg.image === "string" && msg.image.startsWith("data:image/jpeg;base64,") && msg.image.length <= MAX_CROP_CHARS
      ? msg.image : null;
    toBridge({
      type: "ask", tab_id: sender.tab.id, url: sender.tab.url, question: msg.question.slice(0, 600),
      text: typeof msg.text === "string" ? msg.text.slice(0, 4000) : "", target: msg.target, image,
      thread: typeof msg.thread === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(msg.thread) ? msg.thread : undefined,
      links: Array.isArray(msg.links) ? msg.links.slice(0, 8) : [],
      language,
    });
    return false;
  }
  // The human stopped a question they asked on the page.
  if (msg.type === "ask_cancel" && sender.tab && typeof msg.id === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(msg.id)) {
    toBridge({ type: "ask_cancel", tab_id: sender.tab.id, id: msg.id });
    return false;
  }
  // The human cropped an area of a shared page: take its picture.
  if (msg.type === "capture" && sender.tab && isShared(sender.tab.url) && msg.rect) {
    cropVisible(sender.tab, msg.rect).then(image => sendResponse({ image }), () => sendResponse({ image: null }));
    return true;
  }
  if (msg.type === "conversation" && sender.tab && msg.conversation && typeof msg.conversation.thread === "string") {
    saveConversation(sender.tab, msg.conversation).then(() => sendResponse({ ok: true }), () => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === "modes") {
    modes = { immersive: Boolean(msg.immersive), skip: Boolean(msg.skip) };
    chrome.storage.local.set({ modes }).then(async () => {
      for (const tab of await chrome.tabs.query({})) if (isShared(tab.url)) await applyModes(tab.id);
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.type === "language") {
    language = cleanLanguage(msg.language);
    chrome.storage.local.set({ language }).then(() => sendResponse({ ok: true }));
    return true;
  }
  // Mia's side panel: what the person typed, approvals, stops.
  if (msg.type === "chat" && sender.url?.startsWith(chrome.runtime.getURL("sidepanel.html"))) {
    chatFromPanel(msg).then(sendResponse, err => sendResponse({ ok: false, error: `Mia Browser couldn't send that: ${err?.message || err}` }));
    return true;
  }
  if (msg.type === "chat-last" && sender.url?.startsWith(chrome.runtime.getURL("sidepanel.html"))) {
    sendResponse({ state: chatState, connected: Boolean(connected) });
    return false;
  }
  // The reel page wants the words for its PDF: the bridge asks a model.
  if (msg.type === "reel-story" && sender.url?.startsWith(chrome.runtime.getURL("reel.html"))) {
    if (!(connected && ws && ws.readyState === WebSocket.OPEN)) {
      sendResponse({ ok: false, error: "Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome" });
      return false;
    }
    toBridge({ type: "reel_story", id: String(msg.id || "").slice(0, 32), moments: Array.isArray(msg.moments) ? msg.moments.slice(0, 200) : [], language });
    sendResponse({ ok: true });
    return false;
  }
  if (msg.type === "reel") {
    reelOn = Boolean(msg.on);
    chrome.storage.local.set({ reel: reelOn }).then(() => sendResponse({ ok: true }));
    if (reelOn) chrome.tabs.query({ active: true, lastFocusedWindow: true }).then(([tab]) => tab && isShared(tab.url) && reelPage(tab));
    return true;
  }
  if (msg.type === "open-reel") {
    const url = chrome.runtime.getURL("reel.html");
    chrome.tabs.query({ url }).then(([open]) => (open
      ? chrome.tabs.update(open.id, { active: true }).then(() => chrome.windows.update(open.windowId, { focused: true }))
      : chrome.tabs.create({ url })));
    return false;
  }
  if (msg.type === "connect") {
    port = msg.port || DEFAULT_PORT;
    const typed = (msg.token || "").trim();
    // An empty box means: pair and start the bridge automatically.
    if (!typed) {
      disconnect();
      intentionallyDisconnected = false;
      pairAutomatically().then(ok => {
        if (ok) connect();
        sendResponse(ok ? { ok: true } : { ok: false, error: "Couldn't start Mia Browser on this computer. Run the Mia Browser installer once, then try again." });
      });
      return true;
    }
    token = typed;
    chrome.storage.local.set({ port, token }, () => {
      disconnect();
      intentionallyDisconnected = false;
      connect();
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.type === "disconnect") {
    disconnect({ forgetToken: true });
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

chrome.storage.local.get(["port", "token", "reel", "modes", "language"], (data) => {
  language = cleanLanguage(data.language);
  if (data.modes) modes = { immersive: Boolean(data.modes.immersive), skip: Boolean(data.modes.skip) };
  reelOn = Boolean(data.reel);
  if (data.port) port = data.port;
  if (data.token) token = data.token;
  setBadge(token ? "OFF" : "PAIR", token ? "#ef4444" : "#f59e0b");
  connect();
});

// Keep service worker alive while connected
setInterval(() => {
  if (connected && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "heartbeat" }));
  }
}, 25000);
