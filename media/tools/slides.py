"""Renders the code-walkthrough slides (HTML -> PNG via headless Chrome) and writes a timeline
for assemble.py: one frame per narration step, shown for the length of its sentence.

Slide kinds: title, diagram (raw HTML), table, list, code. A code slide shows one or more excerpts
("parts": [(file, anchor_text, n_lines)]) and highlights lines per step ("hl": [(anchor_text, n)]).
Anchors are text, not line numbers, so the slides follow the code if it moves.
"""
import asyncio, base64, html, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LEAD, GAP = 0.6, 0.45      # pause on a new slide before speaking; pause between sentences

CSS = """
*{box-sizing:border-box} body{margin:0;width:1920px;height:1080px;overflow:hidden;background:#0b1118;color:#e6eef8;
  font-family:'DejaVu Sans',system-ui,sans-serif}
.hd{height:84px;display:flex;align-items:center;gap:18px;padding:0 36px;border-bottom:1px solid #223246;background:#0e1722}
.hd .ch{background:#4cc2ff;color:#04121d;font-weight:700;border-radius:16px;padding:5px 14px;font-size:20px}
.hd h1{font-size:31px;margin:0;font-weight:600} .hd .path{margin-left:auto;font-family:'DejaVu Sans Mono',monospace;color:#9fc3e6;font-size:20px}
.bd{position:absolute;top:84px;left:0;right:0;bottom:118px;display:flex;gap:26px;padding:22px 36px}
.code{flex:1;background:#0f1620;border:1px solid #223246;border-radius:10px;padding:12px 14px;overflow:hidden}
.code pre{margin:0;font-family:'DejaVu Sans Mono',monospace;white-space:pre-wrap;word-break:break-all}
.code .lineno{color:#4b5d73;margin-right:14px;user-select:none}
.code .hll{background:#4d4110 !important}
.code .dim{opacity:.38} .sep{color:#4b5d73;font-family:'DejaVu Sans Mono',monospace;margin:6px 0;font-size:15px}
.fn{color:#7fa6c9;font-family:'DejaVu Sans Mono',monospace;font-size:15px;margin:2px 0 4px}
.why{width:560px;background:#0f1a26;border:1px solid #2a4560;border-radius:10px;padding:20px 24px}
.why h3{margin:0 0 12px;color:#4cc2ff;font-size:22px;letter-spacing:.5px;text-transform:uppercase}
.why li{font-size:21px;line-height:1.38;margin:0 0 12px;color:#cfe0f2} .why ul{padding-left:22px;margin:0}
.sub{position:absolute;left:36px;right:36px;bottom:18px;height:86px;display:flex;align-items:center;justify-content:center;
  text-align:center;font-size:25px;line-height:1.3;background:#05090e;border:1px solid #223246;border-radius:10px;padding:0 26px;color:#fff}
.title{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  background:radial-gradient(1200px 700px at 30% 20%,#14324a,#070b10 70%)}
.title h1{font-size:62px;margin:0 0 12px} .title h2{font-size:30px;font-weight:400;color:#9fc3e6;margin:0 0 36px}
.tags{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;max-width:1400px}
.tags span{border:1px solid #4cc2ff66;color:#bfe6ff;padding:7px 15px;border-radius:16px;font-size:21px;background:#0b1a28}
table.t{border-collapse:collapse;width:100%;font-size:20px} table.t th{text-align:left;color:#4cc2ff;border-bottom:2px solid #2a4560;padding:9px 12px}
table.t td{border-bottom:1px solid #1d2b3b;padding:9px 12px;vertical-align:top;line-height:1.3} table.t td:first-child{font-weight:600;color:#fff;white-space:nowrap}
table.t td code{font-family:'DejaVu Sans Mono',monospace;color:#9fc3e6;font-size:18px} table.t tr.on td{background:#3b341288}
.list{flex:1;font-size:23px;line-height:1.45} .list li{margin-bottom:10px} .list li.on{color:#ffd84c}
.list code,.why code{font-family:'DejaVu Sans Mono',monospace;color:#9fc3e6;font-size:.9em}
.dg{flex:1;position:relative}
.box{position:absolute;border:2px solid #3d6a91;border-radius:12px;background:#0f1a26;padding:12px 16px;font-size:19px;line-height:1.35}
.box b{display:block;font-size:22px;color:#fff;margin-bottom:4px} .box code{font-family:'DejaVu Sans Mono',monospace;color:#9fc3e6;font-size:17px}
.box.on{border-color:#ffd84c;box-shadow:0 0 0 4px #ffd84c33,0 0 26px #ffd84c66}
.lnk{position:absolute;color:#9fc3e6;font-size:17px;text-align:center}
"""


def read_lines(path):
    return open(os.path.join(ROOT, path)).read().split("\n")


def find(lines, anchor, start=0):
    for i in range(start, len(lines)):
        if anchor in lines[i]:
            return i
    raise ValueError(f"anchor not found: {anchor!r}")


def code_html(slide, hl):
    parts, out, total = slide["parts"], [], sum(p[2] for p in slide["parts"]) + 1.6 * (len(slide["parts"]) - 1)
    fs = max(14.0, min(19.5, 860 / (total * 1.42)))
    for k, (path, anchor, n) in enumerate(parts):
        lines = read_lines(path)
        s = find(lines, anchor)
        seg = lines[s:s + n]
        marks = set()
        for a, m in hl:
            try:
                i = find(seg, a)
            except ValueError:
                continue
            marks.update(range(i, i + m))
        fmt = HtmlFormatter(linenos="inline", linenostart=s + 1, hl_lines=[i + 1 for i in sorted(marks)], nowrap=False, style="github-dark")
        body = highlight("\n".join(seg), get_lexer_for_filename(path), fmt)
        if k or len(parts) > 1:
            out.append(f'<div class="fn">{html.escape(path)} : {s + 1}</div>')
        out.append(body)
        if k < len(parts) - 1:
            out.append('<div class="sep">⋮</div>')
    dim = "pre .lineno ~ *" if hl else ""
    return (f'<div class="code" style="font-size:{fs:.1f}px;line-height:1.42">' + "".join(out) + "</div>",
            HtmlFormatter(style="github-dark").get_style_defs(".code") + (".code pre>span:not(.hll){opacity:.55}" if hl else ""))


def page(slide, step):
    extra_css = ""
    kind = slide["kind"]
    if kind == "title":
        body = f'<div class="title"><h1>{slide["h1"]}</h1><h2>{slide["h2"]}</h2><div class="tags">' + \
               "".join(f"<span>{t}</span>" for t in slide.get("tags", [])) + "</div></div>"
    else:
        hd = f'<div class="hd"><span class="ch">{slide["ch"]}</span><h1>{slide["title"]}</h1><span class="path">{slide.get("path", "")}</span></div>'
        why = ""
        if slide.get("why"):
            why = '<div class="why"><h3>Why</h3><ul>' + "".join(f"<li>{w}</li>" for w in slide["why"]) + "</ul></div>"
        if kind == "code":
            c, extra_css = code_html(slide, step.get("hl", []))
            main = c
        elif kind == "table":
            on = step.get("rows", [])
            main = '<div style="flex:1"><table class="t"><tr>' + "".join(f"<th>{h}</th>" for h in slide["head"]) + "</tr>" + \
                   "".join(f'<tr class="{"on" if i in on else ""}">' + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for i, r in enumerate(slide["rows"])) + "</table></div>"
        elif kind == "list":
            on = step.get("rows", [])
            main = '<ul class="list">' + "".join(f'<li class="{"on" if i in on else ""}">{x}</li>' for i, x in enumerate(slide["items"])) + "</ul>"
        elif kind == "diagram":
            on = step.get("rows", [])
            main = '<div class="dg">' + "".join(
                f'<div class="box {"on" if i in on else ""}" style="left:{b[0]}px;top:{b[1]}px;width:{b[2]}px;height:{b[3]}px">{b[4]}</div>'
                for i, b in enumerate(slide["boxes"])) + "".join(
                f'<div class="lnk" style="left:{l[0]}px;top:{l[1]}px;width:{l[2]}px">{l[3]}</div>' for l in slide.get("links", [])) + "</div>"
        body = hd + f'<div class="bd">{main}{why}</div>'
    sub = f'<div class="sub">{html.escape(step.get("sub", step["t"]))}</div>' if step.get("t") else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}{extra_css}</style></head><body>{body}{sub}</body></html>"


async def render(slides, durations, wavs, out):
    from cdp import Browser
    fdir = os.path.join(out, "frames")
    os.makedirs(fdir, exist_ok=True)
    b = Browser(port=9335)
    await b.start()
    t, frames, speech, n = 0.0, [], [], 0
    for slide in slides:
        first = True
        for step in slide["steps"]:
            fn = f"s{n:04d}.png"
            hp = os.path.join(out, "page.html")
            open(hp, "w").write(page(slide, step))
            await b.call("Page.navigate", url="file://" + hp)
            await asyncio.sleep(0.25)
            for _ in range(5):                       # a busy host can make a capture fail: retry
                r = await b.call("Page.captureScreenshot", format="png")
                if r and "data" in r:
                    break
                await asyncio.sleep(1.0)
            open(os.path.join(fdir, fn), "wb").write(base64.b64decode(r["data"]))
            frames.append((t, fn))
            if first:
                t += LEAD
                first = False
            if step.get("t"):
                d = durations[step["t"]]
                speech.append(dict(t=t, wav=wavs[step["t"]], text=step.get("sub", step["t"]), dur=d))
                t += d + GAP
            else:
                t += step.get("hold", 2.0)
            n += 1
    t += 0.8
    b.close()
    json.dump(dict(chunks=[(0.0, t)], frames=frames, speech=speech, log=[]), open(os.path.join(out, "timeline.json"), "w"))
    print(f"{n} frames, {t:.1f} s")
