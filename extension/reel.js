const reel = document.getElementById("reel");
const count = document.getElementById("count");
let shown = 0;

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

// Markdown from a bot, drawn with text nodes only (never as HTML).
function inline(text) {
  const parts = [];
  for (const [i, chunk] of text.split(/\*\*(.+?)\*\*/g).entries()) {
    if (!chunk) continue;
    parts.push(i % 2 ? el("strong", { textContent: chunk }) : linkify(chunk));
  }
  return parts.flat();
}

function linkify(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s)]+)/g)) {
    out.push(text.slice(last, m.index));
    const href = m[2] || m[3];
    out.push(el("a", { href, target: "_blank", rel: "noopener", textContent: m[1] || href }));
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function markdownNode(markdown) {
  const root = el("div", { className: "md" });
  let list = null;
  let para = [];
  const flush = () => {
    if (para.length) root.append(el("p", {}, ...inline(para.join(" "))));
    para = [];
  };
  for (const raw of markdown.split("\n")) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const item = line.match(/^\s*(?:[-*]|\d+[.)])\s+(.*)$/);
    if (heading || item || !line.trim()) flush();
    if (!item) list = null;
    if (heading) root.append(el(`h${Math.min(heading[1].length + 1, 5)}`, {}, ...inline(heading[2])));
    else if (item) {
      if (!list) root.append(list = el(/^\s*\d/.test(line) ? "ol" : "ul"));
      list.append(el("li", {}, ...inline(item[1])));
    } else if (line.trim()) para.push(line.trim());
  }
  flush();
  return root;
}

function entryNode(e) {
  const when = new Date(e.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const link = el("a", { href: e.url, target: "_blank", rel: "noopener", textContent: e.title || e.url });
  const kinds = { answer: "Answer", page: "Page", conversation: "Conversation", report: "Report" };
  const meta = el("p", { className: "meta" }, el("span", { className: "kind", textContent: kinds[e.kind] || "Moment" }),
    e.url ? link : el("span", { textContent: e.title || "" }), el("span", { textContent: when }));
  const node = el("section", { className: `entry ${e.kind}` }, meta);
  if (e.image) node.append(el("img", { className: "shot", src: e.image, alt: e.title || "" }));
  if (e.kind === "conversation") {
    const box = el("div", { className: "qa" });
    for (const t of e.turns || []) {
      box.append(el("p", { className: "q", textContent: `${t.by ? `${t.by}: ` : ""}“${t.q}”` }));
      if (t.a) box.append(el("p", { className: "t", textContent: `${t.a.by}: ${t.a.title}` }), el("p", { className: "b", textContent: t.a.body }));
    }
    node.append(box);
  }
  if (e.kind === "report") {
    const save = el("button", { textContent: "Download .md" });
    save.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([e.markdown], { type: "text/markdown" }));
      el("a", { href: url, download: `${(e.title || "report").replace(/[^\w -]+/g, "").slice(0, 60) || "report"}.md` }).click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    node.append(el("div", { className: "report" }, save, markdownNode(e.markdown || "")));
  }
  if (e.kind === "answer") {
    node.append(el("div", { className: "qa" },
      el("p", { className: "q", textContent: `“${e.question}”` }),
      el("p", { className: "t", textContent: `${e.by}: ${e.answer?.title || ""}` }),
      el("p", { className: "b", textContent: e.answer?.body || "" })));
  }
  return node;
}

async function render() {
  const entries = await reelAll();
  const nearBottom = innerHeight + scrollY >= document.body.scrollHeight - 200;
  if (entries.length < shown || (shown === 0 && entries.length)) {
    reel.replaceChildren();
    shown = 0;
  }
  const nodes = entries.slice(shown).map(entryNode);
  reel.append(...nodes);
  // Scroll once the screenshots have their size.
  await Promise.all(nodes.map(n => n.querySelector("img")?.decode().catch(() => {})));
  const added = entries.length - shown;
  shown = entries.length;
  count.textContent = `${shown} moment${shown === 1 ? "" : "s"}`;
  if (!shown) reel.replaceChildren(el("p", { className: "empty", textContent: "Nothing yet. Turn on Reel mode in the Ghost menu and browse a shared page." }));
  // New moments load below; follow them when already at the end.
  if (added && (nearBottom || added === shown) && !document.body.classList.contains("booking")) scrollTo({ top: document.body.scrollHeight, behavior: added === shown ? "auto" : "smooth" });
}

document.getElementById("clear").addEventListener("click", async () => {
  if (!confirm("Remove every screenshot from the reel?")) return;
  await reelClear();
  render();
});
chrome.runtime.onMessage.addListener(msg => { if (msg.type === "reel-added") render(); });
render();
