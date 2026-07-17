"""
sylabis — the learning agent.

The learner surface, in the order you'll use it:

  sylabis init                 one-time setup: save + check your API key
  sylabis                      talk — the agent drives the whole loop
  sylabis web                  the same journey in your browser
  sylabis learn "topic"        start a course in your journey
  sylabis next                 what to do now, across every course
  sylabis submit               grade the work sitting in your journey
  sylabis journey              progress + the knowledge map
  sylabis publish [COURSE]     put a course on GitHub (grades itself on push)
  sylabis sync [COURSE]        pull CI-graded state down, push local work up
  sylabis share [COURSE]       the links that show your verified work
  sylabis attach SOURCE        connect a course from another repo or path
  sylabis serve [COURSE_DIR]   MCP server (journey-wide without a dir)

No paths, no milestone ids, no flags required: the journey lives in
$SYLABIS_HOME (default ~/sylabis) and submit finds the milestone whose
work is on disk. `compile` and `grade` remain as plumbing for scripts
and the bundled GitHub workflow.
"""
import argparse
import os
import sys
from pathlib import Path

import yaml

from . import config
from . import gitio
from . import journey
from .compiler import compile_course
from .grader import grade as run_grade
from .llm import LLM
from .path_engine import decide, actuate


def main():
    p = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0] or "") or "sy",
        description="The learning agent. Run with no arguments to talk.")
    sub = p.add_subparsers(dest="cmd")

    def home_flag(sp):
        sp.add_argument("--home", default=None,
                        help="journey directory (default $SYLABIS_HOME or ~/sylabis)")

    i = sub.add_parser("init", help="one-time setup: save + check your API key")
    i.add_argument("--key", default=None,
                   help="Anthropic API key (prompted for when omitted)")
    i.add_argument("--no-validate", action="store_true",
                   help="save without the one-token validation call")
    home_flag(i)

    l = sub.add_parser("learn", help="start a course in your journey")
    l.add_argument("topic")
    l.add_argument("--hours", type=int, default=5)
    l.add_argument("--hardware", default="")
    l.add_argument("--prior", default="",
                   help="extra known concepts beyond your verified knowledge")
    l.add_argument("--mock", action="store_true")
    home_flag(l)

    n = sub.add_parser("next", help="what to do now, across every course")
    n.add_argument("course_dir", nargs="?", default=None,
                   help="limit to one course bundle (optional)")
    home_flag(n)

    su = sub.add_parser("submit", help="grade the work sitting in your journey")
    su.add_argument("milestone_id", nargs="?", default=None,
                    help="only needed when several milestones are ready")
    su.add_argument("--hours", type=float, default=None,
                    help="hours you actually spent")
    su.add_argument("--mock", action="store_true")
    home_flag(su)

    j = sub.add_parser("journey", help="progress + the knowledge map")
    home_flag(j)

    w = sub.add_parser("web", help="the journey in your browser")
    w.add_argument("--port", type=int, default=8787)
    w.add_argument("--mock", action="store_true")
    home_flag(w)

    pub = sub.add_parser("publish", help="put a course on GitHub — pushed "
                                         "work grades itself there")
    pub.add_argument("course", nargs="?", default=None,
                     help="only needed when the journey has several courses")
    pub.add_argument("--repo", default=None,
                     help="existing empty repo URL to push to")
    pub.add_argument("--public", action="store_true",
                     help="create the repo public (default private — the "
                          "bundle includes your notes and reflections)")
    home_flag(pub)

    sy = sub.add_parser("sync", help="pull CI-graded state down, push "
                                     "local work up")
    sy.add_argument("course", nargs="?", default=None,
                    help="one course (default: every published course)")
    home_flag(sy)

    sh = sub.add_parser("share", help="the links that show your verified work")
    sh.add_argument("course", nargs="?", default=None,
                    help="only needed when the journey has several courses")
    home_flag(sh)

    at = sub.add_parser("attach", help="connect a course that lives in "
                                       "another repo or directory")
    at.add_argument("source", help="git URL (clones) or local path (links)")
    home_flag(at)

    s = sub.add_parser("serve", help="MCP stdio server (journey-wide "
                                     "without a course dir)")
    s.add_argument("course_dir", nargs="?", default=None)
    home_flag(s)

    # ---- plumbing: kept for scripts and the bundled grade workflow ----
    c = sub.add_parser("compile", help="(plumbing) compile into an explicit dir")
    c.add_argument("topic")
    c.add_argument("--out", required=True)
    c.add_argument("--hours", type=int, default=5)
    c.add_argument("--hardware", default="")
    c.add_argument("--prior", default="", help="comma-separated known concepts")
    c.add_argument("--mock", action="store_true")

    g = sub.add_parser("grade", help="(plumbing) grade one milestone by id")
    g.add_argument("course_dir")
    g.add_argument("milestone_id")
    g.add_argument("--hours-actual", type=float, default=None)
    g.add_argument("--skip-tier3", action="store_true",
                   help="skip exemplar-calibrated Tier 3 scoring")
    g.add_argument("--mock", action="store_true")

    args = p.parse_args()

    if args.cmd is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("sylabis talks through Claude and no API key is set.\n"
                     "Run `sy init` once to save one — then `sy` just works.")
        from .agent import Agent  # lazy: the SDK client only for talking
        Agent(journey.home()).run()

    elif args.cmd == "init":
        _init(args)

    elif args.cmd == "learn":
        home = journey.home(args.home)
        prior = journey.prior_knowledge(home) + \
            [s.strip() for s in args.prior.split(",") if s.strip()]
        profile = {"weekly_hours": args.hours, "hardware": args.hardware,
                   "prior_knowledge": prior}
        if prior:
            print(f"Building on {len(prior)} known concepts.")
        out = journey.new_course_dir(home, args.topic)
        compile_course(args.topic, profile, out, LLM(mock=args.mock))
        journey.emit_map(home)
        step = journey.course_next(out)
        print(f"\nStart here: sylabis next  →  {step['milestone_id']} — "
              f"{step['title']}")

    elif args.cmd == "next":
        if args.course_dir:
            _print_step(journey.course_next(Path(args.course_dir)),
                        base=Path(args.course_dir))
            return
        steps = journey.next_steps(journey.home(args.home))
        if not steps:
            print('Nothing here yet. Start with: sylabis learn "a topic"')
            return
        for step in steps:
            _print_step(step)

    elif args.cmd == "submit":
        _submit(args)

    elif args.cmd == "journey":
        home = journey.home(args.home)
        steps = journey.next_steps(home)
        if not steps:
            print('Nothing here yet. Start with: sylabis learn "a topic"')
            return
        for step in steps:
            _print_step(step)
        know = journey.knowledge(home)
        print(f"\nVerified knowledge — {len(know)} concepts:")
        for e in know:
            where = ", ".join(f"{ev['course']}/{ev['milestone_id']}"
                              for ev in e["evidence"])
            print(f"  - {e['concept']}  ({where})")
        path = journey.emit_map(home)
        if path:
            print(f"\nMap written: {path}")

    elif args.cmd == "web":
        from .web import serve as web_serve  # lazy: pulls http.server
        web_serve(journey.home(args.home), port=args.port, mock=args.mock)

    elif args.cmd == "publish":
        _publish(args)

    elif args.cmd == "sync":
        _sync(args)

    elif args.cmd == "share":
        _share(args)

    elif args.cmd == "attach":
        home = journey.home(args.home)
        dest = journey.attach(home, args.source)
        journey.emit_map(home)
        print(f"Attached {dest.name!r} to the journey.")
        print("Keep it fresh with: sylabis sync")
        _print_step(journey.course_next(dest))

    elif args.cmd == "serve":
        from .mcp_server import MCPServer  # lazy: stdio server pulls no deps
        MCPServer(Path(args.course_dir) if args.course_dir else None,
                  home_dir=args.home).run()

    elif args.cmd == "compile":
        profile = {"weekly_hours": args.hours, "hardware": args.hardware,
                   "prior_knowledge": [s.strip() for s in args.prior.split(",")
                                       if s.strip()]}
        compile_course(args.topic, profile, Path(args.out), LLM(mock=args.mock))

    elif args.cmd == "grade":
        result = _grade_and_adapt(Path(args.course_dir), args.milestone_id,
                                  LLM(mock=args.mock),
                                  hours_actual=args.hours_actual,
                                  skip_tier3=args.skip_tier3)
        sys.exit(0 if result["passed"] else 1)


def _init(args) -> None:
    """One-time setup: capture the key, save it where every command finds
    it ($SYLABIS_HOME/.env), prove it works, say what things cost."""
    key = args.key
    if not key:
        if not sys.stdin.isatty():
            sys.exit("No terminal to prompt from — pass the key directly:\n"
                     "  sy init --key sk-ant-...")
        import getpass
        key = getpass.getpass(
            "Paste your Anthropic API key "
            "(https://console.anthropic.com/settings/keys): ").strip()
        if not key:
            sys.exit("No key entered — nothing saved.")
    path = config.save_key(key, journey.home(args.home)
                           if args.home else None)
    print(f"Key saved to {path} (readable only by you).")
    if args.home and Path(args.home).resolve() != config.home().resolve():
        print(f"Note: commands only read this file when SYLABIS_HOME "
              f"points there:\n  export SYLABIS_HOME={args.home}")
    if not args.no_validate:
        os.environ["ANTHROPIC_API_KEY"] = key
        from .llm import validate_key
        problem = validate_key()
        # Exit 0 either way — the key IS saved; the message says how to fix.
        print(problem if problem else "Key checked against the API: working.")
    print("\nWhat it costs: compiling a course is roughly $1-4 of API "
          "usage;\ngrading a submission $0.10-0.50.\n\nNow run: sy")


def _pick_course(home: Path, name: str | None, verb: str) -> Path:
    """The no-ids UX for course-scoped verbs: one course means no argument
    needed; several means list them and let the learner pick."""
    dirs = journey.course_dirs(home)
    if not dirs:
        sys.exit('Nothing here yet. Start with: sylabis learn "a topic"')
    if name:
        match = [d for d in dirs if d.name == name]
        if not match:
            sys.exit(f"No course {name!r}. Courses: "
                     + ", ".join(d.name for d in dirs))
        return match[0]
    if len(dirs) == 1:
        return dirs[0]
    print(f"Several courses — pick one:")
    for d in dirs:
        print(f"  sylabis {verb} {d.name}")
    sys.exit(1)


def _publish(args) -> None:
    home = journey.home(args.home)
    cdir = _pick_course(home, args.course, "publish")
    try:
        out = gitio.publish(cdir, repo_url=args.repo,
                            private=not args.public)
    except gitio.GitError as e:
        sys.exit(str(e))
    if not (out["remote_set"] or out["pushed_new"]):
        print(f"{cdir.name}: already published and up to date.")
    url = gitio.web_url(out["remote"])
    branch = gitio.current_branch(cdir)
    print(f"Published {cdir.name!r} -> {url or out['remote']}")
    print("Enable auto-grading: add ANTHROPIC_API_KEY as a repo secret "
          "(Settings -> Secrets, or `gh secret set ANTHROPIC_API_KEY`).")
    if url:
        print(f"Share it: {url}/blob/{branch}/README.md")


def _sync(args) -> None:
    """Sync published courses: local work up, CI-graded state down.
    Never re-runs the path engine — path decisions already ran wherever
    the grade happened (locally, or in CI via `sylabis grade`)."""
    home = journey.home(args.home)
    if args.course:
        dirs = [_pick_course(home, args.course, "sync")]
    else:
        dirs = journey.course_dirs(home)
        if not dirs:
            sys.exit('Nothing here yet. Start with: sylabis learn "a topic"')
    failures = 0
    for cdir in dirs:
        if not (gitio.is_repo(cdir) and gitio.remote_url(cdir)):
            if args.course:
                sys.exit(f"{cdir.name} is not published — run "
                         "`sylabis publish` first.")
            print(f"{cdir.name}: not published — skipped.")
            continue
        try:
            out = gitio.sync(cdir)
        except gitio.GitError as e:  # one broken remote never aborts the rest
            print(f"{cdir.name}: {e}")
            failures += 1
            continue
        if out["pulled"] or out["pushed"]:
            print(f"{cdir.name}: "
                  + ("pulled new grades, " if out["pulled"] else "")
                  + ("pushed local work" if out["pushed"] else "up to date"))
        else:
            print(f"{cdir.name}: up to date")
    journey.emit_map(home)  # CI-pulled grades reach knowledge.md
    if failures:
        sys.exit(1)


def _share(args) -> None:
    """Compose the links that show verified work — pure local string work,
    zero network, no key needed."""
    home = journey.home(args.home)
    cdir = _pick_course(home, args.course, "share")
    remote = gitio.remote_url(cdir) if gitio.is_repo(cdir) else None
    url = gitio.web_url(remote)
    reports = sorted((cdir / "portfolio" / "reports").glob("*.md")) \
        if (cdir / "portfolio" / "reports").exists() else []
    if not url:
        print(f"{cdir.name} is not on GitHub yet — run `sylabis publish` "
              "first for shareable links.")
        if reports:
            print("\nLocal grade reports:")
            for r in reports:
                print(f"  {r}")
        return
    branch = gitio.current_branch(cdir)
    print(f"Course page (GitHub renders this): {url}")
    print(f"Landing page: {url}/blob/{branch}/README.md")
    for r in reports:
        print(f"Grade report {r.stem}: "
              f"{url}/blob/{branch}/portfolio/reports/{r.name}")
    print("\nBadge for any README:")
    print(f"  [![sylabis]({'https://img.shields.io/badge/sylabis-verified_learning-blue'})]({url})")


def _print_step(step: dict, base: Path | None = None) -> None:
    base = base or Path(journey.COURSES_SUBDIR) / step["course"]
    label = step["course_title"]
    if step["status"] == "complete":
        print(f"{label}: complete — see {base / 'portfolio' / 'index.md'}")
    elif step["status"] == "blocked":
        print(f"{label}: blocked — {step['milestone_id']} waits on "
              f"{', '.join(step['blocked_on'])}")
    else:
        print(f"{label}: {step['milestone_id']} — {step['title']} "
              f"(~{step['estimated_hours']}h)")
        print(f"  Read: {base / step['milestone_id'] / 'LESSON.md'}")


def _submit(args) -> None:
    """Grade without being told where: find ready milestones whose required
    files are on disk; only ask for an id when several qualify."""
    home = journey.home(args.home)
    candidates = journey.submittable(home)
    ready = [c for c in candidates if c["status"] == "ready"]
    if args.milestone_id:
        ready = [c for c in ready if c["milestone_id"] == args.milestone_id
                 or c["course"] == args.milestone_id]
    if not ready:
        waiting = [c for c in candidates if c["status"] == "awaiting_work"]
        if waiting:
            for c in waiting:
                print(f"{c['course']}/{c['milestone_id']}: waiting on "
                      f"{', '.join(c['missing'])}")
        else:
            print("Nothing ready to grade. See: sylabis next")
        sys.exit(1)
    if len(ready) > 1:
        print("Several milestones are ready — pick one:")
        for c in ready:
            print(f"  sylabis submit {c['milestone_id']}   ({c['course']})")
        sys.exit(1)
    step = ready[0]
    cdir = journey.course_dir(home, step["course"])
    result = _grade_and_adapt(cdir, step["milestone_id"], LLM(mock=args.mock),
                              hours_actual=args.hours)
    journey.emit_map(home)
    # Commit here — after actuate(), so remedials land in the same commit —
    # and ONLY here, never in the `grade` plumbing path: that's what CI
    # runs, and grade.yml commits its own state. A git problem must never
    # eat a grade that was already recorded.
    try:
        committed = gitio.commit_all(cdir, _grade_commit_message(result))
    except gitio.GitError as e:
        committed = False
        print(f"\n(git: {e} — the grade is recorded either way)")
    if committed:
        print("\nCommitted to course history."
              + ("" if not gitio.remote_url(cdir)
                 else " Push it with: sylabis sync"))
    sys.exit(0 if result["passed"] else 1)


def _grade_commit_message(result: dict) -> str:
    return (f"grade: {result['milestone_id']} attempt {result['attempt']} — "
            f"{'passed' if result['passed'] else 'not yet'} "
            f"{result['grade']:.0%}")


def _grade_and_adapt(course_dir: Path, milestone_id: str, llm: LLM,
                     **grade_kw) -> dict:
    """Grade one milestone, then let the path engine act on the result —
    the shared back half of `submit` and `grade`."""
    result = run_grade(course_dir, milestone_id, llm, **grade_kw)
    print("\n" + result["feedback"])
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    milestone = next(m for m in manifest["milestones"]
                     if m["id"] == milestone_id)
    decisions = decide(course_dir, milestone, result)
    for d in decisions:
        print(f"\n>> {d['action']}: {d['target']}")
    for line in actuate(course_dir, decisions, llm=llm):
        print(f"   {line}")
    return result


if __name__ == "__main__":
    main()
