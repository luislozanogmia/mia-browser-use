/**
 * Find text on pages that paint it on a canvas (Google Docs, Sheets, Slides).
 *
 * Those apps have no text in the page for the overlay to point at. This file
 * runs in the page's own world before the app starts and notes each string
 * the app draws with fillText, and where. The overlay, which runs in the
 * extension's isolated world, asks for a phrase with a synchronous
 * "ghost-canvas-find" event and reads the answer from an attribute.
 *
 * It only reads what the page itself draws and never sends it anywhere.
 */
(() => {
  if (window.__ghostCanvasText) return;
  window.__ghostCanvasText = true;

  const RESULT_ATTR = "data-ghost-canvas-hit";
  const MAX_RUNS = 4000; // per canvas
  const runs = new Map(); // canvas -> [{text, x, y, size, font, baseline, a, d}] in device pixels
  const proto = CanvasRenderingContext2D.prototype;
  const originalFill = proto.fillText;
  const originalClear = proto.clearRect;
  const measure = document.createElement("canvas").getContext("2d");

  // Docs wraps each line in invisible direction marks; they have no width.
  const INVISIBLE = /[\u200B-\u200F\u202A-\u202E\u2066-\u2069]/g;

  function fontSize(font) {
    const match = /(\d+(?:\.\d+)?)px/.exec(font || "");
    return match ? Number(match[1]) : 16;
  }

  proto.fillText = function (text, x, y, ...rest) {
    try {
      const canvas = this.canvas;
      const clean = typeof text === "string" ? text.replace(INVISIBLE, "") : "";
      if (canvas instanceof HTMLCanvasElement && clean.trim()) {
        const m = this.getTransform();
        let list = runs.get(canvas);
        if (!list) runs.set(canvas, (list = []));
        const run = {
          text: clean, font: this.font, baseline: this.textBaseline,
          x: m.a * x + m.c * y + m.e, y: m.b * x + m.d * y + m.f,
          a: m.a || 1, d: m.d || 1, size: fontSize(this.font),
        };
        // Drawing the same spot again replaces what was there.
        const same = list.findIndex(r => Math.abs(r.x - run.x) < 0.5 && Math.abs(r.y - run.y) < 0.5);
        if (same >= 0) list[same] = run;
        else if (list.push(run) > MAX_RUNS) list.shift();
      }
    } catch {
      // Never break the app's drawing.
    }
    return originalFill.call(this, text, x, y, ...rest);
  };

  proto.clearRect = function (x, y, w, h) {
    try {
      const list = runs.get(this.canvas);
      if (list) {
        const m = this.getTransform();
        const x0 = m.a * x + m.e, y0 = m.d * y + m.f;
        const x1 = x0 + m.a * w, y1 = y0 + m.d * h;
        const [left, right, top, bottom] = [Math.min(x0, x1), Math.max(x0, x1), Math.min(y0, y1), Math.max(y0, y1)];
        runs.set(this.canvas, list.filter(r => !(r.x >= left && r.x <= right && r.y >= top && r.y <= bottom)));
      }
    } catch {
      // Never break the app's drawing.
    }
    return originalClear.call(this, x, y, w, h);
  };

  // Runs drawn on one line of one canvas, left to right, joined into one string.
  function lines(list) {
    const byLine = new Map();
    for (const run of list) {
      const key = `${Math.round(run.y)}`;
      if (!byLine.has(key)) byLine.set(key, []);
      byLine.get(key).push(run);
    }
    return [...byLine.values()].map(parts => {
      parts.sort((p, q) => p.x - q.x);
      let text = "";
      const pieces = [];
      for (const part of parts) {
        if (text && !text.endsWith(" ") && !part.text.startsWith(" ")) {
          // Separate runs that sit apart, so words drawn one by one still match.
          measure.font = part.font;
          const prev = pieces[pieces.length - 1];
          const prevEnd = prev.run.x + measure.measureText(prev.run.text).width * prev.run.a;
          if (part.x - prevEnd > 1) text += " ";
        }
        pieces.push({ start: text.length, run: part });
        text += part.text;
      }
      return { text, pieces };
    });
  }

  // One character for one, so offsets into the drawn text stay right.
  function normalize(text) {
    return text.toLowerCase().replace(/\s/g, " ").replace(/[\u2018\u2019\u201B\u2032]/g, "'").replace(/[\u201C\u201D\u2033]/g, '"');
  }

  function find(needle) {
    const wanted = normalize(needle).replace(/ +/g, " ").trim();
    if (!wanted) return null;
    for (const [canvas, list] of runs) {
      if (!canvas.isConnected) {
        runs.delete(canvas);
        continue;
      }
      const box = canvas.getBoundingClientRect();
      if (!box.width || !canvas.width) continue;
      const scale = box.width / canvas.width;
      for (const line of lines(list)) {
        const index = normalize(line.text).indexOf(wanted);
        if (index < 0) continue;
        const end = index + wanted.length;
        const xAt = offset => {
          let piece = line.pieces[0];
          for (const p of line.pieces) if (p.start <= offset) piece = p;
          measure.font = piece.run.font;
          const inside = Math.max(0, Math.min(offset - piece.start, piece.run.text.length));
          return piece.run.x + measure.measureText(piece.run.text.slice(0, inside)).width * piece.run.a;
        };
        const run = line.pieces[0].run;
        const height = run.size * Math.abs(run.d);
        const top = run.baseline === "top" || run.baseline === "hanging" ? run.y
          : run.baseline === "middle" ? run.y - height / 2
          : run.baseline === "bottom" || run.baseline === "ideographic" ? run.y - height
          : run.y - height * 0.8;
        const left = xAt(index);
        return {
          x: box.left + left * scale, y: box.top + top * scale,
          w: (xAt(end) - left) * scale, h: height * 1.15 * scale,
        };
      }
    }
    return null;
  }

  // Synchronous: the overlay dispatches, this answers before dispatch returns.
  document.addEventListener("ghost-canvas-find", event => {
    const hit = typeof event.detail === "string" ? find(event.detail.slice(0, 200)) : null;
    document.documentElement.setAttribute(RESULT_ATTR, hit ? JSON.stringify(hit) : "");
  });
})();
