// Injected into the operator console while recording: chapter chip, subtitles, click highlights,
// title cards and "time skipped" badges. Pure presentation; it never changes the demo's behaviour.
(() => {
  if (window.__ov) return;
  const css = `
  #__chap{position:fixed;top:6px;left:50%;transform:translateX(-50%);z-index:9999;background:#0d1520ee;border:1px solid #4cc2ff;
    color:#e8f1ff;font:600 19px system-ui;padding:6px 16px;border-radius:18px;box-shadow:0 4px 18px #0008;transition:opacity .4s}
  #__chap b{color:#4cc2ff;margin-right:8px}
  #__sub{position:fixed;left:50%;bottom:44px;transform:translateX(-50%);z-index:9999;max-width:1380px;width:max-content;
    background:#05090ee6;color:#fff;font:500 25px/1.32 system-ui;padding:10px 20px;border-radius:10px;text-align:center;
    border:1px solid #2a3a50;box-shadow:0 6px 24px #000a;transition:opacity .25s}
  #__sub:empty{opacity:0}
  .__ring{position:fixed;z-index:9998;border:3px solid #ffd84c;border-radius:10px;pointer-events:none;
    box-shadow:0 0 0 4px #ffd84c33,0 0 26px #ffd84c88;animation:__p 1.1s ease-in-out infinite}
  @keyframes __p{0%,100%{box-shadow:0 0 0 3px #ffd84c33,0 0 18px #ffd84c66}50%{box-shadow:0 0 0 9px #ffd84c22,0 0 34px #ffd84caa}}
  #__card{position:fixed;inset:0;z-index:10000;display:flex;flex-direction:column;align-items:center;justify-content:center;
    background:radial-gradient(1200px 700px at 30% 20%,#14324a,#070b10 70%);color:#e8f1ff;font-family:system-ui;transition:opacity .6s}
  #__card.hide{opacity:0;pointer-events:none}
  #__card h1{font-size:64px;margin:0 0 10px;letter-spacing:.5px} #__card h2{font-size:30px;font-weight:400;color:#9fc3e6;margin:0 0 34px}
  #__card .tags{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;max-width:1300px}
  #__card .tags span{border:1px solid #4cc2ff66;color:#bfe6ff;padding:7px 15px;border-radius:16px;font-size:21px;background:#0b1a28}
  #__card .foot{position:absolute;bottom:40px;color:#7f93a8;font-size:20px}
  #__skip{position:fixed;top:60px;right:24px;z-index:9999;background:#2b1d05ee;border:1px solid #ffb648;color:#ffd79a;
    font:600 22px system-ui;padding:8px 16px;border-radius:10px;opacity:0;transition:opacity .4s}
  #__skip.on{opacity:1}`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  const mk = (id) => { const d = document.createElement("div"); d.id = id; document.body.appendChild(d); return d; };
  const chap = mk("__chap"), sub = mk("__sub"), card = mk("__card"), skip = mk("__skip");
  card.className = "hide"; chap.style.opacity = 0;
  let rings = [];
  window.__ov = {
    chap(n, t) { chap.innerHTML = n ? `<b>${n}</b>${t}` : t; chap.style.opacity = 1; },
    sub(t) { sub.textContent = t || ""; },
    card(html) { if (html) { card.innerHTML = html; card.className = ""; } else card.className = "hide"; },
    skip(t) { skip.textContent = t; skip.className = "on"; setTimeout(() => skip.className = "", 3200); },
    clear() { rings.forEach((r) => r.remove()); rings = []; },
    hl(sel, ms = 2600, pad = 6) {
      const el = typeof sel === "string" ? document.querySelector(sel) : sel;
      if (!el) return false;
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      setTimeout(() => {
        const b = el.getBoundingClientRect(), r = document.createElement("div");
        r.className = "__ring";
        Object.assign(r.style, { left: b.left - pad + "px", top: b.top - pad + "px", width: b.width + 2 * pad + "px", height: b.height + 2 * pad + "px" });
        document.body.appendChild(r); rings.push(r);
        setTimeout(() => { r.remove(); rings = rings.filter((x) => x !== r); }, ms);
      }, 250);
      return true;
    },
    click(sel, delay = 700) {
      const el = typeof sel === "string" ? document.querySelector(sel) : sel;
      if (!el) return false;
      this.hl(el, delay + 1200);
      // the cabinet panel re-renders every 200 ms: look the button up again at click time
      setTimeout(() => (typeof sel === "string" ? document.querySelector(sel) || el : el).click(), delay);
      return true;
    },
    // highlight an intersection on the city canvas (no DOM element there)
    hlTsc(t, ms = 2600) {
      const [x, y] = ctr(t), cvb = document.querySelector("#world").getBoundingClientRect();
      const s = 70 * scale, r = document.createElement("div");
      r.className = "__ring";
      Object.assign(r.style, { left: cvb.left + x * scale - s + "px", top: cvb.top + y * scale - s + "px", width: 2 * s + "px", height: 2 * s + "px", borderRadius: "50%" });
      document.body.appendChild(r); rings.push(r);
      setTimeout(() => r.remove(), ms);
    },
    scrollTo(sel, block = "start") { const el = document.querySelector(sel); if (el) el.scrollIntoView({ block, behavior: "smooth" }); return !!el; },
    // set a field in the rule table the way a user would (value + change event)
    setRule(ruleId, sel, value) {
      const row = [...document.querySelectorAll("#r-table .rule")].find((r) => r.querySelector('input[data-k="id"]').value === ruleId);
      const el = row && row.querySelector(sel);
      if (!el) return false;
      this.hl(el, 2200, 3);
      setTimeout(() => { el.value = value; el.dispatchEvent(new Event("change", { bubbles: true })); }, 600);
      return true;
    },
    cabBox(title) { return [...document.querySelectorAll("#cab .box h4")].find((h) => h.textContent.includes(title))?.parentElement; },
  };
})();
