"""
The standard interface — sylabis "Reading Room" (design concept 1a).
A calm, single-column editorial surface: warm paper, Didot display over
Georgia body with mono labels, the aka/matcha/coral brand palette. The
reading column carries lessons, grading, and the knowledge map; Sy waits
behind an "Ask Sy" tab and slides in as a right dock only when called.
Reading comes first; the agent stays quiet until asked.

It is the third harness over the same journey tools the terminal agent
and the MCP server use, so every interface has exactly the same powers.
Deliberately dependency-free: stdlib http.server, hand-rolled markdown
subset, server-rendered SVG for the map. No JS framework, no build step,
and no third-party requests: the display face is a system stack (Didot /
Georgia), so a page view never beacons anyone.

Security model (Jupyter's, wholesale): the server binds 127.0.0.1 and
every route requires a per-session bearer token, printed once as a
tokenized URL at startup and exchanged for an HttpOnly session cookie.
The Host header must be localhost/127.0.0.1 (the DNS-rebinding defense),
POST bodies are size-capped, POSTs are Origin-checked, and HTML forms
carry a CSRF token. Tests authenticate via the documented hooks:
WebApp(..., token=...) or by reading server.app.token / server.app.csrf.
"""
import hmac
import html
import json
import math
import os
import posixpath
import re
import secrets
import shutil
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from . import journey
from . import okf
from .tools import JourneyTools, ToolError, _safe_id

# sylabis design tokens (design-system project, tokens/*.css) — editorial,
# single-mode: ink lives on paper, color marks identity and action only.
_CSS = """
:root{
--shiro:#FFFFFF;--paper:#F9F9F7;--surface:#FCFCFB;--sumi:#1A1A1A;
--ink-2:#52514E;--muted:#898781;--line:#E8E8E4;--line-soft:#F0F0EC;
--aka:#E03D28;--aka-press:#C4321F;--coral:#F2907E;--coral-tint:#FFF3F2;
--matcha:#2D5A30;--matcha-tint:#EDFAEE;
--series-1:#E03D28;--series-2:#2D5A30;--series-3:#C98A2E;--series-4:#2A6F97;
--series-5:#7A4E8C;--series-6:#B5482F;--series-7:#5C7A3F;--series-8:#C25B7C;
--font-display:'Didot','Bodoni MT','Georgia',serif;
--font-body:'Georgia','Times New Roman',serif;
--font-mono:'Courier New','Courier',ui-monospace,Menlo,monospace;
--radius-xs:2px;--radius-sm:4px;--radius-md:6px;
--ease:cubic-bezier(.2,0,.2,1)}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--paper);color:var(--sumi);
font:400 15px/1.65 var(--font-body)}
a{color:var(--aka);text-decoration:none}
a:hover{opacity:.75}
@keyframes syRise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes syPulse{0%,100%{opacity:1}50%{opacity:.35}}

.topnav{height:58px;display:flex;align-items:center;gap:26px;padding:0 40px;
border-bottom:1px solid var(--line);background:var(--paper)}
.wordmark{font-family:var(--font-display);font-size:23px;color:var(--sumi);
margin-right:auto}
.wordmark b{color:var(--aka);font-weight:400}
.navlink{font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;
text-transform:uppercase;color:var(--ink-2)}
main{max-width:680px;margin:0 auto;padding:44px 40px 90px;
animation:syRise .3s var(--ease)}

.eyebrow{font-family:var(--font-mono);font-size:11px;letter-spacing:.2em;
text-transform:uppercase;color:var(--matcha);margin:0 0 12px}
.eyebrow.hot{color:var(--aka)}
.eyebrow.quiet{color:var(--muted)}
h1{font-family:var(--font-display);font-weight:400;font-size:32px;
line-height:1.2;margin:0 0 16px}
h1.hero-h,h2.hero-h{font-size:44px;line-height:1.12}
h2{font-family:var(--font-display);font-weight:400;font-size:22px;
margin:34px 0 9px}
h3,h4{font-family:var(--font-display);font-weight:400;font-size:19px;
margin:22px 0 7px}
p{margin:0 0 14px;color:var(--ink-2)}
.lead{font-size:18px;line-height:1.6}
.quiet{color:var(--muted)}
em{color:var(--aka)}
li{color:var(--ink-2);margin-bottom:5px}
pre{background:var(--shiro);border:1px solid var(--line);
border-radius:var(--radius-md);padding:12px 14px;overflow-x:auto;
font-family:var(--font-mono);font-size:12.5px;line-height:1.6}
code{font-family:var(--font-mono);font-size:.92em}
table{border-collapse:collapse}
td,th{padding:.3rem .9rem .3rem 0;text-align:left;vertical-align:top}

.card{background:var(--surface);border:1px solid var(--line);
border-radius:var(--radius-md);padding:16px 20px;margin:0 0 12px}
.card.next{border-left:4px solid var(--aka);padding:20px 22px;margin:0 0 26px}
.card .title{font-family:var(--font-display);font-size:19px;color:var(--sumi)}
.card.next .title{font-size:24px;display:block;margin-bottom:5px}
.mini{font-family:var(--font-mono);font-size:10px;letter-spacing:.16em;
text-transform:uppercase;color:var(--aka);margin:0 0 9px}
.mini.ok{color:var(--matcha)}
.mini.quiet{color:var(--muted)}
.meta{font-family:var(--font-mono);font-size:10px;letter-spacing:.05em;
color:var(--muted)}
.chip{display:inline-block;width:11px;height:11px;border-radius:50%;
margin-right:9px;vertical-align:baseline}
.bar{display:block;height:8px;background:var(--line);border-radius:4px;
overflow:hidden;margin:11px 0 9px}
.bar span{display:block;height:100%;border-radius:4px}
.rowline{display:flex;align-items:baseline;justify-content:space-between;
border-top:1px solid var(--line);padding-top:18px;margin-top:26px}

.btn{display:inline-block;padding:11px 22px;border:0;
border-radius:var(--radius-xs);background:var(--aka);color:#fff;
font-family:var(--font-mono);font-size:11px;letter-spacing:.15em;
text-transform:uppercase;cursor:pointer}
.btn:hover{background:var(--aka-press);opacity:1}
.btn.outline{background:transparent;border:1px solid var(--aka);
color:var(--aka)}
.btn.ghost{background:transparent;color:var(--ink-2)}
.linkbtn{font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;
text-transform:uppercase;color:var(--aka)}

label{display:block;font-weight:700;font-size:15px;margin:0 0 6px}
textarea,input[type=text],input[type=number]{width:100%;padding:11px 13px;
border:1px solid var(--line);border-radius:var(--radius-md);
background:var(--surface);color:var(--sumi);font-family:var(--font-body);
font-size:15px;outline:none}
textarea{min-height:120px;resize:vertical;margin-bottom:16px}
textarea:focus,input:focus{border-color:var(--aka)}

.capsule{background:var(--paper);border:1px solid var(--line);
border-radius:var(--radius-md);padding:16px 18px;margin:0 0 26px}
.capsule .body{font-size:15px;line-height:1.6;color:var(--sumi)}
.tag{display:inline-block;font-family:var(--font-mono);font-size:10px;
letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;
border-radius:var(--radius-xs)}
.tag.pass{background:var(--matcha-tint);color:var(--matcha)}
.tag.fail{background:var(--coral-tint);color:var(--aka)}
.signal{border-radius:var(--radius-md);padding:18px 20px;margin:0 0 16px}
.signal.up{background:var(--matcha-tint);border:1px solid var(--matcha)}
.signal.flat{background:var(--coral-tint);border:1px solid var(--coral)}
.signal .title{font-family:var(--font-display);font-size:20px;
color:var(--sumi);margin-bottom:5px}
.concept-tag{display:inline-block;font-family:var(--font-mono);font-size:11px;
letter-spacing:.06em;padding:5px 11px;border:1px solid var(--matcha);
border-radius:var(--radius-xs);color:var(--matcha);margin:0 8px 8px 0}
.suggest{appearance:none;padding:7px 13px;background:var(--surface);
border:1px solid var(--line);border-radius:20px;font-family:var(--font-body);
font-size:13px;color:var(--ink-2);cursor:pointer}
.points{border-top:1px solid var(--line);padding-top:24px;display:grid;
grid-template-columns:1fr 1fr 1fr;gap:22px;margin-top:40px}
.points .k{font-family:var(--font-mono);font-size:10px;letter-spacing:.14em;
text-transform:uppercase;color:var(--matcha);margin-bottom:8px}
.points .v{font-size:14px;line-height:1.5;color:var(--ink-2)}
.stage{display:flex;gap:16px;padding:16px 0;border-top:1px solid var(--line)}
.stage .mark{flex:none;width:22px;text-align:center;color:var(--aka);
animation:syPulse 1.4s infinite}
.stage .lbl{font-family:var(--font-mono);font-size:11px;letter-spacing:.13em;
text-transform:uppercase;color:var(--muted);margin-bottom:4px}
details{margin:0 0 26px}
summary{font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;
text-transform:uppercase;color:var(--muted);cursor:pointer}

.sy-launch{position:fixed;right:0;top:96px;z-index:5;appearance:none;
cursor:pointer;background:var(--sumi);color:#fff;border:0;
border-radius:8px 0 0 8px;padding:14px 9px;writing-mode:vertical-rl;
transform:rotate(180deg);font-family:var(--font-mono);font-size:10px;
letter-spacing:.2em;text-transform:uppercase}
.sy-dock{position:fixed;top:0;right:0;bottom:0;width:352px;z-index:6;
background:var(--surface);border-left:1px solid var(--line);
box-shadow:-8px 0 30px rgba(26,26,26,.08);display:flex;
flex-direction:column;transform:translateX(100%);
transition:transform .18s var(--ease)}
.sy-dock.open{transform:none}
.sy-head{flex:none;display:flex;align-items:center;gap:10px;
padding:16px 18px;border-bottom:1px solid var(--line)}
.sy-head .dot{width:9px;height:9px;border-radius:50%;
background:var(--matcha)}
.sy-head .name{font-family:var(--font-display);font-size:17px}
.sy-head .sub{font-family:var(--font-mono);font-size:9px;
letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.sy-head button{appearance:none;background:none;border:0;cursor:pointer;
font-size:18px;color:var(--muted);line-height:1;margin-left:auto}
.sy-log{flex:1;overflow-y:auto;padding:18px}
.sy-log .who{font-family:var(--font-mono);font-size:9px;
letter-spacing:.12em;text-transform:uppercase;margin-bottom:5px}
.sy-log .who.sy{color:var(--matcha)}
.sy-log .who.you{color:var(--muted)}
.sy-log .msg{font-size:14px;line-height:1.6;white-space:pre-wrap;
margin-bottom:16px}
.sy-foot{flex:none;border-top:1px solid var(--line);padding:14px}
.sy-foot textarea{min-height:52px;background:var(--paper);font-size:14px;
resize:none;margin-bottom:10px}
.sy-foot .btn{width:100%;padding:10px;font-size:10px}
@media (max-width:760px){.points{grid-template-columns:1fr}
.topnav{padding:0 20px}main{padding:32px 20px 70px}}
"""


def series(i: int) -> str:
    """Categorical slot for course i — fixed order, never cycled; courses
    beyond the 8 brand slots fold into muted (identity via label)."""
    return f"var(--series-{i + 1})" if i < 8 else "var(--muted)"


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
    series color. Concepts proven in more than one course (the connections)
    get a sumi ring and a bold label — identity never rides on color alone."""
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
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'style="max-width:{width}px;font-family:var(--font-body)" '
             f'role="img" '
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
                f'stroke="{series(i)}" stroke-width="2" opacity=".65">'
                f'<title>{html.escape(e["concept"])} — '
                f'{html.escape(ev["course_title"])} ({grade})</title></path>')
    for i, s in enumerate(steps):
        parts.append(
            f'<circle cx="{xc}" cy="{cy[i]:.1f}" r="7" fill="{series(i)}">'
            f'<title>{html.escape(s["course_title"])}</title></circle>'
            f'<text x="{xc - 14}" y="{cy[i]:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" fill="var(--ink-2)" font-size="13">'
            f'{html.escape(_clip(s["course_title"], 26))}</text>')
    for j, e in enumerate(know):
        bridge = len({ev["course"] for ev in e["evidence"]}) > 1
        ring = (' stroke="var(--sumi)" stroke-width="2"' if bridge
                else ' stroke="var(--muted)"')
        weight = ' font-weight="700"' if bridge else ""
        parts.append(
            f'<circle cx="{xk}" cy="{ky[j]:.1f}" r="7" '
            f'fill="var(--surface)"{ring}>'
            f'<title>{html.escape(e["concept"])}'
            f'{" — links courses" if bridge else ""}</title></circle>'
            f'<text x="{xk + 14}" y="{ky[j]:.1f}" dominant-baseline="middle" '
            f'fill="var(--sumi)" font-size="13"{weight}>'
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

MAX_BODY = 2 * 1024 * 1024  # request-body cap: artifacts are text, not uploads
COOKIE_NAME = "sylabis_session"
CHAT_MESSAGE_CAP = 60   # messages per chat session before a reset is needed
CHAT_SESSION_CAP = 32   # per-tab chat histories kept server-side
_CHAT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}$")


class WebApp:
    """The Reading Room behind an auth wall.

    Test hooks (documented, deliberate): pass ``token=`` to know the
    bearer token up front, or read ``.token`` / ``.csrf`` off the
    instance (``server.app``); ``.max_body`` is overridable so the
    body-size cap can be tested with small payloads; ``.jobs`` is the
    background-compile registry that ``/status/<job>`` reads.
    """

    def __init__(self, home_dir: Path, mock: bool = False,
                 token: str | None = None):
        self.home = Path(home_dir)
        self.mock = mock
        self.tools = JourneyTools(self.home, mock=mock)
        # Scoped to journey-STATE mutation (grading, emit_map, claiming a
        # new course dir) — never held across a whole compile or a model
        # call, so the server stays responsive mid-compile.
        self.lock = threading.Lock()
        # --- auth: per-session bearer token -> HttpOnly session cookie
        self.token = token or secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self._session = secrets.token_urlsafe(32)
        self.max_body = MAX_BODY
        # --- background compiles: job id -> {state, topic, course_dir, ...}
        self.jobs: dict[str, dict] = {}
        # --- chat: one capped history per browser tab, never one global
        self._chat_lock = threading.Lock()
        self._chats: dict[str, list] = {}
        self._agent = None

    # -------------------------------------------------------------- auth

    def token_ok(self, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(
            supplied.encode(), self.token.encode())

    def csrf_ok(self, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(
            supplied.encode(), self.csrf.encode())

    def cookie_ok(self, header: str) -> bool:
        for part in (header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value and hmac.compare_digest(
                    value.encode(), self._session.encode()):
                return True
        return False

    def session_cookie(self) -> str:
        """Set-Cookie value handed out when a request presents the token."""
        return (f"{COOKIE_NAME}={self._session}; HttpOnly; "
                f"SameSite=Strict; Path=/")

    # ------------------------------------------------------------- shell

    def page(self, title: str, body: str) -> str:
        return (f'<!DOCTYPE html><html lang="en"><head>'
                f'<meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,'
                f'initial-scale=1">'
                f"<title>{html.escape(title)} — sylabis</title>"
                f"<style>{_CSS}</style></head><body>"
                f'<nav class="topnav">'
                f'<a class="wordmark" href="/">sylabis<b>.</b></a>'
                f'<a class="navlink" href="/">journey</a>'
                f'<a class="navlink" href="/knowledge">knowledge</a></nav>'
                f"<main>{body}</main>{self._sy_dock()}</body></html>")

    def _sy_dock(self) -> str:
        """Sy behind a tab, per the Reading Room: present on every page,
        on screen only when called."""
        if self.chat_enabled():
            foot = ('<form id="syform"><label for="symsg" '
                    'style="display:none">Your message</label>'
                    '<textarea id="symsg" '
                    'placeholder="Ask about the milestone…"></textarea>'
                    '<button class="btn">Send</button></form>')
            first = ("Ask about the milestone, the source, or where to go "
                     "next. I won't write your artifact — that's yours.")
        else:
            foot = ""
            first = ("Sy needs ANTHROPIC_API_KEY on the server to talk. "
                     "The rest of the room works without it.")
        shell = f"""
<button class="sy-launch" id="sylaunch" aria-expanded="false"
 aria-controls="sydock">Ask Sy</button>
<aside class="sy-dock" id="sydock" aria-label="Sy, your guide">
  <div class="sy-head"><span class="dot"></span>
    <div><div class="name">Sy</div>
    <div class="sub">your guide · here when called</div></div>
    <button id="syreset" aria-label="New chat" title="New chat">↺</button>
    <button id="syclose" aria-label="Close">×</button></div>
  <div class="sy-log" id="sylog" aria-live="polite">
    <div><div class="who sy">Sy</div>
    <div class="msg">{html.escape(first)}</div></div></div>
  <div class="sy-foot">{foot}</div>
</aside>"""
        # Plain string, not an f-string: JS braces stay readable. The chat
        # id lives in sessionStorage, so each browser tab talks in its own
        # capped session; the reset button starts that tab's chat over.
        script = """
<script>
const dock=document.getElementById('sydock'),
      launch=document.getElementById('sylaunch');
function syToggle(open){dock.classList.toggle('open',open);
launch.setAttribute('aria-expanded',dock.classList.contains('open'));}
launch.addEventListener('click',()=>syToggle());
document.getElementById('syclose').addEventListener('click',()=>syToggle(false));
let syChat='default';
try{
  syChat=sessionStorage.getItem('syChat');
  if(!syChat){
    syChat=Math.random().toString(36).slice(2)+Date.now().toString(36);
    sessionStorage.setItem('syChat',syChat);
  }
}catch(_){syChat='default'}
const form=document.getElementById('syform');
if(form){
  const log=document.getElementById('sylog'),
        box=document.getElementById('symsg');
  form.addEventListener('submit',async e=>{
    e.preventDefault();
    const t=box.value.trim(); if(!t)return;
    add('you','You',t); box.value=''; box.disabled=true;
    try{
      const r=await fetch('/chat',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message:t,chat:syChat})});
      const d=await r.json();
      add('sy','Sy',d.reply||d.error||'(no reply)');
    }catch(_){add('sy','Sy',
      '(the server did not answer — its terminal will say why)')}
    box.disabled=false; box.focus();
  });
  document.getElementById('syreset').addEventListener('click',async()=>{
    try{
      await fetch('/chat/reset',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({chat:syChat})});
    }catch(_){}
    log.innerHTML=''; add('sy','Sy','(fresh chat)'); box.focus();
  });
  function add(cls,who,text){
    const d=document.createElement('div');
    const w=document.createElement('div');
    w.className='who '+cls; w.textContent=who;
    const m=document.createElement('div');
    m.className='msg'; m.textContent=text;
    d.append(w,m); log.appendChild(d); log.scrollTop=log.scrollHeight;
  }
}
</script>"""
        return shell + script

    # ------------------------------------------------------------- pages

    def dashboard(self) -> str:
        steps = journey.next_steps(self.home)
        if not steps:
            return self.page("A new journey", self._first_run())
        know = journey.knowledge(self.home)
        n = len(steps)
        count_word = {1: "One course", 2: "Two courses",
                      3: "Three courses"}.get(n, f"{n} courses")
        body = ['<p class="eyebrow">Your journey</p>',
                f'<h1>{count_word}, <em>one</em> map</h1>' if n > 1 else
                f'<h1>{count_word}, <em>one</em> path</h1>']
        ready = [s for s in steps if s["status"] == "ready"]
        if ready:
            s = ready[0]
            body.append(
                f'<div class="card next">'
                f'<p class="mini">Next — the one thing to do</p>'
                f'<span class="title">{html.escape(s["title"])}</span>'
                f'<p style="font-size:14px;margin-bottom:16px">'
                f'~{s["estimated_hours"]}h · '
                f'{html.escape(s["course_title"])} · milestone '
                f'{html.escape(s["milestone_id"])}</p>'
                f'<a class="btn" href="/course/{s["course"]}/lesson/'
                f'{s["milestone_id"]}">Open the lesson</a></div>')
        for i, s in enumerate(steps):
            cdir = journey.course_dir(self.home, s["course"])
            manifest = yaml.safe_load((cdir / "course.yaml").read_text())
            total = len(manifest["milestones"])
            passed = sum(journey.milestone_passed(cdir, m["id"])
                         for m in manifest["milestones"])
            pct = round(100 * passed / total) if total else 0
            if s["status"] == "complete":
                status = "complete — every milestone passed"
            elif s["status"] == "blocked":
                status = ("blocked on "
                          + html.escape(", ".join(s["blocked_on"])))
            else:
                status = (f'next: {html.escape(s["milestone_id"])} — '
                          f'{html.escape(s["title"])}')
            body.append(
                f'<a class="card" style="display:block" '
                f'href="/course/{s["course"]}">'
                f'<span style="display:flex;align-items:baseline;gap:0">'
                f'<span class="chip" style="background:{series(i)}"></span>'
                f'<span class="title">{html.escape(s["course_title"])}'
                f'</span><span class="meta" style="margin-left:auto">'
                f'{passed} / {total} milestones</span></span>'
                f'<span class="bar" role="progressbar" aria-valuenow="{pct}"'
                f' aria-valuemin="0" aria-valuemax="100"><span '
                f'style="width:{pct}%;background:{series(i)}"></span></span>'
                f'<span style="font-size:14px;color:var(--ink-2)">{status}'
                f"</span></a>")
        bridges = sum(1 for e in know
                      if len({ev["course"] for ev in e["evidence"]}) > 1)
        bridge_note = (f" · {bridges} bridge{'s' if bridges != 1 else ''} "
                       f"between courses" if bridges else "")
        body.append(
            f'<div class="rowline"><div>'
            f'<div style="font-family:var(--font-display);font-size:19px">'
            f"Knowledge</div>"
            f'<div style="font-size:14px;color:var(--ink-2)">{len(know)} '
            f"verified concept{'s' if len(know) != 1 else ''}{bridge_note}"
            f"</div></div>"
            f'<a class="linkbtn" href="/knowledge">See the map →</a></div>')
        return self.page("Your journey", "".join(body))

    def _first_run(self) -> str:
        points = [
            ("Artifact gravity", "Every milestone ends in something you "
             "build — never “understand X.”"),
            ("Real grading", "A three-tier grader probes the artifact. "
             "Finishing leaves a portfolio."),
            ("One map", "What you prove in one course is assumed in the "
             "next. Curricula connect."),
        ]
        points_html = "".join(
            f'<div><div class="k">{k}</div><div class="v">{v}</div></div>'
            for k, v in points)
        body = f"""
<div id="firstrun">
<p class="eyebrow">A new journey</p>
<h1 class="hero-h">What do you want to <em>learn</em>?</h1>
<p class="lead" style="max-width:560px">Name it in a sentence. sylabis
compiles a course from primary sources, teaches it milestone by milestone,
and grades the real thing you build. The work is yours; the rest is the
agent's.</p>
<form id="learnform" style="display:flex;gap:10px;align-items:stretch;
margin:30px 0 16px">
<label for="topic" style="display:none">Topic</label>
<input type="text" id="topic" name="topic" style="flex:1;font-size:16px;
padding:14px 16px"
 placeholder="e.g. distill a small coding model for my MacBook M4" required>
<button class="btn" style="flex:none">Compile it</button></form>
<div class="points">{points_html}</div>
</div>
<div id="compiling" style="display:none">
<p class="eyebrow hot" style="animation:syPulse 1.4s infinite">Compiling</p>
<h1 id="ctopic"></h1>
<p class="quiet">The compiler reports each stage as it finishes — the list
below is its real progress, and this page moves on by itself when the
course is ready. A refresh is safe: the compile keeps running server-side.</p>
<div id="cstages"><div class="stage"><div class="mark">●</div><div>
<div class="lbl">Starting</div>
<div style="font-size:15px;color:var(--ink-2)">Handing your topic to the
compiler…</div></div></div></div>
<p class="quiet" id="cerror" style="color:var(--aka);margin-top:20px"></p>
</div>"""
        # Plain string, not an f-string: the stage list is rendered from
        # /status/<job>, which reads the compiler's own checkpoint file —
        # the progress shown is real, never an animation.
        script = """
<script>
const lf=document.getElementById('learnform');
function cerr(t){document.getElementById('cerror').textContent=t;}
function renderStages(c){
  if(!c||!Array.isArray(c.stages)||!c.stages.length)return;
  const done=c.done||[],box=document.getElementById('cstages');
  box.innerHTML='';
  for(const s of c.stages){
    const isDone=done.includes(s),isCur=s===c.current;
    const row=document.createElement('div');row.className='stage';
    const mk=document.createElement('div');mk.className='mark';
    mk.textContent=isDone?'✓':'●';
    mk.style.color=isDone?'var(--matcha)':(isCur?'var(--aka)':'var(--line)');
    if(!isCur)mk.style.animation='none';
    const lbl=document.createElement('div');lbl.className='lbl';
    lbl.style.marginBottom='0';lbl.textContent=s.replace(/_/g,' ');
    const cell=document.createElement('div');cell.appendChild(lbl);
    row.append(mk,cell);box.appendChild(row);
  }
}
async function poll(job){
  try{
    const r=await fetch('/status/'+job);
    const d=await r.json();
    renderStages(d.compile);
    if(d.state==='done'){location.href='/';return;}
    if(d.state==='error'){cerr(d.error||'Compile failed.');return;}
  }catch(_){/* transient — keep polling */}
  setTimeout(()=>poll(job),1000);
}
lf.addEventListener('submit',async e=>{
  e.preventDefault();
  const topic=document.getElementById('topic').value.trim();
  if(!topic)return;
  document.getElementById('firstrun').style.display='none';
  document.getElementById('ctopic').textContent=topic;
  document.getElementById('compiling').style.display='block';
  try{
    const r=await fetch('/learn',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({topic})});
    const d=await r.json();
    if(d.ok&&d.job){poll(d.job);return;}
    cerr(d.error||'Compile failed.');
  }catch(_){cerr('The server did not answer — check its terminal.');}
});
</script>"""
        return body + script

    def learn(self, payload: dict) -> dict:
        """Enqueue a compile on a worker thread and return a job id the
        page polls via /status/<job>. The request never blocks for the
        minute a real compile takes."""
        topic = payload.get("topic")
        topic = topic.strip() if isinstance(topic, str) else ""
        if not topic:
            raise ToolError("Say what you want to learn first.")
        if len(topic) > 500:
            raise ToolError("Keep the topic under 500 characters.")
        with self.lock:  # claim the course dir name atomically
            out_dir = journey.new_course_dir(self.home, topic)
            out_dir.mkdir(parents=True)
        job_id = secrets.token_hex(8)
        self.jobs[job_id] = {"state": "queued", "topic": topic,
                             "course_dir": out_dir, "course": None,
                             "error": None}
        threading.Thread(target=self._compile_job,
                         args=(job_id, topic, out_dir), daemon=True).start()
        return {"ok": True, "job": job_id}

    def _compile_job(self, job_id: str, topic: str, out_dir: Path) -> None:
        job = self.jobs[job_id]
        job["state"] = "running"
        try:
            from .compiler import compile_course
            profile = {"weekly_hours": 5, "hardware": "",
                       "prior_knowledge": journey.prior_knowledge(self.home)}
            compile_course(topic, profile, out_dir, self.tools._llm())
            with self.lock:  # journey-state mutation only
                journey.emit_map(self.home)
            job["course"] = out_dir.name
            job["state"] = "done"
        except BaseException as e:  # SystemExit / SylabisError / anything
            if not (out_dir / "course.yaml").exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            job["error"] = str(e) or e.__class__.__name__
            job["state"] = "error"

    def job_status(self, job_id: str) -> dict:
        """Real progress for one compile job. The `compile` field is the
        compiler's own <course_dir>/.compile/status.json (schema 1:
        stages/done/current/error); it is tolerated missing — the writer
        may not have started yet."""
        job = self.jobs.get(job_id)
        if job is None:
            raise ToolError("No such compile job.")
        compile_status = None
        status_path = job["course_dir"] / ".compile" / "status.json"
        try:
            compile_status = json.loads(status_path.read_text())
        except (OSError, ValueError):
            pass
        return {"ok": True, "state": job["state"], "course": job["course"],
                "error": job["error"], "compile": compile_status}

    def knowledge_page(self) -> str:
        steps = journey.next_steps(self.home)
        know = journey.knowledge(self.home)
        body = ['<p class="eyebrow">Connected curriculum</p>',
                "<h1>Knowledge map</h1>"]
        svg = knowledge_svg(steps, know)
        if not svg:
            body.append('<p class="quiet">Nothing verified yet — pass a '
                        "milestone and the map begins.</p>")
        else:
            body.append(f'<div class="card" style="padding:18px 16px">'
                        f"{svg}</div>")
            body.append("<h2>Verified concepts</h2><table>")
            for e in know:
                refs = "<br>".join(
                    (f'<a href="/course/{ev["course"]}/doc?p=portfolio/claims/'
                     f'{ev["milestone_id"]}.md">{html.escape(ev["course_title"])}'
                     f' · {html.escape(ev["milestone_id"])}</a>'
                     if ev.get("has_claim")
                     else f'{html.escape(ev["course_title"])} · '
                          f'{html.escape(ev["milestone_id"])} (unscored)')
                    for ev in e["evidence"])
                body.append(f"<tr><td><strong>{html.escape(e['concept'])}"
                            f"</strong></td><td>{refs}</td></tr>")
            body.append("</table>")
            bridges = [e for e in know
                       if len({ev["course"] for ev in e["evidence"]}) > 1]
            if bridges:
                body.append("<h2>Connections</h2>"
                            "<p>Concepts proven in more than one course. "
                            "These are what let the next course build on "
                            "what the last one proved, instead of "
                            "re-teaching it.</p><ul>")
                for e in bridges:
                    names = " and ".join(
                        f"<em>{html.escape(nm)}</em>"
                        for nm in _bridge_names(e))
                    body.append(f"<li style='color:var(--sumi)'><strong>"
                                f"{html.escape(e['concept'])}</strong> "
                                f"links {names}</li>")
                body.append("</ul>")
        return self.page("Knowledge map", "".join(body))

    def course_page(self, name: str) -> str:
        cdir = self._cdir(name)
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        body = ['<a class="navlink quiet" href="/" style="font-size:10px">'
                "← journey</a>",
                f'<h1 style="margin-top:16px">'
                f"{html.escape(manifest['meta']['title'])}</h1>"]
        target = (manifest.get("target_artifact")  # published templates
                  or manifest.get("learner", {}).get("target_artifact", ""))
        if target:
            body.append(f'<p class="lead">{html.escape(target)}</p>')
        for m in manifest["milestones"]:
            gpath = cdir / m["id"] / "grade.yaml"
            if journey.milestone_passed(cdir, m["id"]):
                g = yaml.safe_load(gpath.read_text()) or {}
                status = (f'<span class="tag pass">✓ passed · '
                          f'{okf.grade_token(g)}</span>')
            elif gpath.exists():
                g = yaml.safe_load(gpath.read_text()) or {}
                status = (f'<span class="tag fail">attempt '
                          f'{g.get("attempt", 1)} · not yet</span>')
            else:
                status = '<span class="meta">not started</span>'
            body.append(
                f'<a class="card" style="display:block" '
                f'href="/course/{name}/lesson/{m["id"]}">'
                f'<span style="display:flex;align-items:baseline;gap:10px">'
                f'<span class="title">{html.escape(m["id"])} — '
                f'{html.escape(m["title"])}</span>'
                f'<span class="meta" style="margin-left:auto;flex:none">'
                f'~{m["estimated_hours"]}h</span></span>'
                f'<span style="display:block;margin-top:8px">{status}'
                f"</span></a>")
        body.append(
            f'<div class="rowline">'
            f'<a class="linkbtn" href="/course/{name}/doc?p=knowledge/'
            f'index.md">Sources</a>'
            f'<a class="linkbtn" href="/course/{name}/doc?p=portfolio/'
            f'index.md">Portfolio</a></div>')
        return self.page(manifest["meta"]["title"], "".join(body))

    def lesson_page(self, name: str, mid: str) -> str:
        cdir = self._cdir(name)
        lesson = self.tools.call("get_lesson",
                                 {"course": name, "milestone_id": mid})
        _, body_md = _split_frontmatter(lesson)
        cp_path = cdir / mid / "checkpoint.yaml"
        cp = yaml.safe_load(cp_path.read_text()) if cp_path.exists() else {}
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        m = next((x for x in manifest["milestones"] if x["id"] == mid), {})

        head = [f'<a class="navlink quiet" href="/course/{name}" '
                'style="font-size:10px">← course</a>',
                f'<p class="eyebrow" style="margin-top:16px;'
                f'letter-spacing:.1em">{html.escape(mid)} · '
                f'~{m.get("estimated_hours", "?")}h</p>']
        if cp.get("artifact_spec"):
            head.append(
                f'<div class="capsule"><p class="mini ok">The artifact</p>'
                f'<div class="body">{html.escape(cp["artifact_spec"])}'
                f"</div></div>")
        reflection_hint = "Your reflection, in your own words."
        if cp.get("misconception_target"):
            reflection_hint = ("Target the misconception: "
                               + cp["misconception_target"])
        form = f"""
<h2>Submit your work</h2>
<p class="quiet" style="font-size:14px">Your own words — the grader probes
understanding, not polish.</p>
<form method="post" action="/course/{name}/submit/{mid}">
<input type="hidden" name="csrf" value="{html.escape(self.csrf, quote=True)}">
<label for="artifact">Artifact</label>
<textarea id="artifact" name="artifact" required
 placeholder="Paste your artifact…"></textarea>
<label for="reflection">Reflection</label>
<textarea id="reflection" name="reflection" required style="min-height:90px"
 placeholder="{html.escape(reflection_hint, quote=True)}"></textarea>
<label for="hours">Hours spent (optional)</label>
<input id="hours" name="hours" type="number" step="0.5" min="0"
 style="margin-bottom:18px">
<button class="btn" style="padding:13px 28px">Grade it</button></form>"""
        return self.page(mid, "".join(head)
                         + md_to_html(body_md, self._linker(name, mid))
                         + form)

    def submit(self, name: str, mid: str, form: dict) -> str:
        args = {"course": name, "milestone_id": mid,
                "artifact": form.get("artifact", [""])[0],
                "reflection": form.get("reflection", [""])[0]}
        hours = (form.get("hours", [""])[0] or "").strip()
        if hours:
            try:
                h = float(hours)
            except ValueError:
                raise ToolError("Hours must be a number, e.g. 2 or 2.5.")
            if not math.isfinite(h) or not 0 <= h <= 10000:
                raise ToolError("Hours must be between 0 and 10000.")
            args["hours_actual"] = h
        with self.lock:  # journey-state mutation: grade, path engine, map
            feedback = self.tools.call("submit_work", args)
        cdir = self._cdir(name)
        gy = yaml.safe_load((cdir / mid / "grade.yaml").read_text()) or {}
        cp = yaml.safe_load((cdir / mid / "checkpoint.yaml").read_text()) or {}
        passed = bool(gy.get("passed"))
        grade_pct = okf.grade_token(gy)

        if passed:
            head = (f'<div style="display:flex;align-items:flex-end;'
                    f'gap:14px;margin-bottom:6px">'
                    f'<h1 class="hero-h" style="color:var(--matcha);'
                    f'margin:0;line-height:1">✓ Passed</h1>'
                    f'<span style="font-family:var(--font-mono);'
                    f'font-size:22px;padding-bottom:5px">{grade_pct}</span>'
                    f"</div><p style='font-size:16px'>Verified and on the "
                    f"map. The claim, the artifact, and the grade travel "
                    f"together now.</p>")
        else:
            head = (f'<div style="display:flex;align-items:flex-end;'
                    f'gap:14px;margin-bottom:6px">'
                    f'<h1 class="hero-h" style="color:var(--aka);margin:0;'
                    f'line-height:1">Not yet</h1>'
                    f'<span style="font-family:var(--font-mono);'
                    f'font-size:22px;padding-bottom:5px">{grade_pct}</span>'
                    f"</div><p style='font-size:16px'>A path signal, not a "
                    f"verdict — the tiers below say exactly what to fix.</p>")

        cards = "".join(self._tier_cards(gy))
        signal = self._path_signal(feedback)
        chips = ""
        if passed and cp.get("core_concepts"):
            chips = ('<div style="margin:0 0 26px">'
                     '<span class="mini quiet" style="margin-right:10px;'
                     'display:inline-block">Now verified</span>'
                     + "".join(f'<span class="concept-tag">+ '
                               f"{html.escape(c)}</span>"
                               for c in cp["core_concepts"]) + "</div>")
        actions = (
            f'<div style="display:flex;gap:12px;border-top:1px solid '
            f'var(--line);padding-top:20px">'
            f'<a class="btn outline" href="/">Back to journey</a>'
            + (f'<a class="btn ghost" href="/knowledge">See it on the map '
               f"→</a>" if passed else
               f'<a class="btn ghost" href="/course/{name}/lesson/{mid}">'
               f"Back to the lesson</a>")
            + "</div>")
        raw = (f"<details><summary>Full grader output</summary>"
               f"<pre>{html.escape(feedback)}</pre></details>")
        return self.page(f"Graded — {mid}",
                         head + cards + signal + chips + raw + actions)

    def _tier_cards(self, gy: dict) -> list[str]:
        """The grade, told the way the grader actually works: tier by tier,
        from the structured grade.yaml the grader just wrote."""
        def card(label: str, ok: bool, verdict: str, detail: str) -> str:
            return (f'<div class="card"><span style="display:flex;'
                    f'align-items:baseline;gap:10px;margin-bottom:5px">'
                    f'<span style="font-family:var(--font-mono);'
                    f'font-size:11px;letter-spacing:.13em;'
                    f'text-transform:uppercase">{label}</span>'
                    f'<span class="tag {"pass" if ok else "fail"}" '
                    f'style="margin-left:auto">{html.escape(verdict)}</span>'
                    f'</span><div style="font-size:14px;line-height:1.55;'
                    f'color:var(--ink-2)">{html.escape(detail)}</div></div>')

        cards = []
        t1 = bool(gy.get("tier_1_passed"))
        missing = [f.split(":", 1)[1] for f in gy.get("failure_flags", [])
                   if f.startswith("missing_file:")]
        cards.append(card("Tier 1 — structural", t1,
                          "pass" if t1 else "fail",
                          "Required files present and readable."
                          if t1 else "Missing: " + ", ".join(missing)))
        if not t1:
            return cards
        audit = gy.get("claim_audit")
        if audit:
            ok = bool(gy.get("tier_2_passed"))
            flags = audit.get("flags", [])
            verdict = "pass" if ok else "fail"
            if ok and flags:
                verdict = f"pass · {len(flags)} flag{'s' if len(flags) != 1 else ''}"
            cards.append(card(
                "Tier 2 — claim audit", ok, verdict,
                f"{audit.get('passed', 0)} of {audit.get('total', 0)} claims "
                f"matched their evidence."
                + (f" Flags: {', '.join(flags)}." if flags else "")))
            if not ok:
                return cards
        mode = gy.get("tier_3_mode")
        if mode == "rubric_scripts":
            notes = "; ".join(gy.get("rubric_scripts", []))
            cards.append(card("Tier 3 — rubric scripts", True, "ran",
                              notes or "Executable rubric scored the artifact."))
        elif mode == "exemplar_rubric":
            t3 = gy.get("tier_3", {})
            cards.append(card("Tier 3 — exemplar rubric", True,
                              f"{t3.get('overall', 0):.0%}",
                              t3.get("feedback", "")))
        for p in gy.get("explain_back", []):
            ok = p.get("verdict") == "understood"
            detail = (p.get("followup_question")
                      or "Names the boundary before we could ask about it.")
            cards.append(card(f"Explain-back — {p.get('concept', '')[:40]}",
                              ok, p.get("verdict", ""), detail))
        return cards

    def _path_signal(self, feedback: str) -> str:
        """Path decisions from the grader's own narration, rendered as the
        Reading Room's signal capsule."""
        lines = [l.strip() for l in feedback.splitlines()]
        unlocked = next((l for l in lines
                         if l.startswith("unlocked sidequest")), None)
        remedial = next((l for l in lines
                         if l.startswith("injected remedial")), None)
        if unlocked:
            title = unlocked.split("—", 1)[-1].strip() or unlocked
            return (f'<div class="signal up"><p class="mini ok">↑ Path '
                    f"signal — sidequest unlocked</p>"
                    f'<div class="title">{html.escape(title)}</div>'
                    f'<div style="font-size:14px;color:var(--ink-2)">You '
                    f"beat the bar with room to spare, so the path opened "
                    f"a depth sidequest. Take it now or let it wait — it "
                    f"stays on your journey either way.</div></div>")
        if remedial:
            return (f'<div class="signal flat"><p class="mini">→ Path '
                    f"signal — remedial injected</p>"
                    f'<div class="title">A short detour first</div>'
                    f'<div style="font-size:14px;color:var(--ink-2)">'
                    f"{html.escape(remedial)}. It appears before this "
                    f"milestone and unblocks it.</div></div>")
        return ""

    def doc_page(self, name: str, rel: str) -> str:
        cdir = self._cdir(name)
        path = (cdir / rel).resolve()
        if not path.is_relative_to(cdir.resolve()) or \
                path.suffix not in (".md", ".yaml", ".jsonl") or \
                not path.exists():
            raise ToolError(f"No document {rel!r} in this course.")
        text = path.read_text()
        back = (f'<a class="navlink quiet" href="/course/{name}" '
                'style="font-size:10px">← course</a>')
        if path.suffix != ".md":
            return self.page(rel, f"{back}<h1 style='margin-top:16px'>"
                                  f"{html.escape(rel)}</h1>"
                                  f"<pre>{html.escape(text)}</pre>")
        _, body_md = _split_frontmatter(text)
        return self.page(rel, back + md_to_html(body_md,
                                                self._linker(name, rel)))

    # -------------------------------------------------------------- chat

    def chat_enabled(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY")) and not self.mock

    def chat_reply(self, message: str, chat_id: str = "default") -> str:
        """One turn in the per-tab chat session `chat_id`. Sessions are
        capped in length (reset to continue) and in count (oldest tab's
        history is dropped first)."""
        from .agent import Agent
        from .console import Console
        if not _CHAT_ID.match(chat_id or ""):
            raise ToolError("Bad chat id.")
        with self._chat_lock:
            msgs = self._chats.get(chat_id)
            if msgs is not None and len(msgs) >= CHAT_MESSAGE_CAP:
                raise ToolError("This chat hit its length cap — reset it "
                                "(the ↺ in the dock) to keep talking.")
            if self._agent is None:  # same loop as the terminal, silenced
                self._agent = Agent(self.home, console=Console(enabled=False))
            if msgs is None:
                if len(self._chats) >= CHAT_SESSION_CAP:
                    self._chats.pop(next(iter(self._chats)))
                msgs = self._chats[chat_id] = []
                message = f"(new session — orient first)\n{message}"
            msgs.append({"role": "user", "content": message})
            return self._agent.turn(msgs)

    def chat_reset(self, chat_id: str = "default") -> dict:
        if not _CHAT_ID.match(chat_id or ""):
            raise ToolError("Bad chat id.")
        with self._chat_lock:
            self._chats.pop(chat_id, None)
        return {"ok": True}

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
    ("GET", re.compile(r"^/status/([A-Za-z0-9_-]{1,64})$"), "status"),
    ("GET", re.compile(r"^/course/([A-Za-z0-9._-]+)$"), "course_page"),
    ("GET", re.compile(r"^/course/([A-Za-z0-9._-]+)/lesson/"
                       r"([A-Za-z0-9._-]+)$"), "lesson_page"),
    ("POST", re.compile(r"^/course/([A-Za-z0-9._-]+)/submit/"
                        r"([A-Za-z0-9._-]+)$"), "submit"),
    ("GET", re.compile(r"^/course/([A-Za-z0-9._-]+)/doc$"), "doc_page"),
    ("POST", re.compile(r"^/learn$"), "learn"),
    ("POST", re.compile(r"^/chat$"), "chat"),
    ("POST", re.compile(r"^/chat/reset$"), "chat_reset"),
]

# Routes whose clients speak JSON — errors go back as JSON, never as an
# HTML page a fetch() would choke on ("connection lost" was a lie).
_JSON_ACTIONS = {"learn", "chat", "chat_reset", "status"}
_ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _host_ok(host: str) -> bool:
    """Host-header allowlist — the DNS-rebinding defense. Binding
    127.0.0.1 does not stop a hostile page resolving its own domain to
    127.0.0.1; only rejecting foreign Host values does."""
    host = (host or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):                # [::1] or [::1]:port
        host = host.partition("]")[0].lstrip("[")
    elif host.count(":") == 1:              # name:port
        host = host.rsplit(":", 1)[0]
    return host in _ALLOWED_HOSTNAMES


def _origin_ok(origin: str) -> bool:
    """A present Origin must be this server. Absent Origin passes — the
    request already carried a valid token or cookie to get this far, and
    browsers always send Origin on cross-origin POSTs."""
    try:
        p = urlparse(origin)
    except ValueError:
        return False
    return (p.scheme in ("http", "https")
            and (p.hostname or "").lower() in _ALLOWED_HOSTNAMES)


def _json_body(body: bytes) -> dict:
    try:
        data = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, ValueError):
        raise ToolError("The request body must be JSON.")
    if not isinstance(data, dict):
        raise ToolError("The request body must be a JSON object.")
    return data


class _BodyTooLarge(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "sylabis"

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        app: WebApp = self.server.app  # type: ignore[attr-defined]
        self._set_cookie = None
        url = urlparse(self.path)
        query = parse_qs(url.query)

        action, m = None, None
        for verb, pat, act in _ROUTES:
            mm = pat.match(url.path)
            if mm and verb == method:
                action, m = act, mm
                break
        as_json = action in _JSON_ACTIONS

        # Order matters: Host first (rebinding), then token-or-cookie,
        # then Origin (cross-site forgery), then CSRF inside form routes.
        if not _host_ok(self.headers.get("Host", "")):
            return self._deny(403, "Refused: unexpected Host header.", as_json)

        if app.token_ok(query.get("token", [""])[0]):
            # a valid token exchanges for the session cookie
            self._set_cookie = app.session_cookie()
        elif not app.cookie_ok(self.headers.get("Cookie", "")):
            return self._deny(403, "Authentication required — open the "
                                   "tokenized URL printed in the terminal.",
                              as_json)

        if method == "POST":
            origin = self.headers.get("Origin")
            if origin and not _origin_ok(origin):
                return self._deny(403, "Cross-origin request refused.",
                                  as_json)

        if action is None:
            return self._html(app.page("Not found", "<h1>No such page.</h1>"
                              "<p><a href='/'>Back to the journey</a></p>"),
                              status=404)

        try:
            body = self._body(app.max_body)
        except _BodyTooLarge:
            return self._deny(413, "Request body too large.", as_json)
        except ValueError:
            return self._deny(400, "Malformed request.", as_json)

        try:
            if action == "submit":
                form = parse_qs(body.decode(errors="replace"))
                if not app.csrf_ok(form.get("csrf", [""])[0]):
                    return self._deny(403, "Missing or stale form token — "
                                           "reload the page and resubmit.",
                                      False)
                return self._html(app.submit(*m.groups(), form))
            if action == "learn":
                return self._json(200, app.learn(_json_body(body)))
            if action == "status":
                return self._json(200, app.job_status(m.group(1)))
            if action == "chat":
                if not app.chat_enabled():
                    return self._json(503, {"error": "chat needs "
                                            "ANTHROPIC_API_KEY"})
                payload = _json_body(body)
                reply = app.chat_reply(str(payload.get("message") or ""),
                                       str(payload.get("chat") or "default"))
                return self._json(200, {"reply": reply})
            if action == "chat_reset":
                payload = _json_body(body)
                return self._json(200, app.chat_reset(
                    str(payload.get("chat") or "default")))
            if action == "doc_page":
                rel = query.get("p", [""])[0]
                return self._html(app.doc_page(m.group(1), rel))
            return self._html(getattr(app, action)(*m.groups()))
        except ToolError as e:
            if as_json:
                return self._json(404 if action == "status" else 400,
                                  {"error": str(e)})
            return self._html(app.page("Not found",
                                       f"<h1>Hmm.</h1><p>{html.escape(str(e))}"
                                       f"</p><p><a href='/'>Journey</a></p>"),
                              status=400 if action == "submit" else 404)
        except Exception:  # a page bug renders generically; details stay
            traceback.print_exc(file=sys.stderr)  # server-side only
            if as_json:
                return self._json(500, {"error": "Something broke on the "
                                        "server — its terminal has the "
                                        "details."})
            return self._html(app.page("Error", "<h1>Something broke.</h1>"
                              "<p>The server hit an internal error; the "
                              "terminal it runs in has the details.</p>"),
                              status=500)

    def _deny(self, status: int, message: str, as_json: bool) -> None:
        if as_json:
            return self._json(status, {"error": message})
        app: WebApp = self.server.app  # type: ignore[attr-defined]
        return self._html(app.page("Refused",
                                   f"<h1>Refused.</h1>"
                                   f"<p>{html.escape(message)}</p>"),
                          status=status)

    def _body(self, cap: int) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length < 0:
            raise ValueError("negative Content-Length")
        if length > cap:
            raise _BodyTooLarge
        return self.rfile.read(length)

    def _send(self, status: int, ctype: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'")
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
        self.end_headers()
        self.wfile.write(data)

    def _html(self, text: str, status: int = 200) -> None:
        self._send(status, "text/html; charset=utf-8", text.encode())

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, "application/json", json.dumps(obj).encode())

    def log_message(self, fmt, *args):  # quiet: one line per request on stderr
        print(f"[sylabis.web] {self.address_string()} {fmt % args}",
              file=sys.stderr)


def make_server(home_dir: Path, port: int = 8787, mock: bool = False,
                token: str | None = None) -> ThreadingHTTPServer:
    """token=None generates a fresh one; passing it is the test hook
    (or read server.app.token / server.app.csrf after construction)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.app = WebApp(home_dir, mock=mock, token=token)  # type: ignore[attr-defined]
    return server


def serve(home_dir: Path, port: int = 8787, mock: bool = False) -> None:
    server = make_server(home_dir, port=port, mock=mock)
    app: WebApp = server.app  # type: ignore[attr-defined]
    host, actual_port = server.server_address[:2]
    url = f"http://{host}:{actual_port}/?token={app.token}"
    print("sylabis — your reading room is at  (Ctrl-C to stop)")
    print(f"  {url}")
    print("  (the ?token=… is this session's key; your browser trades it "
          "for a cookie)")
    if not app.chat_enabled():
        print("  (Sy is off — set ANTHROPIC_API_KEY to turn the guide on)")
    if not os.environ.get("SYLABIS_NO_BROWSER"):
        try:
            import webbrowser
            threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
