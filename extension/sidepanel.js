/**
 * Mia's chat in Chrome's side panel. It only talks to the background, which
 * talks to the bridge; the bridge runs the conversation and the workers. All
 * text from the bridge (and so from models and pages) is drawn as text nodes.
 */
const $ = id => document.getElementById(id);
const log = $("log"), input = $("input"), send = $("send"), status = $("status");
const STATUS_WORDS = { waiting: "waiting", working: "working…", needs_you: "needs you", done: "done", failed: "failed", stopped: "stopped" };
const prefs = { model: "claude-sonnet-5-5" };
const openAgents = new Set();  // agents whose tasks are shown
const tabTitles = new Map();
let state = null;
let connected = false;
let sending = false;
let connectionEpoch = 0;
let developmentReloadEnabled = false;

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) if (child !== "" && child != null) node.append(child);
  return node;
}

// A bot wears a Mote tinted to its color; Mia herself (no color) is the dot diamond.
function mote(color, extra = "") {
  const face = el("span", { className: `${color ? "mote" : "mote mia-mark"} ${extra}`.trim(), ariaHidden: "true" });
  if (color) face.style.setProperty("--hue", `${moteHue(color)}deg`);
  return face;
}

// The Mote art is pink (hue 337); this turns it to a bot's color.
function moteHue(hex) {
  let h = String(hex).replace("#", "");
  if (!/^[0-9a-fA-F]{3,8}$/.test(h)) return 0;
  if (h.length < 6) h = [...h.slice(0, 3)].map(c => c + c).join("");
  const [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
  if (!d) return 0;
  const hue = max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return Math.round(((hue * 60 - 337 + 540) % 360) - 180);
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function setStatus(text, bad = false) {
  status.textContent = text;
  status.classList.toggle("bad", bad);
}

function isConnectionStatus(text) {
  return text.startsWith("Not connected") || text.startsWith("Mia Browser is not running.");
}

function connectionRecovered() {
  connectionEpoch++;
  if (isConnectionStatus(status.textContent)) setStatus("");
}

// -- talking to the background ----------------------------------------------------

function chat(action, extra = {}) {
  return new Promise(resolve => {
    let settled = false;
    const started = connectionEpoch;
    // Never wait forever: say so when the background doesn't answer.
    const timer = setTimeout(() => {
      settled = true;
      const error = "No acknowledgement yet. Check the task's status before trying again.";
      if (started === connectionEpoch) setStatus(error, true);
      resolve({ ok: false, error });
    }, 15000);
    chrome.runtime.sendMessage({ type: "chat", action, ...extra }, reply => {
      const runtimeError = chrome.runtime.lastError?.message;
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const error = runtimeError || (reply?.ok ? "" : reply?.error || "Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome");
      if (error) {
        if (!isConnectionStatus(error) || started === connectionEpoch) setStatus(error, true);
      } else connectionRecovered();
      resolve(error ? { ok: false, error } : reply);
    });
  });
}

chrome.runtime.onMessage.addListener(msg => {
  if (msg.type === "chat-state" && msg.state && typeof msg.state === "object") {
    connected = true;
    render(msg.state);
  }
});

// -- drawing ------------------------------------------------------------------------

function render(next) {
  state = next;
  renderModels(state.models);
  renderLog(state.messages || []);
  renderTasks(state.tasks || [], state.agents || []);
  renderApprovals((state.tasks || []).filter(t => t.status === "needs_you"));
  renderClaude(state.claude);
  if (!$("history").hidden) renderHistory();
  if (!$("plays").hidden) renderPlays();
  if (connected) connectionRecovered();
}

// Mia runs on the person's Claude account; offer to set it up when it isn't ready.
function renderClaude(claude) {
  const ready = !claude || !("installed" in claude) || (claude.installed && claude.signed_in);
  $("claudeBar").hidden = ready;
  if (ready) return;
  const busy = claude.busy;
  $("claudeText").textContent =
    busy === "installing" ? "Installing Claude for Mia. This takes about a minute…"
    : busy === "signing_in" ? "Sign in to Claude in the browser tab that just opened. Mia is ready right after."
    : "Mia answers with your Claude account. Sign in once and you're set.";
  $("claudeBtn").hidden = Boolean(busy);
  $("claudeBtn").textContent = claude.installed ? "Sign in to Claude" : "Set up Claude";
}

function renderModels(models) {
  const select = $("model");
  if (!Array.isArray(models) || select.options.length === models.length) return;
  select.replaceChildren(...models.map(m => el("option", { value: m.id, textContent: m.name })));
  select.value = models.some(m => m.id === prefs.model) ? prefs.model : models[0]?.id;
}

// Links in a message become clickable. Text stays text nodes; only http(s) addresses become links,
// and a link always shows its own address, so a page can't disguise where it goes.
const URL_RE = /\b(?:https?:\/\/|www\.)[^\s<>"']+/gi;

function linked(text) {
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(URL_RE)) {
    let url = match[0].replace(/[.,;:!?)\]}'"»”’]+$/, "");
    // Keep a closing parenthesis that belongs to the address, as in Wikipedia links.
    if (match[0].charAt(url.length) === ")" && url.includes("(")) url += ")";
    const href = /^www\./i.test(url) ? `https://${url}` : url;
    let ok = false;
    try { ok = ["http:", "https:"].includes(new URL(href).protocol); } catch {}
    if (!ok) continue;
    parts.push(text.slice(last, match.index), el("a", { href, textContent: url, target: "_blank", rel: "noopener noreferrer" }));
    last = match.index + url.length;
  }
  parts.push(text.slice(last));
  return parts;
}

function renderLog(messages) {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const nodes = messages.map(m => m.who === "you"
    ? el("div", { className: "you" }, ...linked(m.text))
    : el("div", { className: "mia" }, mote(m.color, "sm"), el("p", {}, ...linked(m.text))));
  // What each bot is doing shows in the Agents list below, not here.
  if (state.planning) nodes.push(el("div", { className: "mia thinking" }, mote("", "sm working"), el("p", { textContent: "Thinking…" })));
  $("hello").hidden = nodes.length > 0;
  log.replaceChildren($("hello"), ...nodes);
  if (atBottom || sending) log.scrollTop = log.scrollHeight;
}

const LIVE = ["waiting", "working", "needs_you"];

// One row per agent (one per tab): its face, the tab's name and what it's doing. Open it for its tasks.
function buildProgressText(t) {
  const p = t.build_progress;
  if (!p) return "";
  const parts = [];
  if (p.uncertain_action) parts.push(p.destination_observed
    ? "Destination observed · resume needs verification"
    : "Write outcome unknown · check destination before retrying");
  else if (p.failed_step) parts.push(`Repair step ${p.failed_step}`);
  else if (p.pending_steps?.length) parts.push(`Step ${p.pending_steps[0]} needs a live test`);
  parts.push(`${p.verified} step${p.verified === 1 ? "" : "s"} verified`);
  if (p.planned_steps) parts.push(`${p.planned_steps} planned`);
  if (p.reviews_passed?.includes("adversarial")) parts.push("Reuse review passed");
  if (p.reviews_passed?.includes("human")) parts.push("Usability review passed");
  return parts.join(" · ");
}

function renderTasks(tasks, agents) {
  $("tasks").hidden = !tasks.length;
  if (!tasks.length) return;
  const byId = new Map((agents || []).map(a => [a.id, a]));
  const groups = new Map();
  for (const t of tasks) {
    const key = t.agent || t.id;
    if (!groups.has(key)) groups.set(key, { key, agent: byId.get(key) || { name: t.label, color: t.color, tab_id: t.tab_id, host: "" }, tasks: [] });
    groups.get(key).tasks.push(t);
  }
  const finished = tasks.filter(t => !LIVE.includes(t.status)).length;
  const builds = tasks.filter(t => t.build_id).length;
  $("tasksLabel").textContent = builds === tasks.length ? "Mia's builds" : builds ? "Tasks" : "Bots";
  $("taskSum").textContent = builds ? `${finished} of ${tasks.length} finished`
    : `${groups.size} bot${groups.size === 1 ? "" : "s"} · ${finished} of ${tasks.length} finished`;
  $("stopAll").hidden = finished === tasks.length;
  const rows = [];
  for (const { key, agent, tasks: list } of groups.values()) {
    const now = list.find(t => t.status === "needs_you") || list.find(t => t.status === "working")
      || list.find(t => t.status === "waiting") || list[list.length - 1];
    const expanded = openAgents.has(key);
    const head = el("button", { className: `agent${expanded ? " open" : ""}`, ariaExpanded: String(expanded),
                               title: now.build_id ? buildProgressText(now) : "" },
      mote(agent.color, `sm${now.status === "working" ? " working" : ""}`),
      el("span", { className: "what" }, el("b", { textContent: now.build_id ? `Mia · ${now.title}` : tabName(agent) }),
        now.build_id ? el("small", { textContent: buildProgressText(now) }) : null),
      el("span", { className: `pill ${now.status}`, textContent: STATUS_WORDS[now.status] || now.status }),
      el("span", { className: "chev", textContent: "›", ariaHidden: "true" }));
    head.addEventListener("click", () => { expanded ? openAgents.delete(key) : openAgents.add(key); renderTasks(tasks, agents); });
    const live = list.some(t => LIVE.includes(t.status));
    const close = el("button", { className: "close", textContent: "×", title: live ? "Stop and close" : "Close",
                                 ariaLabel: `Close ${tabName(agent)}` });
    close.addEventListener("click", () => { openAgents.delete(key); chat("close", { agent: key }); });
    rows.push(el("div", { className: "agent-row" }, head, close));
    if (!expanded) continue;
    for (const t of list) {
      const isLive = LIVE.includes(t.status);
      const row = el("button", { className: `task sub ${t.status}`, title: t.tab_id ? "Show this tab" : "" },
        el("span", { className: "what" }, el("b", { textContent: t.title }),
          t.build_id ? el("small", { textContent: buildProgressText(t) }) : null,
          el("small", { textContent: (isLive ? t.note : t.result) || "" })),
        el("span", { className: `pill ${t.status}`, textContent: STATUS_WORDS[t.status] || t.status }));
      row.addEventListener("click", () => showTab(t.tab_id));
      if (isLive) {
        const stop = el("span", { className: "icon", role: "button", title: "Stop", ariaLabel: `Stop ${t.title}`, textContent: "×" });
        stop.style.marginLeft = "0";
        stop.addEventListener("click", event => { event.stopPropagation(); chat("stop", { task: t.id }); });
        row.append(stop);
      }
      rows.push(row);
    }
  }
  $("taskRows").replaceChildren(...rows);
}

// The tab's own title, looked up once; the site or the agent's name until then.
function tabName(agent) {
  const id = agent.tab_id;
  if (!Number.isInteger(id)) return agent.name;
  if (!tabTitles.has(id)) {
    tabTitles.set(id, "");
    chrome.tabs.get(id).then(tab => {
      tabTitles.set(id, tab.title || "");
      if (tab.title && state) renderTasks(state.tasks || [], state.agents || []);
    }).catch(() => {});
  }
  return tabTitles.get(id) || agent.host || agent.name;
}
chrome.tabs.onUpdated.addListener((id, info) => {
  if (info.title && tabTitles.has(id)) { tabTitles.set(id, info.title); if (state) renderTasks(state.tasks || [], state.agents || []); }
});

function renderApprovals(tasks) {
  $("approvals").replaceChildren(...tasks.map(t => {
    const yes = el("button", { className: "yes", textContent: "Approve" });
    const no = el("button", { textContent: "Reject" });
    const show = el("button", { className: "show", textContent: "Show me" });
    yes.addEventListener("click", () => { yes.disabled = no.disabled = true; chat("approve", { task: t.id }); });
    no.addEventListener("click", () => { yes.disabled = no.disabled = true; chat("reject", { task: t.id }); });
    show.addEventListener("click", () => showTab(t.tab_id));
    const card = el("div", { className: "approval" },
      el("div", { className: "head" }, mote(t.color, "sm"), el("span", { textContent: `${t.label} needs you` })),
      el("p", { textContent: t.question }), el("div", { className: "row" }, yes, no, show));
    card.style.setProperty("--c", t.color);
    return card;
  }));
}

async function showTab(tabId) {
  if (!Number.isInteger(tabId)) return;
  const tab = await chrome.tabs.update(tabId, { active: true }).catch(() => null);
  if (tab) chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
}

// -- the context chips: what Mia can see ----------------------------------------------

async function renderChips() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const chips = $("chips");
  chips.replaceChildren();
  if (!tab) return;
  const web = /^https?:/.test(tab.url || "");
  chips.append(el("span", { className: "chip", textContent: web ? `This tab · ${hostOf(tab.url)}` : "This tab can't be read",
    title: tab.title || "" }));
  if (!web) return;
  try {
    const [run] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => String(getSelection() || "").trim().slice(0, 80) });
    if (run?.result) chips.append(el("span", { className: "chip sel", textContent: `Selection · “${run.result}”` }));
  } catch {}
}

chrome.tabs.onActivated.addListener(renderChips);
chrome.tabs.onUpdated.addListener((id, info, tab) => { if (tab.active && (info.url || info.status === "complete")) renderChips(); });
window.addEventListener("focus", renderChips);

// -- the composer ------------------------------------------------------------------------

$("model").addEventListener("change", event => { prefs.model = event.target.value; chrome.storage.local.set({ chatPrefs: prefs }); });

async function submit() {
  const text = input.value.trim();
  if (!text || sending) return;
  sending = true;
  send.disabled = true;
  const reply = await chat("send", { text, model: prefs.model });
  if (reply.ok) {
    input.value = "";
  }
  sending = false;
  send.disabled = false;
  input.focus();
}

send.addEventListener("click", submit);
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  }
});
$("stopAll").addEventListener("click", () => chat("stop_all"));
// The bots list starts collapsed: just the count until you open it.
$("tasksToggle").addEventListener("click", () => {
  const open = $("taskRows").hidden;
  $("taskRows").hidden = !open;
  $("tasksToggle").setAttribute("aria-expanded", String(open));
});
$("newChat").addEventListener("click", () => { toggleHistory(false); chat("new"); });

// -- past chats: saved on this computer by the bridge, so closing Chrome loses nothing ---------

function when(ts) {
  const d = new Date(ts), today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderHistory() {
  const chats = state?.chats || [];
  if (!chats.length) {
    $("chatList").replaceChildren(el("p", { className: "empty", textContent: connected ? "No past chats yet." : "Connect to Mia Browser to see past chats." }));
    return;
  }
  $("chatList").replaceChildren(...chats.map(c => {
    const open = el("button", { className: "chat-row" + (c.id === state.chat ? " current" : "") },
      el("span", { className: "chat-title", textContent: c.title || "Chat" }),
      el("small", { textContent: c.id === state.chat ? "Open now" : when(c.ts) }));
    open.addEventListener("click", () => { toggleHistory(false); if (c.id !== state.chat) chat("open_chat", { chat: c.id }); });
    const del = el("button", { className: "icon", title: "Delete", ariaLabel: `Delete ${c.title || "chat"}` });
    del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    del.addEventListener("click", () => chat("delete_chat", { chat: c.id }));
    return el("div", { className: "chat-item" }, open, del);
  }));
}

function toggleHistory(open) {
  if (open) { toggleSettings(false); togglePlays(false); }
  document.body.classList.toggle("in-history", open);
  $("history").hidden = !open;
  $("openHistory").setAttribute("aria-expanded", String(open));
  if (open) { renderHistory(); chat("sync"); }
}
$("openHistory").addEventListener("click", () => toggleHistory($("history").hidden));
$("closeHistory").addEventListener("click", () => toggleHistory(false));
$("claudeBtn").addEventListener("click", () => { $("claudeBtn").disabled = true; chat("claude_setup").finally(() => { $("claudeBtn").disabled = false; }); });

// -- Play Automations: scripts Mia Browser replays click by click, no AI ----------------------

const openPlays = new Set();  // automations being edited explicitly with the pencil
const runPlays = new Set();  // automations whose run inputs are shown
const playInputs = new Map();  // what the person typed in an automation's boxes, kept across redraws and reloads
const playProblem = new Map();  // why Play didn't start, shown under the automation's button
const sheetCache = new Map();  // spreadsheet link -> its columns, read once per panel (↻ reads it again)

function sheetData(link) {
  if (!sheetCache.has(link)) {
    sheetCache.set(link, { loading: true });
    chrome.runtime.sendMessage({ type: "sheet-columns", sheet: link }, reply => {
      const error = chrome.runtime.lastError?.message || (reply?.headers ? "" : reply?.error || "no answer");
      sheetCache.set(link, error ? { error } : { headers: reply.headers, values: reply.values });
      renderPlays();
    });
  }
  return sheetCache.get(link);
}

function saveInputs() {
  chrome.storage.local.set({ playInputs: Object.fromEntries(playInputs) }).catch(() => {});
}

function lastRun(last) {
  if (!last?.at) return "";
  const word = { done: "Last run", failed: "Last run failed", stopped: "Last run stopped" }[last.status] || "Last run";
  return `${word} ${when(last.at)}${last.status === "failed" && last.note ? `: ${last.note}` : ""}`;
}

function renderPlays() {
  // Don't redraw under the person's cursor while they type in a box.
  if (document.activeElement?.closest?.("#playList textarea, #playList input")) return;
  const plays = state?.automations || [];
  if (!plays.length) {
    $("playList").replaceChildren(el("p", { className: "empty", textContent: connected
      ? "No Play Automations yet. Press + and tell Mia what to do, step by step. She does it once, then saves it."
      : "Connect to Mia Browser to see your Play Automations." }));
    return;
  }
  $("playList").replaceChildren(...plays.map(a => {
    const open = openPlays.has(a.id);
    const runOpen = runPlays.has(a.id);
    // Only a schedule or a pause is worth a second line; "runs when you press Play" is what ▶ says.
    const line = a.running ? "Running…" : a.paused ? `Paused · ${a.schedule}` : a.scheduled ? a.schedule : "";
    const run = el("button", { className: "icon play-run" + (a.running ? " stop" : ""), textContent: a.running ? "■" : "▶",
      title: a.running ? "Stop" : "Play", ariaLabel: `${a.running ? "Stop" : "Play"} ${a.name}` });
    const startPlay = () => {
      if (a.running) { chat("stop", { task: a.task }); return; }
      const values = playInputs.get(a.id) || {};
      const empty = (a.inputs || []).find(input => !String(values[input.name] || "").trim());
      if (empty) {
        // Keep the run form visible so the missing value can be supplied immediately.
        playProblem.set(a.id, `Enter ${empty.label.toLowerCase()} to run.`);
        openPlays.delete(a.id);
        runPlays.add(a.id);
        renderPlays();
        return;
      }
      if (playProblem.delete(a.id)) renderPlays();
      chat("play", { automation: a.id, inputs: values }).then(reply => {
        if (!reply.ok) { playProblem.set(a.id, reply.error || "Mia Browser didn't answer. Try again."); openPlays.delete(a.id); runPlays.add(a.id); renderPlays(); }
      });
    };
    run.addEventListener("click", () => {
      if (a.running || !(a.inputs || []).length) { startPlay(); return; }
      openPlays.delete(a.id);
      runPlays.add(a.id);
      playProblem.delete(a.id);
      renderPlays();
    });
    const edit = el("button", { className: "icon play-edit" + (open ? " on" : ""), textContent: "✎",
      title: open ? "Close" : "Edit", ariaLabel: `${open ? "Close" : "Edit"} ${a.name}`, ariaExpanded: String(open) });
    edit.addEventListener("click", () => { runPlays.delete(a.id); open ? openPlays.delete(a.id) : openPlays.add(a.id); renderPlays(); });
    const row = el("div", { className: "play-row" },
      el("span", { className: "state" + (a.running ? " running" : a.paused ? " paused" : "") }),
      el("span", { className: "what" }, el("b", { textContent: a.name }), line ? el("small", { textContent: line }) : ""),
      run, edit);
    const item = el("div", { className: "play" + (open || runOpen ? " open" : "") }, row);
    if (!open && !runOpen) return item;
    const name = el("input", { type: "text", value: a.name, maxLength: 60, ariaLabel: "Name" });
    const rename = () => {
      const value = name.value.trim();
      if (!value) { name.value = a.name; return; }
      if (value !== a.name) chat("automation_rename", { automation: a.id, name: value });
    };
    name.addEventListener("change", rename);
    name.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); name.blur(); }
      if (event.key === "Escape") { name.value = a.name; name.blur(); }
    });
    const typed = playInputs.get(a.id) || {};
    const boxFor = {};
    const remember = (name, value) => {
      playInputs.set(a.id, { ...(playInputs.get(a.id) || {}), [name]: value });
      saveInputs();
    };
    const boxes = (a.inputs || []).map(input => {
      if (Array.isArray(input.choices)) {
        // A drop-down: the different values of one column of the automation's spreadsheet (most used first),
        // plus the ones added here that aren't in the sheet yet. The last one picked stays picked.
        const link = String(typed[a.sheet_input] || "").trim();
        const sheet = link ? sheetData(link) : null;
        const ready = sheet && !sheet.loading && !sheet.error;
        let column = input.column || "";
        if (ready && !column) {
          // First time: the column whose header shares a word with the box's label ("Source / Campaign").
          const words = input.label.toLowerCase().match(/[a-z]{4,}/g) || [];
          column = sheet.headers.find(h => words.some(w => h.toLowerCase().includes(w))) || "";
          if (column) chat("automation_column", { automation: a.id, input: input.name, column });
        }
        const at = ready ? sheet.headers.indexOf(column) : -1;
        const fromSheet = at >= 0 ? sheet.values[at] : [];
        const extra = input.choices.filter(c => !fromSheet.includes(c));
        const choices = [...fromSheet, ...extra];
        // A sheet-backed choice list is temporarily empty while its CSV loads.
        // Keep the last selection until the actual column values are available.
        const picked = choices.includes(typed[input.name]) ? typed[input.name]
          : column && !ready ? (typed[input.name] || choices[0] || "") : (choices[0] || "");
        if (picked !== (typed[input.name] ?? "")) remember(input.name, picked);
        const columnBox = el("select", { ariaLabel: `Column for ${input.label}` },
          el("option", { value: "", textContent: !link ? "Put the spreadsheet link first" : sheet.loading ? "Reading the sheet…"
            : sheet.error ? "Couldn't read the sheet" : "Pick a column" }),
          ...(ready ? sheet.headers.map((h, i) => h && el("option", { value: h, textContent: `${String.fromCharCode(65 + i)} · ${h}`,
            selected: h === column })).filter(Boolean) : []));
        columnBox.disabled = !ready;
        columnBox.addEventListener("change", () => chat("automation_column", { automation: a.id, input: input.name, column: columnBox.value }));
        const reread = el("button", { className: "icon", textContent: "↻", title: "Read the sheet again", ariaLabel: "Read the sheet again" });
        reread.disabled = !link;
        reread.addEventListener("click", event => { event.preventDefault(); sheetCache.delete(link); renderPlays(); });
        const select = el("select", { ariaLabel: input.label },
          ...(choices.length ? choices.map(c => el("option", { value: c, textContent: c, selected: c === picked }))
                             : [el("option", { value: "", textContent: "Add one with ＋" })]));
        select.disabled = !choices.length;
        select.addEventListener("change", () => remember(input.name, select.value));
        const add = el("button", { className: "icon", textContent: "＋", title: `Add to ${input.label}`, ariaLabel: `Add to ${input.label}` });
        add.addEventListener("click", event => {
          event.preventDefault();
          const value = (prompt(`New value for “${input.label}”:`) || "").trim().slice(0, 100);
          if (!value) return;
          remember(input.name, value);
          if (!choices.includes(value)) chat("automation_choices", { automation: a.id, input: input.name, choices: [...input.choices, value] });
          else renderPlays();
        });
        // Only the ones added here can be deleted: the sheet's come back while they're in the column.
        const mine = input.choices.includes(picked) && !fromSheet.includes(picked);
        const del = el("button", { className: "icon", textContent: "×", ariaLabel: `Delete ${picked}`,
          title: mine ? `Delete “${picked}”` : picked ? "This one comes from the sheet" : "" });
        del.disabled = !mine;
        del.addEventListener("click", event => {
          event.preventDefault();
          if (!mine || !confirm(`Delete “${picked}” from ${input.label}?`)) return;
          remember(input.name, choices.find(c => c !== picked) || "");
          chat("automation_choices", { automation: a.id, input: input.name, choices: input.choices.filter(c => c !== picked) });
        });
        return boxFor[input.name] = el("div", { className: "play-input" }, el("span", { textContent: input.label }),
          ...(open ? [el("div", { className: "play-choice" }, columnBox, reread)] : []),
          el("div", { className: "play-choice" }, select, ...(open ? [add, del] : [])));
      }
      const urlInput = /(?:^|[^a-z])(?:url|link)(?:$|[^a-z])/i.test(`${input.name} ${input.label}`);
      const box = el(urlInput ? "input" : "textarea", { ...(urlInput ? { type: "url" } : { rows: 3 }), value: typed[input.name] ?? "",
        placeholder: urlInput ? "https://…" : `Enter ${input.label.toLowerCase()}` });
      box.addEventListener("input", () => {
        playInputs.set(a.id, { ...(playInputs.get(a.id) || {}), [input.name]: box.value });
        saveInputs();
      });
      // A new spreadsheet link: the drop-downs read its columns.
      if (input.name === a.sheet_input) box.addEventListener("change", () => renderPlays());
      return boxFor[input.name] = el("label", { className: "play-input" }, el("span", { textContent: input.label }), box);
    });
    if (!open) {
      const submit = el("button", { type: "submit", className: "run", textContent: a.running ? "Running…" : "Run" });
      submit.disabled = !!a.running;
      const cancel = el("button", { type: "button", textContent: "Cancel" });
      cancel.addEventListener("click", () => { runPlays.delete(a.id); playProblem.delete(a.id); renderPlays(); });
      const form = el("form", { className: "play-detail play-run-form" }, ...boxes,
        playProblem.has(a.id) ? el("p", { className: "last failed", role: "alert", textContent: playProblem.get(a.id) }) : "",
        el("div", { className: "play-actions" }, submit, cancel));
      form.addEventListener("submit", event => {
        event.preventDefault();
        document.activeElement?.blur?.();
        startPlay();
      });
      item.append(form);
      return item;
    }
    const shown = new Set();
    const steps = a.steps.map((step, i) => {
      const remove = el("button", { className: "icon step-del", textContent: "×", title: "Delete this step", ariaLabel: `Delete step ${i + 1}` });
      remove.addEventListener("click", () => {
        if (confirm(`Delete step ${i + 1}, “${step}”?`)) chat("automation_step_delete", { automation: a.id, step: i });
      });
      // The box this step types goes right under it.
      const own = (a.inputs || []).map(input => input.name)
        .filter(name => (a.uses?.[i] || []).includes(name) && boxFor[name] && !shown.has(name));
      own.forEach(name => shown.add(name));
      return el("li", {}, el("div", { className: "step" }, el("span", { textContent: step }), remove),
        ...own.map(name => boxFor[name]));
    });
    const rest = (a.inputs || []).filter(input => !shown.has(input.name)).map(input => boxFor[input.name]);
    const full = el("button", { className: "full" + (a.full_access ? " on" : ""), textContent: a.full_access ? "Full access ✓" : "Full access",
      ariaPressed: String(!!a.full_access),
      title: a.full_access ? "Send, Post and the like run without asking when you press Play. Click to ask again."
                           : "When you press Play, run Send, Post and the like without asking you first" });
    full.addEventListener("click", () => {
      if (!a.full_access && !confirm(`Let “${a.name}” click Send, Post and the like without asking, when you press Play?`)) return;
      chat("automation_full_access", { automation: a.id, on: !a.full_access });
    });
    const actions = el("div", { className: "play-actions" }, full);
    if (a.scheduled) {
      const pause = el("button", { textContent: a.paused ? "Resume" : "Pause" });
      pause.addEventListener("click", () => chat(a.paused ? "automation_resume" : "automation_pause", { automation: a.id }));
      actions.append(pause);
    }
    if (a.each && a.done) {
      const reset = el("button", { textContent: "Start over", title: `Forget the ${a.done} links already done` });
      reset.addEventListener("click", () => chat("automation_reset", { automation: a.id }));
      actions.append(reset);
    }
    const del = el("button", { className: "del", textContent: "Delete" });
    del.addEventListener("click", () => {
      if (confirm(`Delete the Play Automation “${a.name}”?`)) { openPlays.delete(a.id); runPlays.delete(a.id); chat("automation_delete", { automation: a.id }); }
    });
    actions.append(del);
    const last = lastRun(a.last_run);
    item.append(el("div", { className: "play-detail" },
      el("label", { className: "play-input play-name" }, el("span", { textContent: "Name" }), name),
      a.about ? el("p", { textContent: a.about }) : "",
      el("ol", {}, ...steps),
      a.each ? el("p", { textContent: `${a.each}, until the list is used up or you press Stop.`
        + (a.done ? ` ${a.done} done so far; the next run carries on.` : "") }) : "",
      ...rest,
      last ? el("p", { className: "last " + (a.last_run?.status || ""), textContent: last }) : "",
      playProblem.has(a.id) ? el("p", { className: "last failed", role: "alert", textContent: playProblem.get(a.id) }) : "",
      actions));
    return item;
  }));
}

function togglePlays(open) {
  if (open) { toggleSettings(false); toggleHistory(false); }
  document.body.classList.toggle("in-plays", open);
  $("plays").hidden = !open;
  $("openPlays").setAttribute("aria-expanded", String(open));
  if (open) { renderPlays(); chat("sync"); }
}
$("openPlays").addEventListener("click", () => togglePlays($("plays").hidden));
$("closePlays").addEventListener("click", () => togglePlays(false));
$("newPlay").addEventListener("click", () => {
  togglePlays(false);
  input.value = "Make a Play Automation that ";
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
});

// -- settings: what the toolbar popup used to hold --------------------------------------

function toggleSettings(open) {
  if (open) { toggleHistory(false); togglePlays(false); }
  document.body.classList.toggle("in-settings", open);
  $("settings").hidden = !open;
  $("openSettings").setAttribute("aria-expanded", String(open));
  if (open) refreshSettings();
  else input.focus();
}

function updateSettings(info) {
  const on = Boolean(info.connected);
  developmentReloadEnabled = info.developmentReload?.enabled === true;
  $("developmentReloadBox").hidden = info.developmentReload?.available !== true;
  $("developmentReload").checked = info.developmentReload?.enabled === true;
  connected = on;
  $("openPlays").classList.toggle("off", !on);
  $("openPlays").title = on ? "Play Automations" : "Play Automations · Mia Browser is not running";
  $("dot").classList.toggle("on", on);
  $("statusLabel").textContent = on ? "Connected" : "Disconnected";
  $("statusDetail").textContent = on ? `Bridge on port ${info.port}`
    : info.paired ? "Starting Mia on this computer…" : setupDetail(info.helper);
  renderSetup(info);
  if (document.activeElement !== $("port")) $("port").value = info.port;
  $("version").textContent = `Mia v${info.version}`;
  // Pairing is automatic; the manual fields only matter when it hasn't worked.
  $("setup").hidden = on;
  $("disconnectBtn").hidden = !on;
  $("modesBox").hidden = !on;
  $("reelMode").checked = Boolean(info.reel);
  $("immersive").checked = Boolean(info.modes?.immersive);
  $("skipPrompt").checked = Boolean(info.modes?.skip);
  if (document.activeElement !== $("language")) $("language").value = info.language || "English";
  if (!on) setStatus(info.paired || info.helper === "ok" ? "Mia is not running. Close and reopen Chrome, or reload the extension on chrome://extensions"
    : "Mia isn't set up on this Mac yet. One download finishes it (see above).", true);
  else if ($("status").textContent.startsWith("Not connected") || $("status").textContent.startsWith("Mia isn't set up")) setStatus("");
}

function setupDetail(helper) {
  if (helper === "outdated") return "An older Mia helper is installed; the current installer replaces it";
  if (helper === "failed") return "The Mia helper didn't answer. Running the installer again fixes it";
  return "Not set up on this Mac yet";
}

// The card for the one step the extension can't do itself: the installer.
const SETUP_STEPS = {
  mac: ["Open the download (bottom of Chrome, or your Downloads folder)", "Click Install, then enter your Mac password when asked",
        "Come back here. This panel turns green on its own"],
  win: ["Open the download (bottom of Chrome, or your Downloads folder)", "Click Install. No administrator password is needed",
        "Come back here. This panel turns green on its own"],
  linux: ["Open the download (bottom of Chrome, or your Downloads folder)", "Your software installer opens; click Install and enter your password",
          "Come back here. This panel turns green on its own"],
};
const SETUP_WHAT = {
  mac: "The Mia helper (its own copy of Python and Mia's programs, in /Library/Application Support/Ghost), and Claude Code from Anthropic if you don't have it. Mia answers with your own Claude account. Nothing runs until Chrome asks for it, and everything is removed by the uninstaller in that folder.",
  win: "The Mia helper (its own copy of Python and Mia's programs, in your user folder under AppData\\Local\\Mia), and Claude Code from Anthropic if you don't have it. Mia answers with your own Claude account. Nothing runs until Chrome asks for it, and Mia appears in Windows' Installed apps to uninstall.",
  linux: "The Mia helper (its own copy of Python and Mia's programs, in /opt/mia-browser-use), and Claude Code from Anthropic if you don't have it. Mia answers with your own Claude account. Nothing runs until Chrome asks for it; remove it with your package manager (mia-browser-use).",
};

function renderSetup(info) {
  const paired = Boolean(info.paired) || info.helper === "ok";
  $("setupCard").hidden = paired;
  $("downloadBtn").href = info.installer_url || "#";
  $("downloadBtn").hidden = !info.installer_url;
  $("setupUnsupported").hidden = Boolean(info.installer_url);
  const os = SETUP_STEPS[info.os] ? info.os : "mac";
  $("setupSteps").replaceChildren(...SETUP_STEPS[os].map(text => { const li = document.createElement("li"); li.textContent = text; return li; }));
  $("setupWhat").textContent = SETUP_WHAT[os];
  if (info.helper === "outdated") {
    $("setupTitle").textContent = "Update the Mia helper on this Mac";
    $("setupText").textContent = "This version of Mia needs a newer helper than the one installed. Download the current installer and run it; your chats and settings stay.";
  } else if (info.helper === "failed") {
    $("setupTitle").textContent = "The Mia helper isn't answering";
    $("setupText").textContent = "Running the installer again puts a fresh copy in place. If it still fails, close and reopen Chrome.";
  } else {
    $("setupTitle").textContent = "One more step on this Mac";
    $("setupText").textContent = "Mia runs on your computer, not in the cloud. Chrome extensions can't install programs, so there is one download: the Mia installer. Open it, click Install, and this panel connects by itself.";
  }
}

function refreshSettings() {
  chrome.runtime.sendMessage({ type: "get-status" }, info => {
    if (!chrome.runtime.lastError && info) updateSettings(info);
  });
}

function setting(message, delay = 300) {
  $("error").textContent = "";
  chrome.runtime.sendMessage(message, () => setTimeout(refreshSettings, delay));
}

$("openSettings").addEventListener("click", () => toggleSettings($("settings").hidden));
$("closeSettings").addEventListener("click", () => toggleSettings(false));
$("reelMode").addEventListener("change", () => setting({ type: "reel", on: $("reelMode").checked }));
for (const id of ["immersive", "skipPrompt"]) {
  $(id).addEventListener("change", () => setting({ type: "modes", immersive: $("immersive").checked, skip: $("skipPrompt").checked }));
}
$("language").addEventListener("change", () => setting({ type: "language", language: $("language").value }));
$("openReel").addEventListener("click", () => chrome.runtime.sendMessage({ type: "open-reel" }));
$("disconnectBtn").addEventListener("click", () => setting({ type: "disconnect" }, 200));
$("connectBtn").addEventListener("click", () => {
  const port = parseInt($("port").value, 10) || 9377;
  const token = $("token").value.trim();
  $("error").textContent = "";
  chrome.runtime.sendMessage({ type: "connect", port, token }, result => {
    if (!result?.ok) $("error").textContent = result?.error || "Connection failed";
    $("token").value = "";
    setTimeout(() => { refreshSettings(); if (connected) chat("sync"); }, 500);
  });
});
// The connection dot follows the bridge.
setInterval(refreshSettings, 5000);

// -- start --------------------------------------------------------------------------------

(async () => {
  const stored = await chrome.storage.local.get(["chatPrefs", "playInputs"]);
  for (const [id, values] of Object.entries(stored.playInputs || {})) {
    if (values && typeof values === "object") playInputs.set(id, values);
  }
  const saved = stored.chatPrefs || {};
  if (saved.model) prefs.model = String(saved.model);
  renderChips();
  chrome.runtime.sendMessage({ type: "chat-last" }, reply => {
    if (chrome.runtime.lastError || !reply) return;
    connected = Boolean(reply.connected);
    if (reply.state) render(reply.state);
    if (connected) chat("sync");
    else toggleSettings(true);
  });
  refreshSettings();
  input.focus();
})();

// A live, empty panel is required: closed/stale panels and drafts fail closed.
const developmentIdlePort = chrome.runtime.connect({ name: "mia-development-idle" });
function developmentPanelClean() {
  return !sending && !renaming && !input.value.trim()
    && !(document.activeElement !== input && document.activeElement?.matches("input, textarea, select, [contenteditable=true]"))
    && $("settings").hidden && $("plays").hidden;
}
const developmentFreeze = new MiaDevelopmentReload.PanelFreeze({
  document, clean: developmentPanelClean,
  ack: message => developmentIdlePort.postMessage(message),
});
developmentIdlePort.onMessage.addListener(message => developmentFreeze.receive(message));
developmentIdlePort.onDisconnect.addListener(() => developmentFreeze.release());
function reportDevelopmentIdle() {
  try { developmentIdlePort.postMessage({ clean: developmentPanelClean() }); } catch {}
}
document.addEventListener("input", reportDevelopmentIdle, true);
document.addEventListener("click", () => queueMicrotask(reportDevelopmentIdle), true);
setInterval(() => {
  reportDevelopmentIdle();
  if (developmentReloadEnabled && connected && !sending) chat("sync");
}, 1000);
$("developmentReload").addEventListener("change", event =>
  setting({ type: "development-reload", enabled: event.target.checked }));
