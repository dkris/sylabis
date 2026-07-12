"""
The standard interface. Not every learner lives in a terminal: `sylabis
web` serves the whole journey in the browser — courses, lessons, a submit
form, grading feedback, a chat with the guide, and a visual knowledge map.
It is the third harness over the same journey tools the terminal agent and
the MCP server use, so every interface has exactly the same powers.

Deliberately dependency-free: stdlib http.server, hand-rolled markdown
subset, server-rendered SVG for the map. No CDN, no JS framework — the
page a learner opens on a train still works.
"""
import html
import json
import posixpath
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from . import journey
from .tools import JourneyTools, ToolError, _safe_id

# Chart chrome + categorical series colors follow the validated reference
# palette (dataviz method): both modes are selected steps, not an automatic
# flip, and series color marks identity only — text always wears ink tokens.
_CSS = """
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--line:#e1e0d9;--border:rgba(11,11,11,.10);--good:#006300;
--s1:#2a78d6;--s2:#1baf7a;--s3:#eda100;--s4:#008300;--s5:#4a3aa7;
--s6:#e34948;--s7:#e87ba4;--s8:#eb6834}
@media (prefers-color-scheme:dark){:root{--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--line:#2c2c2a;--border:rgba(255,255,255,.10);
--good:#0ca30c;--s1:#3987e5;--s2:#199e70;--s3:#c98500;--s5:#9085e9;
--s6:#e66767;--s7:#d55181;--s8:#d95926}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:1.2rem 1rem 4rem}
a{color:var(--s1)}
nav{display:flex;gap:1rem;align-items:baseline;padding:.4rem 0;
border-bottom:1px solid var(--line);margin-bottom:1.2rem;flex-wrap:wrap}
nav .brand{font-weight:700;color:var(--ink);text-decoration:none}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem}
.card{background:var(--surface);border:1px solid var(--border);
border-radius:10px;padding:1rem 1.2rem;margin:.8rem 0}
.hero{border-left:4px solid var(--s1)}
.chip{display:inline-block;width:.75em;height:.75em;border-radius:50%;
margin-right:.45em;vertical-align:baseline}
.muted{color:var(--muted)}.ok{color:var(--good);font-weight:600}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;
margin:.5rem 0}
.bar span{display:block;height:100%;background:var(--s1);border-radius:4px}
pre{background:var(--surface);border:1px solid var(--border);
border-radius:8px;padding:.8rem 1rem;overflow-x:auto}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em}
label{display:block;font-weight:600;margin:1rem 0 .3rem}
textarea,input[type=number]{width:100%;padding:.6rem;border-radius:8px;
border:1px solid var(--line);background:var(--surface);color:var(--ink);
font:inherit}
textarea{min-height:10rem}
button{margin-top:1rem;padding:.55rem 1.3rem;border-radius:8px;border:0;
background:var(--s1);color:#fff;font:inherit;font-weight:600;cursor:pointer}
figure{margin:1rem 0;overflow-x:auto}
#chatlog{max-height:20rem;overflow-y:auto}
#chatlog p{white-space:pre-wrap;margin:.5rem 0}
#chatlog .you{color:var(--ink2)}
table{border-collapse:collapse}td,th{padding:.3rem .8rem .3rem 0;
text-align:left;vertical-align:top}
"""


def series(i: int) -> str:
    """Categorical slot for course i — fixed order, never cycled; courses
    beyond the 8 validated slots fold into muted (identity via label)."""
    return f"var(--s{i + 1})" if i < 8 else "var(--muted)"


# ------------------------------------------------------------- markdown

_H = re.compile(r"^(#{1,4})\s+(.*)$")
_LI = re.compile(r"^[-*]\s+(.*)$")
_OL = re.compile(r"^\d+\.\s+(.*)$")


def md_to_html(md: str, link_fn=None) -> str:
    """A deliberately small markdown subset: headings, lists, fenced code,
    inline code/bold/links, paragraphs. Lessons are compiled to exactly
    this shape; anything fancier renders as visible text, never breakage."""
    def inline(s: str) -> str:
        s = html.escape(s, quote=True)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

        def link(m):
            href = m.group(2)
            if link_fn:
                href = link_fn(href)
            return f'<a href="{href}">{m.group(1)}</a>'
        return re.sub(r"\[([^\]]+)\]\(([^()\s]+)\)", link, s)

    out, para, mode = [], [], None  # mode: None | "ul" | "ol" | "code"

    def flush_para():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close_list():
        nonlocal mode
        if mode in ("ul", "ol"):
            out.append(f"</{mode}>")
            mode = None

    for line in md.splitlines():
        if line.startswith("```"):
            if mode == "code":
                out.append("</code></pre>")
                mode = None
            else:
                flush_para(); close_list()
                out.append("<pre><code>")
                mode = "code"
            continue
        if mode == "code":
            out.append(html.escape(line))
            continue
        h = _H.match(line)
        li = _LI.match(line) or _OL.match(line)
        if h:
            flush_para(); close_list()
            n = len(h.group(1))
            out.append(f"<h{n}>{inline(h.group(2))}</h{n}>")
        elif li:
            flush_para()
            want = "ol" if _OL.match(line) else "ul"
            if mode != want:
                close_list()
                out.append(f"<{want}>")
                mode = want
            out.append(f"<li>{inline(li.group(1))}</li>")
        elif not line.strip():
            flush_para(); close_list()
        else:
            close_list()
            para.append(line.strip())
    flush_para(); close_list()
    if mode == "code":
        out.append("</code></pre>")
    return "\n".join(out)


# ----------------------------------------------------------- knowledge map

def knowledge_svg(steps: list[dict], know: list[dict]) -> str:
    """The knowledge map as a bipartite graph: courses on the left, verified
    concepts on the right, an edge per piece of evidence in the course's
    color. Concepts proven in more than one course (the connections) get a
    ring and a bold label — identity never rides on color alone."""
    courses = [s["course"] for s in steps]
    if not courses or not know:
        return ""
    idx = {c: i for i, c in enumerate(courses)}
    rows = max(len(courses), len(know))
    height = rows * 36 + 40
    xc, xk, width = 200, 460, 780

    def ys(n: int) -> list[float]:
        return [20 + (i + 0.5) * (height - 40) / n for i in range(n)]

    cy, ky = ys(len(courses)), ys(len(know))
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" '
             f'height="{height}" role="img" '
             f'aria-label="Knowledge map: courses and verified concepts">']
    for j, e in enumerate(know):
        for ev in e["evidence"]:
            i = idx.get(ev["course"])
            if i is None:
                continue
            grade = f"{ev['grade']:.0%}" if ev.get("grade") else "passed"
            parts.append(
                f'<path d="M {xc} {cy[i]:.1f} C {xc + 90} {cy[i]:.1f}, '
                f'{xk - 90} {ky[j]:.1f}, {xk} {ky[j]:.1f}" fill="none" '
                f'stroke="{series(i)}" stroke-width="2" opacity=".75">'
                f'<title>{html.escape(e["concept"])} — '
                f'{html.escape(ev["course_title"])} ({grade})</title></path>')
    for i, s in enumerate(steps):
        parts.append(
            f'<circle cx="{xc}" cy="{cy[i]:.1f}" r="7" fill="{series(i)}">'
            f'<title>{html.escape(s["course_title"])}</title></circle>'
            f'<text x="{xc - 14}" y="{cy[i]:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" fill="var(--ink2)" font-size="13">'
            f'{html.escape(_clip(s["course_title"], 26))}</text>')
    for j, e in enumerate(know):
        bridge = len({ev["course"] for ev in e["evidence"]}) > 1
        ring = (' stroke="var(--ink)" stroke-width="2"' if bridge else "")
        weight = ' font-weight="600"' if bridge else ""
        parts.append(
            f'<circle cx="{xk}" cy="{ky[j]:.1f}" r="7" '
            f'fill="var(--surface)" stroke="var(--muted)"{ring}>'
            f'<title>{html.escape(e["concept"])}'
            f'{" — links courses" if bridge else ""}</title></circle>'
            f'<text x="{xk + 14}" y="{ky[j]:.1f}" dominant-baseline="middle" '
            f'fill="var(--ink)" font-size="13"{weight}>'
            f'{html.escape(_clip(e["concept"], 38))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _bridge_names(entry: dict) -> list[str]:
    """Course titles for a connection line — directory names when titles
    collide, so 'X links A and A' can never happen."""
    titles = sorted({ev["course_title"] for ev in entry["evidence"]})
    if len(titles) > 1:
        return titles
    return sorted({ev["course"] for ev in entry["evidence"]})


# ------------------------------------------------------------------ app

class WebApp:
    def __init__(self, home_dir: Path, mock: bool = False):
        self.home = Path(home_dir)
        self.mock = mock
        self.tools = JourneyTools(self.home, mock=mock)
        self.lock = threading.Lock()  # grading and chat are one-at-a-time
        self._agent = None
        self._chat: list[dict] = []

    # ------------------------------------------------------------- pages

    def page(self, title: str, body: str) -> str:
        return (f'<!DOCTYPE html><html lang="en"><head>'
                f'<meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,'
                f'initial-scale=1">'
                f"<title>{html.escape(title)} — sylabis</title>"
                f"<style>{_CSS}</style></head><body><main>"
                f'<nav><a class="brand" href="/">sylabis</a>'
                f'<a href="/">journey</a>'
                f'<a href="/knowledge">knowledge</a></nav>'
                f"{body}</main></body></html>")

    def dashboard(self) -> str:
        steps = journey.next_steps(self.home)
        if not steps:
            return self.page("Your journey", (
                "<h1>Welcome</h1><div class='card hero'><p>Nothing here yet."
                " Start a course from the terminal —</p>"
                "<pre><code>sylabis learn \"a topic you want to learn\""
                "</code></pre>"
                "<p>— or ask the guide below.</p></div>" + self._chat_html()))
        know = journey.knowledge(self.home)
        body = ["<h1>Your journey</h1>"]
        ready = [s for s in steps if s["status"] == "ready"]
        if ready:
            s = ready[0]
            body.append(
                f'<div class="card hero"><strong>Next:</strong> '
                f'{html.escape(s["title"])} '
                f'<span class="muted">(~{s["estimated_hours"]}h, '
                f'{html.escape(s["course_title"])})</span><br>'
                f'<a href="/course/{s["course"]}/lesson/{s["milestone_id"]}">'
                f"Open the lesson →</a></div>")
        for i, s in enumerate(steps):
            cdir = journey.course_dir(self.home, s["course"])
            manifest = yaml.safe_load((cdir / "course.yaml").read_text())
            total = len(manifest["milestones"])
            passed = sum(journey.milestone_passed(cdir, m["id"])
                         for m in manifest["milestones"])
            pct = round(100 * passed / total) if total else 0
            if s["status"] == "complete":
                status = '<span class="ok">✓ complete</span>'
            elif s["status"] == "blocked":
                status = (f'<span class="muted">blocked on '
                          f'{html.escape(", ".join(s["blocked_on"]))}</span>')
            else:
                status = (f'next: {html.escape(s["milestone_id"])} — '
                          f'{html.escape(s["title"])}')
            body.append(
                f'<div class="card"><span class="chip" '
                f'style="background:{series(i)}"></span>'
                f'<a href="/course/{s["course"]}">'
                f'<strong>{html.escape(s["course_title"])}</strong></a> '
                f'<span class="muted">{passed}/{total} milestones</span>'
                f'<div class="bar" role="progressbar" aria-valuenow="{pct}" '
                f'aria-valuemin="0" aria-valuemax="100"><span '
                f'style="width:{pct}%"></span></div>{status}</div>')
        body.append(f"<h2>Knowledge</h2><p>{len(know)} verified "
                    f'concepts. <a href="/knowledge">See the map →</a></p>')
        body.append(self._chat_html())
        return self.page("Your journey", "".join(body))

    def knowledge_page(self) -> str:
        steps = journey.next_steps(self.home)
        know = journey.knowledge(self.home)
        body = ["<h1>Knowledge map</h1>"]
        svg = knowledge_svg(steps, know)
        if not svg:
            body.append("<p class='muted'>Nothing verified yet — pass a "
                        "milestone and the map begins.</p>")
        else:
            body.append(f"<figure>{svg}</figure>")
            body.append("<h2>Verified concepts</h2><table>")
            for e in know:
                refs = "<br>".join(
                    f'<a href="/course/{ev["course"]}/doc?p=portfolio/claims/'
                    f'{ev["milestone_id"]}.md">{html.escape(ev["course_title"])}'
                    f' · {html.escape(ev["milestone_id"])}</a>'
                    for ev in e["evidence"])
                body.append(f"<tr><td><strong>{html.escape(e['concept'])}"
                            f"</strong></td><td>{refs}</td></tr>")
            body.append("</table>")
            bridges = [e for e in know
                       if len({ev["course"] for ev in e["evidence"]}) > 1]
            if bridges:
                body.append("<h2>Connections</h2><ul>")
                for e in bridges:
                    body.append(f"<li><strong>{html.escape(e['concept'])}"
                                f"</strong> links "
                                f"{html.escape(' and '.join(_bridge_names(e)))}"
                                f"</li>")
                body.append("</ul>")
        return self.page("Knowledge map", "".join(body))

    def course_page(self, name: str) -> str:
        cdir = self._cdir(name)
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        body = [f"<h1>{html.escape(manifest['meta']['title'])}</h1>"]
        target = manifest.get("learner", {}).get("target_artifact", "")
        if target:
            body.append(f"<p class='muted'>{html.escape(target)}</p>")
        body.append("<h2>Milestones</h2>")
        for m in manifest["milestones"]:
            gpath = cdir / m["id"] / "grade.yaml"
            if journey.milestone_passed(cdir, m["id"]):
                g = yaml.safe_load(gpath.read_text()) or {}
                status = (f'<span class="ok">✓ passed '
                          f'({g.get("grade", 0):.0%})</span>')
            elif gpath.exists():
                g = yaml.safe_load(gpath.read_text()) or {}
                status = f'attempt {g.get("attempt", 1)} — not yet'
            else:
                status = '<span class="muted">not started</span>'
            body.append(
                f'<div class="card">'
                f'<a href="/course/{name}/lesson/{m["id"]}">'
                f'<strong>{html.escape(m["id"])} — {html.escape(m["title"])}'
                f'</strong></a> <span class="muted">'
                f'(~{m["estimated_hours"]}h)</span><br>{status}</div>')
        body.append(f'<p><a href="/course/{name}/doc?p=knowledge/index.md">'
                    f"Sources</a> · "
                    f'<a href="/course/{name}/doc?p=portfolio/index.md">'
                    f"Portfolio</a></p>")
        return self.page(manifest["meta"]["title"], "".join(body))

    def lesson_page(self, name: str, mid: str) -> str:
        lesson = self.tools.call("get_lesson",
                                 {"course": name, "milestone_id": mid})
        _, body_md = _split_frontmatter(lesson)
        form = (
            f"<h2>Submit your work</h2>"
            f"<p class='muted'>Your own words — the grader probes "
            f"understanding, not polish.</p>"
            f'<form method="post" action="/course/{name}/submit/{mid}">'
            f'<label for="artifact">Artifact</label>'
            f'<textarea id="artifact" name="artifact" required></textarea>'
            f'<label for="reflection">Reflection</label>'
            f'<textarea id="reflection" name="reflection" required>'
            f"</textarea>"
            f'<label for="hours">Hours spent (optional)</label>'
            f'<input id="hours" name="hours" type="number" step="0.5" '
            f'min="0">'
            f"<button>Grade it</button></form>")
        return self.page(mid, md_to_html(body_md, self._linker(name, mid))
                         + form)

    def submit(self, name: str, mid: str, form: dict) -> str:
        args = {"course": name, "milestone_id": mid,
                "artifact": form.get("artifact", [""])[0],
                "reflection": form.get("reflection", [""])[0]}
        hours = form.get("hours", [""])[0]
        if hours:
            args["hours_actual"] = float(hours)
        with self.lock:
            feedback = self.tools.call("submit_work", args)
        passed = "PASSED" in feedback.splitlines()[0]
        headline = ('<span class="ok">✓ Passed.</span>' if passed
                    else "Not yet — that's a path signal, not a verdict.")
        return self.page(
            f"Graded — {mid}",
            f"<h1>{headline}</h1><pre>{html.escape(feedback)}</pre>"
            f'<p><a href="/course/{name}/lesson/{mid}">Back to the lesson'
            f"</a> · <a href='/'>Journey</a> · "
            f"<a href='/knowledge'>Knowledge map</a></p>")

    def doc_page(self, name: str, rel: str) -> str:
        cdir = self._cdir(name)
        path = (cdir / rel).resolve()
        if not path.is_relative_to(cdir.resolve()) or \
                path.suffix not in (".md", ".yaml", ".jsonl") or \
                not path.exists():
            raise ToolError(f"No document {rel!r} in this course.")
        text = path.read_text()
        if path.suffix != ".md":
            return self.page(rel, f"<h1>{html.escape(rel)}</h1>"
                                  f"<pre>{html.escape(text)}</pre>")
        _, body_md = _split_frontmatter(text)
        return self.page(rel, md_to_html(body_md, self._linker(name, rel)))

    # -------------------------------------------------------------- chat

    def chat_enabled(self) -> bool:
        import os
        return bool(os.environ.get("ANTHROPIC_API_KEY")) and not self.mock

    def chat_reply(self, message: str) -> str:
        from .agent import Agent
        from .console import Console
        with self.lock:
            if self._agent is None:  # same loop as the terminal, silenced
                self._agent = Agent(self.home, console=Console(enabled=False))
            if not self._chat:
                message = f"(new session — orient first)\n{message}"
            self._chat.append({"role": "user", "content": message})
            return self._agent.turn(self._chat)

    def _chat_html(self) -> str:
        if not self.chat_enabled():
            return ""
        return """
<h2>Ask the guide</h2>
<div class="card"><div id="chatlog" aria-live="polite"></div>
<form id="chatform"><label for="chatmsg">Your message</label>
<textarea id="chatmsg" rows="2"></textarea>
<button>Send</button></form></div>
<script>
const log=document.getElementById('chatlog'),
      form=document.getElementById('chatform'),
      box=document.getElementById('chatmsg');
form.addEventListener('submit',async e=>{
  e.preventDefault();
  const msg=box.value.trim(); if(!msg) return;
  add('you',msg); box.value=''; box.disabled=true;
  try{
    const r=await fetch('/chat',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg})});
    const d=await r.json();
    add('guide',d.reply||d.error||'(no reply)');
  }catch(err){add('guide','(connection lost — is the server running?)')}
  box.disabled=false; box.focus();
});
function add(who,text){
  const p=document.createElement('p');
  p.className=who==='you'?'you':'';
  p.textContent=(who==='you'?'you: ':'')+text;
  log.appendChild(p); log.scrollTop=log.scrollHeight;
}
</script>"""

    # ------------------------------------------------------------ helpers

    def _cdir(self, name: str) -> Path:
        cdir = journey.course_dir(self.home, _safe_id(name, "course"))
        if not (cdir / "course.yaml").exists():
            raise ToolError(f"No course {name!r} in the journey.")
        return cdir

    def _linker(self, name: str, current_rel: str):
        """Rewrite a document's relative links to web routes so a lesson's
        pointer at knowledge/source-x.md keeps working in the browser."""
        base = posixpath.dirname(current_rel)

        def fn(href: str) -> str:
            if href.startswith(("http://", "https://", "#", "/")):
                return href
            rel = posixpath.normpath(posixpath.join(base, href))
            if rel.startswith(".."):
                return href
            return f"/course/{name}/doc?p={rel}"
        return fn


def _split_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return "", text


# ---------------------------------------------------------------- server

_ROUTES = [
    ("GET", re.compile(r"^/$"), "dashboard"),
    ("GET", re.compile(r"^/knowledge$"), "knowledge_page"),
    ("GET", re.compile(r"^/course/([A-Za-z0-9._-]+)$"), "course_page"),
    ("GET", re.compile(r"^/course/([A-Za-z0-9._-]+)/lesson/"
                       r"([A-Za-z0-9._-]+)$"), "lesson_page"),
    ("POST", re.compile(r"^/course/([A-Za-z0-9._-]+)/submit/"
                        r"([A-Za-z0-9._-]+)$"), "submit"),
    ("GET", re.compile(r"^/course/([A-Za-z0-9._-]+)/doc$"), "doc_page"),
    ("POST", re.compile(r"^/chat$"), "chat"),
]


class _Handler(BaseHTTPRequestHandler):
    server_version = "sylabis"

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        app: WebApp = self.server.app  # type: ignore[attr-defined]
        url = urlparse(self.path)
        for verb, pat, action in _ROUTES:
            m = pat.match(url.path)
            if verb != method or not m:
                continue
            try:
                if action == "submit":
                    form = parse_qs(self._body().decode())
                    return self._html(app.submit(*m.groups(), form))
                if action == "chat":
                    if not app.chat_enabled():
                        return self._json(503, {"error": "chat needs "
                                                "ANTHROPIC_API_KEY"})
                    msg = json.loads(self._body()).get("message", "")
                    return self._json(200, {"reply": app.chat_reply(msg)})
                if action == "doc_page":
                    rel = parse_qs(url.query).get("p", [""])[0]
                    return self._html(app.doc_page(m.group(1), rel))
                return self._html(getattr(app, action)(*m.groups()))
            except ToolError as e:
                return self._html(app.page("Not found",
                                           f"<h1>Hmm.</h1><p>{html.escape(str(e))}"
                                           f"</p><p><a href='/'>Journey</a></p>"),
                                  status=404)
            except Exception as e:  # a page bug should render, not hang
                return self._html(app.page("Error",
                                           f"<h1>Something broke.</h1>"
                                           f"<pre>{html.escape(str(e))}</pre>"),
                                  status=500)
        self._html(app.page("Not found", "<h1>No such page.</h1>"
                            "<p><a href='/'>Back to the journey</a></p>"),
                   status=404)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _html(self, text: str, status: int = 200) -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quiet: one line per request on stderr
        import sys
        print(f"[sylabis.web] {self.address_string()} {fmt % args}",
              file=sys.stderr)


def make_server(home_dir: Path, port: int = 8787,
                mock: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.app = WebApp(home_dir, mock=mock)  # type: ignore[attr-defined]
    return server


def serve(home_dir: Path, port: int = 8787, mock: bool = False) -> None:
    server = make_server(home_dir, port=port, mock=mock)
    host, actual_port = server.server_address[:2]
    url = f"http://{host}:{actual_port}/"
    print(f"sylabis web — your journey at {url}  (Ctrl-C to stop)")
    if not server.app.chat_enabled():  # type: ignore[attr-defined]
        print("  (chat with the guide is off — set ANTHROPIC_API_KEY "
              "to turn it on)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
