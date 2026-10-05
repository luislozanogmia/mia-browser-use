/**
 * The reel as a printed booklet. A model (through the bridge) writes the
 * words: title, summary, takeaways, chapters, captions. This page lays them
 * out next to the screenshots, with text nodes only, and the browser's print
 * dialog saves it as a PDF. Without the bridge or the model, the booklet is
 * still made, with plain chapters by site.
 */
const book = document.getElementById("book");
const makePdf = document.getElementById("makePdf");
const STORY_WAIT_MS = 200000;

function siteOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function whenOf(ts, withDay = true) {
  return new Date(ts).toLocaleString([], withDay
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" });
}

// What the model reads: words only, never the screenshots.
function momentsOf(entries) {
  return entries.map(e => ({
    kind: e.kind || "moment",
    when: whenOf(e.ts),
    title: (e.title || "").slice(0, 200),
    url: (e.url || "").slice(0, 300),
    ...(e.kind === "answer" ? {
      question: (e.question || "").slice(0, 600),
      answer: `${e.by || "bot"}: ${e.answer?.title || ""}. ${e.answer?.body || ""}`.slice(0, 1200),
    } : {}),
    ...(e.kind === "conversation" ? {
      turns: (e.turns || []).slice(0, 12).map(t => ({
        q: String(t.q || "").slice(0, 400),
        a: t.a ? `${t.a.by}: ${t.a.title}. ${t.a.body || ""}`.slice(0, 800) : "",
      })),
    } : {}),
    ...(e.kind === "report" ? { report: (e.markdown || "").slice(0, 3000) } : {}),
  }));
}

// Without a model: one chapter per site, in the order they were visited.
function plainStory(entries) {
  const chapters = [];
  entries.forEach((e, i) => {
    const site = siteOf(e.url) || (e.kind === "report" ? "Reports" : "Moments");
    const last = chapters[chapters.length - 1];
    if (last && last.heading === site) last.moments.push(i + 1);
    else chapters.push({ heading: site, intro: "", moments: [i + 1] });
  });
  return { title: "Browsing reel", subtitle: "", summary: "", takeaways: [], chapters, captions: {}, next_steps: [] };
}

function askStory(entries) {
  return new Promise(resolve => {
    const id = Math.random().toString(36).slice(2, 14);
    let timer = 0;
    const done = value => {
      clearTimeout(timer);
      chrome.runtime.onMessage.removeListener(listen);
      resolve(value);
    };
    const listen = msg => {
      if (msg.type !== "reel-story-done" || msg.id !== id) return;
      done(msg.story && typeof msg.story === "object" ? { story: msg.story } : { error: msg.error || "No answer" });
    };
    chrome.runtime.onMessage.addListener(listen);
    timer = setTimeout(() => done({ error: "The model took too long" }), STORY_WAIT_MS);
    chrome.runtime.sendMessage({ type: "reel-story", id, moments: momentsOf(entries) }, reply => {
      if (!reply?.ok) done({ error: reply?.error || "Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome" });
    });
  });
}

const pad2 = n => String(n).padStart(2, "0");
const clip = (text, n) => {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t;
};

function ghostMark() {
  return el("span", { className: "b-mark", ariaHidden: "true" });
}

function shotNode(e) {
  return el("div", { className: "b-shot" }, el("img", { src: e.image, alt: e.title || "" }));
}

// The cover opens on its best picture: about two thirds of the page is image.
function coverNode(entries, story) {
  const first = entries[0].ts, last = entries[entries.length - 1].ts;
  const sameDay = new Date(first).toDateString() === new Date(last).toDateString();
  const day = d => new Date(d).toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" });
  const pages = new Set(entries.filter(e => e.url).map(e => e.url)).size;
  const questions = entries.reduce((n, e) => n + (e.kind === "answer" ? 1 : e.kind === "conversation" ? (e.turns || []).length : 0), 0);
  const hero = entries[(story.cover || 0) - 1]?.image ? entries[story.cover - 1]
    : entries.find(e => e.kind === "answer" && e.image) || entries.find(e => e.image);
  const stat = (n, one, many) => el("div", {}, el("strong", { textContent: n }), el("span", { textContent: n === 1 ? one : many }));
  const cover = el("section", { className: "b-cover" },
    el("div", { className: "b-brand" }, ghostMark(), el("span", { textContent: "Mia Browser · Reel" }),
      el("span", { className: "b-date", textContent: sameDay ? day(first) : `${day(first)} – ${day(last)}` })),
    el("h1", { className: "b-title", textContent: story.title }),
    story.subtitle ? el("p", { className: "b-subtitle", textContent: story.subtitle }) : "");
  if (hero) cover.append(el("div", { className: "b-hero" }, shotNode(hero)));
  const side = el("div", { className: "b-overview" },
    el("div", { className: "b-stats" }, stat(entries.length, "moment", "moments"), stat(pages, "page", "pages"),
      stat(questions, "question", "questions")),
    story.summary ? el("p", { className: "b-summary", textContent: story.summary }) : "");
  if (story.takeaways.length) {
    side.append(el("ol", { className: "b-takeaways" }, ...story.takeaways.map((t, i) =>
      el("li", {}, el("span", { className: "b-n", textContent: pad2(i + 1) }), el("p", { textContent: t })))));
  }
  cover.append(side);
  return cover;
}

// Under each picture, one short block of words: what it is, what was asked, what it means.
function wordsNode(e, caption) {
  const kinds = { answer: "Answer", page: "Page", conversation: "Conversation", report: "Report" };
  const words = el("div", { className: "b-words" },
    el("p", { className: "b-meta" },
      el("span", { className: "b-kind", textContent: kinds[e.kind] || "Moment" }),
      el("span", { textContent: siteOf(e.url) || clip(e.title, 40) }),
      el("span", { className: "b-time", textContent: whenOf(e.ts) })));
  const asked = e.kind === "answer" ? e.question : e.kind === "conversation" ? e.turns?.[0]?.q : "";
  if (asked) words.append(el("p", { className: "b-q", textContent: clip(asked, 140) }));
  // The writer's caption condenses the answer; without it, a short cut of the answer itself.
  const fallback = e.kind === "answer" ? `${e.answer?.title || ""}. ${e.answer?.body || ""}`
    : e.kind === "conversation" ? e.turns?.[0]?.a?.title
      : e.kind === "report" ? e.markdown?.replace(/[#*_>-]+/g, " ") : e.title;
  const text = caption || clip(fallback, 220);
  if (text) words.append(el("p", { className: "b-caption", textContent: text }));
  return words;
}

// A diagram the writer asked for, drawn here from plain values (never from its markup).
function visualNode(v) {
  const box = el("div", { className: `b-visual ${v.type}` });
  if (v.type === "stats") {
    box.append(...v.items.map(i => el("div", { className: "b-stat" },
      el("strong", { textContent: i.value }), el("span", { textContent: i.label }))));
  } else if (v.type === "steps") {
    box.append(el("ol", {}, ...v.items.map((t, i) => el("li", {},
      el("span", { className: "b-step", textContent: i + 1 }), el("p", { textContent: t })))));
  } else if (v.type === "compare") {
    box.append(...v.columns.map(c => el("div", { className: "b-col" },
      el("h3", { textContent: c.heading }), el("ul", {}, ...c.points.map(p => el("li", { textContent: p }))))));
  } else if (v.type === "bars") {
    const top = Math.max(...v.items.map(i => i.value)) || 1;
    if (v.title) box.append(el("p", { className: "b-vtitle", textContent: v.title }));
    for (const i of v.items) {
      const bar = el("span", { className: "b-bar-fill" });
      bar.style.width = `${Math.max(2, (i.value / top) * 100)}%`;
      box.append(el("div", { className: "b-bar-row" }, el("span", { className: "b-bar-label", textContent: i.label }),
        el("span", { className: "b-bar-track" }, bar),
        el("span", { className: "b-bar-value", textContent: Number(i.value.toFixed(2)).toLocaleString() })));
    }
  } else {
    return "";
  }
  return box;
}

function momentNode(e, n, caption) {
  const figure = el("figure", { className: `b-moment is-${e.kind || "moment"}${e.image ? "" : " no-image"}` });
  if (e.image) figure.append(shotNode(e));
  figure.append(wordsNode(e, caption));
  figure.dataset.n = n;
  return figure;
}

function bookNode(entries, story) {
  const root = el("article", { className: "b-book" }, coverNode(entries, story));
  story.chapters.forEach((chapter, i) => {
    const section = el("section", { className: "b-chapter" },
      el("div", { className: "b-chead" },
        el("span", { className: "b-cnum", textContent: pad2(i + 1) }),
        el("h2", { textContent: chapter.heading }),
        chapter.intro ? el("p", { textContent: chapter.intro }) : ""),
      chapter.visual ? visualNode(chapter.visual) : "");
    // Questions get the full width; plain page visits sit two to a row.
    let pair = null;
    for (const n of chapter.moments) {
      const e = entries[n - 1];
      if (!e) continue;
      const node = momentNode(e, n, story.captions[String(n)]);
      if (e.kind === "page" && e.image) {
        if (!pair || pair.childElementCount === 2) section.append(pair = el("div", { className: "b-pair" }));
        pair.append(node);
      } else {
        pair = null;
        section.append(node);
      }
    }
    root.append(section);
  });
  const closing = el("section", { className: "b-closing" });
  if (story.next_steps.length) {
    closing.append(el("h2", { className: "b-label", textContent: "Next steps" }),
      el("ol", { className: "b-takeaways" }, ...story.next_steps.map((t, i) =>
        el("li", {}, el("span", { className: "b-n", textContent: pad2(i + 1) }), el("p", { textContent: t })))));
  }
  closing.append(el("p", { className: "b-colophon" }, ghostMark(),
    el("span", { textContent: `Made with Mia Browser · ${new Date().toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" })}` })));
  root.append(closing);
  return root;
}

function closeBook() {
  document.body.classList.remove("booking");
  book.replaceChildren();
  document.title = "Mia Browser reel";
}

async function openBook(entries, story, note = "") {
  const save = el("button", { className: "primary", textContent: "Save as PDF" });
  const back = el("button", { textContent: "Back to the reel" });
  save.addEventListener("click", () => print());
  back.addEventListener("click", closeBook);
  const bar = el("div", { className: "b-bar" }, back,
    el("span", { className: "b-note", textContent: note || "In the print dialog, choose “Save as PDF”." }), save);
  const pages = bookNode(entries, story);
  book.replaceChildren(bar, pages);
  document.body.classList.add("booking");
  document.title = story.title; // the PDF's file name
  scrollTo({ top: 0 });
  await Promise.all([...pages.querySelectorAll("img")].map(img => img.decode().catch(() => {})));
  print();
}

makePdf.addEventListener("click", async () => {
  const entries = await reelAll();
  if (!entries.length) {
    alert("The reel is empty. Turn on Reel mode in the Mia Browser menu and browse a shared page first.");
    return;
  }
  makePdf.disabled = true;
  makePdf.textContent = "Writing your booklet…";
  try {
    const { story, error } = await askStory(entries);
    await openBook(entries, story || plainStory(entries),
      story ? "" : `Made without the writer (${error}). In the print dialog, choose “Save as PDF”.`);
  } finally {
    makePdf.disabled = false;
    makePdf.textContent = "Make PDF";
  }
});
