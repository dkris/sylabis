"""
WS1c — local web app security + UX (Primetime plan, Phase 1, section 1c).
Adversarial, offline tests over real HTTP transport, same pattern as the
web tests in run_all.py: a ThreadingHTTPServer on an ephemeral port, raw
http.client requests, mock LLM fixtures only.

Covered: token/cookie auth, Host-header rejection (DNS rebinding),
Origin checks on JSON POSTs, CSRF on the submit form, body-size cap,
malformed bodies, form-numeric validation, generic 500 pages with no
str(e) leaks, background compile + /status/<job> against a fabricated
.compile/status.json (the shared contract), and the chat routes.
"""
import http.client
import io
import json
import os
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import urlencode

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from sylabis import journey  # noqa: E402
from sylabis.compiler import compile_course  # noqa: E402
from sylabis.llm import LLM  # noqa: E402
from sylabis.web import CHAT_MESSAGE_CAP, make_server  # noqa: E402

TOKEN = "ws1c-test-token"


def _home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    return home


def _compile_into(home: Path, name: str) -> Path:
    out = home / "courses" / name
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, out,
                       LLM(mock=True))
    return out


def _start(home: Path, mock: bool = True):
    server = make_server(home, port=0, mock=mock, token=TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _req(port, method, path, body=None, headers=None, token=TOKEN):
    """One raw request. token=None sends no credential at all; a custom
    Host/Origin/Cookie goes in headers. Returns (status, text, headers)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    if token is not None:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}token={token}"
    conn.request(method, path, body=body, headers=dict(headers or {}))
    resp = conn.getresponse()
    text = resp.read().decode()
    hdrs = dict(resp.getheaders())
    conn.close()
    return resp.status, text, hdrs


def _wait_job(port, job, deadline_s=30):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        _, out, _ = _req(port, "GET", f"/status/{job}")
        d = json.loads(out)
        if d["state"] in ("done", "error"):
            return d
        time.sleep(0.05)
    raise AssertionError("compile job never finished")


# ---------------------------------------------------------------- auth

def test_ws1c_token_or_cookie_required_everywhere(tmp):
    home = _home(tmp)
    server, port = _start(home)
    try:
        # no credential at all -> 403, HTML and JSON alike
        status, page, _ = _req(port, "GET", "/", token=None)
        assert status == 403 and "Refused" in page
        status, out, _ = _req(port, "POST", "/learn",
                              json.dumps({"topic": "x"}),
                              {"Content-Type": "application/json"},
                              token=None)
        assert status == 403 and "error" in json.loads(out), \
            "tokenless /learn must fail closed with a JSON error"
        status, out, _ = _req(port, "POST", "/chat", json.dumps({}),
                              {"Content-Type": "application/json"},
                              token=None)
        assert status == 403, "tokenless /chat must fail closed"
        status, _, _ = _req(port, "POST", "/course/x/submit/y",
                            "artifact=a", token=None)
        assert status == 403, "tokenless submit must fail closed"

        # wrong token -> 403
        status, _, _ = _req(port, "GET", "/", token="wrong-token")
        assert status == 403

        # right token -> 200, and it exchanges for the session cookie
        status, page, hdrs = _req(port, "GET", "/")
        assert status == 200 and "What do you want to" in page
        cookie = hdrs.get("Set-Cookie", "")
        assert cookie.startswith("sylabis_session=") and "HttpOnly" in cookie

        # the cookie alone (no token) now authenticates
        cookie_val = cookie.split(";", 1)[0]
        status, page, _ = _req(port, "GET", "/", headers={"Cookie": cookie_val},
                               token=None)
        assert status == 200, "session cookie must authenticate on its own"
        # a forged cookie must not
        status, _, _ = _req(port, "GET", "/",
                            headers={"Cookie": "sylabis_session=forged"},
                            token=None)
        assert status == 403
    finally:
        server.shutdown()


def test_ws1c_host_header_rejected(tmp):
    home = _home(tmp)
    server, port = _start(home)
    try:
        # DNS rebinding: attacker's domain resolves to 127.0.0.1, but the
        # browser still sends the attacker's Host header.
        for host in ("evil.example", "evil.example:80", f"evil.example:{port}"):
            status, _, _ = _req(port, "GET", "/", headers={"Host": host})
            assert status == 403, f"Host {host!r} must be rejected"
        status, _, _ = _req(port, "POST", "/learn",
                            json.dumps({"topic": "x"}),
                            {"Content-Type": "application/json",
                             "Host": "evil.example"})
        assert status == 403, "wrong-Host POST /learn must be rejected"
        # legitimate spellings pass
        for host in (f"127.0.0.1:{port}", "localhost", f"localhost:{port}"):
            status, _, _ = _req(port, "GET", "/", headers={"Host": host})
            assert status == 200, f"Host {host!r} must be accepted"
    finally:
        server.shutdown()


def test_ws1c_cross_origin_posts_refused(tmp):
    home = _home(tmp)
    _compile_into(home, "survey")
    server, port = _start(home)
    try:
        evil = {"Origin": "http://evil.example"}
        # even WITH a valid credential, a cross-origin POST is refused
        status, out, _ = _req(port, "POST", "/learn",
                              json.dumps({"topic": "x"}),
                              {"Content-Type": "application/json", **evil})
        assert status == 403 and "error" in json.loads(out)
        status, _, _ = _req(port, "POST", "/chat",
                            json.dumps({"message": "hi"}),
                            {"Content-Type": "application/json", **evil})
        assert status == 403
        status, _, _ = _req(port, "POST", "/course/survey/submit/00-data-audit",
                            urlencode({"artifact": "a", "reflection": "r",
                                       "csrf": server.app.csrf}),
                            {"Content-Type": "application/x-www-form-urlencoded",
                             **evil})
        assert status == 403
        # and the realistic forgery: no credential at all + foreign Origin
        status, _, _ = _req(port, "POST", "/learn",
                            json.dumps({"topic": "x"}),
                            {"Content-Type": "application/json", **evil},
                            token=None)
        assert status == 403
        # same-origin Origin passes (learn accepts and enqueues)
        status, out, _ = _req(port, "POST", "/learn",
                              json.dumps({"topic": "Survey synthesis"}),
                              {"Content-Type": "application/json",
                               "Origin": f"http://127.0.0.1:{port}"})
        assert status == 200 and json.loads(out)["ok"]
        _wait_job(port, json.loads(out)["job"])
    finally:
        server.shutdown()


def test_ws1c_csrf_required_on_submit_form(tmp):
    home = _home(tmp)
    _compile_into(home, "survey")
    server, port = _start(home)
    try:
        form = {"artifact": "a", "reflection": "r"}
        status, _, _ = _req(port, "POST", "/course/survey/submit/00-data-audit",
                            urlencode(form),
                            {"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 403, "form POST without CSRF token must be refused"
        status, _, _ = _req(port, "POST", "/course/survey/submit/00-data-audit",
                            urlencode({**form, "csrf": "wrong"}),
                            {"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 403, "wrong CSRF token must be refused"
        # the lesson page carries the real token in the form
        status, page, _ = _req(port, "GET",
                               "/course/survey/lesson/00-data-audit")
        assert status == 200 and server.app.csrf in page
    finally:
        server.shutdown()


# ---------------------------------------------------- hardening sweep

def test_ws1c_body_cap_and_malformed_bodies(tmp):
    home = _home(tmp)
    server, port = _start(home)
    server.app.max_body = 4096  # documented test hook
    try:
        big = json.dumps({"topic": "x" * 8000})
        status, out, _ = _req(port, "POST", "/learn", big,
                              {"Content-Type": "application/json"})
        assert status == 413 and "error" in json.loads(out), \
            "oversized body must be rejected with a JSON error"

        status, out, _ = _req(port, "POST", "/learn", "this is not json",
                              {"Content-Type": "application/json"})
        assert status == 400 and "error" in json.loads(out)

        status, out, _ = _req(port, "POST", "/learn", "[1, 2, 3]",
                              {"Content-Type": "application/json"})
        assert status == 400, "a non-object JSON body is malformed"

        status, out, _ = _req(port, "POST", "/learn", b"\xff\xfe garbage",
                              {"Content-Type": "application/json"})
        assert status == 400, "undecodable bytes are malformed, not a 500"

        status, out, _ = _req(port, "POST", "/learn", json.dumps({}),
                              {"Content-Type": "application/json"})
        assert status == 400, "a missing topic is refused"

        status, out, _ = _req(port, "POST", "/learn",
                              json.dumps({"topic": "x" * 600}),
                              {"Content-Type": "application/json"})
        assert status == 400, "an absurdly long topic is refused"
    finally:
        server.shutdown()


def test_ws1c_form_numerics_validated(tmp):
    home = _home(tmp)
    _compile_into(home, "survey")
    server, port = _start(home)
    try:
        def submit_hours(hours):
            return _req(port, "POST", "/course/survey/submit/00-data-audit",
                        urlencode({"artifact": "a", "reflection": "r",
                                   "hours": hours,
                                   "csrf": server.app.csrf}),
                        {"Content-Type":
                         "application/x-www-form-urlencoded"})[0]

        for bad in ("abc", "nan", "inf", "-3", "99999999"):
            status = submit_hours(bad)
            assert status == 400, f"hours={bad!r} must 400, got {status}"
    finally:
        server.shutdown()


def test_ws1c_generic_500_no_leaks(tmp):
    home = _home(tmp)
    server, port = _start(home)
    try:
        marker = "secret-internal-path-string"

        def boom():
            raise RuntimeError(marker)

        server.app.knowledge_page = boom  # instance attr shadows the method
        status, page, _ = _req(port, "GET", "/knowledge")
        assert status == 500
        assert marker not in page and "RuntimeError" not in page, \
            "500 pages must not leak exception internals"

        server.app.learn = lambda payload: boom()
        status, out, _ = _req(port, "POST", "/learn",
                              json.dumps({"topic": "x"}),
                              {"Content-Type": "application/json"})
        assert status == 500
        d = json.loads(out)  # JSON route errors stay JSON
        assert marker not in d.get("error", "")
    finally:
        server.shutdown()


def test_ws1c_security_headers_and_no_font_beacon(tmp):
    home = _home(tmp)
    server, port = _start(home)
    try:
        status, page, hdrs = _req(port, "GET", "/")
        assert status == 200
        assert hdrs.get("X-Content-Type-Options") == "nosniff"
        assert "default-src 'none'" in hdrs.get("Content-Security-Policy", "")
        assert hdrs.get("Referrer-Policy") == "no-referrer", \
            "the tokenized URL must never leak via Referer"
        assert "fonts.googleapis.com" not in page and "@import" not in page, \
            "a page view must not beacon a third party"
    finally:
        server.shutdown()


# -------------------------------------- background compile + /status

def test_ws1c_background_compile_with_status(tmp):
    home = _home(tmp)
    server, port = _start(home)
    try:
        status, out, _ = _req(port, "POST", "/learn",
                              json.dumps({"topic": "Survey synthesis"}),
                              {"Content-Type": "application/json"})
        d = json.loads(out)
        assert status == 200 and d["ok"] and d["job"]
        done = _wait_job(port, d["job"])
        assert done["state"] == "done" and done["course"]
        assert journey.course_dirs(home), "compile must land in the journey"

        status, out, _ = _req(port, "GET", "/status/no-such-job")
        assert status == 404 and "error" in json.loads(out)
    finally:
        server.shutdown()


def test_ws1c_status_reports_real_stage_from_status_json(tmp):
    """The /status route reads <course_dir>/.compile/status.json per the
    shared contract. Fabricated here so the test does not depend on the
    checkpoint writer's code."""
    home = _home(tmp)
    server, port = _start(home)
    try:
        cdir = home / "courses" / "fab"
        (cdir / ".compile").mkdir(parents=True)
        contract = {"schema": 1,
                    "stages": ["intake", "harvest", "sequence",
                               "lesson_00-data-audit", "self_test"],
                    "done": ["intake", "harvest"],
                    "current": "sequence", "error": None}
        (cdir / ".compile" / "status.json").write_text(json.dumps(contract))
        server.app.jobs["fabjob"] = {"state": "running", "topic": "fab",
                                     "course_dir": cdir, "course": None,
                                     "error": None}
        status, out, _ = _req(port, "GET", "/status/fabjob")
        d = json.loads(out)
        assert status == 200 and d["state"] == "running"
        assert d["compile"] == contract, \
            "/status must surface the compiler's own checkpoint file"

        # tolerate the file not existing yet (writer lands concurrently)
        cdir2 = home / "courses" / "fab2"
        cdir2.mkdir(parents=True)
        server.app.jobs["fabjob2"] = {"state": "running", "topic": "fab2",
                                      "course_dir": cdir2, "course": None,
                                      "error": None}
        status, out, _ = _req(port, "GET", "/status/fabjob2")
        d = json.loads(out)
        assert status == 200 and d["compile"] is None
    finally:
        server.shutdown()


# --------------------------------------------------------------- chat

def test_ws1c_chat_routes(tmp):
    home = _home(tmp)
    server, port = _start(home)  # mock=True -> chat disabled
    try:
        status, out, _ = _req(port, "POST", "/chat",
                              json.dumps({"message": "hi", "chat": "t1"}),
                              {"Content-Type": "application/json"})
        assert status == 503 and "ANTHROPIC_API_KEY" in json.loads(out)["error"]
        status, out, _ = _req(port, "POST", "/chat/reset",
                              json.dumps({"chat": "t1"}),
                              {"Content-Type": "application/json"})
        assert status == 200 and json.loads(out) == {"ok": True}
        status, out, _ = _req(port, "POST", "/chat/reset",
                              json.dumps({"chat": "../etc"}),
                              {"Content-Type": "application/json"})
        assert status == 400, "a malformed chat id is refused"
    finally:
        server.shutdown()


def test_ws1c_chat_sessions_per_tab_and_reset(tmp):
    """Full /chat coverage with a stub agent: per-tab histories, the
    message-count cap, and /chat/reset. The env var only flips
    chat_enabled(); the stub means no model call ever happens."""
    home = _home(tmp)
    had_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
    server, port = _start(home, mock=False)

    class _StubAgent:
        def turn(self, msgs):
            msgs.append({"role": "assistant", "content": "stub reply"})
            return "stub reply"

    server.app._agent = _StubAgent()  # pre-set: Agent() is never built
    try:
        def chat(message, chat_id):
            return _req(port, "POST", "/chat",
                        json.dumps({"message": message, "chat": chat_id}),
                        {"Content-Type": "application/json"})

        status, out, _ = chat("hello", "tab1")
        assert status == 200 and json.loads(out)["reply"] == "stub reply"
        status, out, _ = chat("again", "tab1")
        assert status == 200
        chat("other tab", "tab2")

        chats = server.app._chats
        assert set(chats) == {"tab1", "tab2"}, "one history per tab"
        assert len(chats["tab1"]) == 4 and len(chats["tab2"]) == 2
        assert chats["tab1"][0]["content"].startswith("(new session"), \
            "each tab's first message orients the agent"
        assert chats["tab1"][2]["content"] == "again", \
            "later messages are the learner's verbatim"

        # message-count cap: a full chat refuses with a JSON error
        chats["tab1"] = [{"role": "user", "content": "x"}] * CHAT_MESSAGE_CAP
        status, out, _ = chat("one more", "tab1")
        assert status == 400 and "cap" in json.loads(out)["error"]

        # reset clears exactly that tab
        status, out, _ = _req(port, "POST", "/chat/reset",
                              json.dumps({"chat": "tab1"}),
                              {"Content-Type": "application/json"})
        assert status == 200 and json.loads(out) == {"ok": True}
        assert "tab1" not in server.app._chats
        assert "tab2" in server.app._chats, "reset must not touch other tabs"
    finally:
        server.shutdown()
        if had_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = had_key
