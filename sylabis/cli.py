"""
sylabis — the learning agent.

The learner surface, in the order you'll use it:

  sylabis                      talk — the agent drives the whole loop
  sylabis web                  the same journey in your browser
  sylabis learn "topic"        start a course in your journey
  sylabis next                 what to do now, across every course
  sylabis submit               grade the work sitting in your journey
  sylabis journey              progress + the knowledge map
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
            sys.exit("sylabis talks through Claude: set ANTHROPIC_API_KEY "
                     "(or put it in .env), then run `sylabis` again.")
        from .agent import Agent  # lazy: the SDK client only for talking
        Agent(journey.home()).run()

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

    elif args.cmd == "attach":
        home = journey.home(args.home)
        dest = journey.attach(home, args.source)
        journey.emit_map(home)
        print(f"Attached {dest.name!r} to the journey.")
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
    sys.exit(0 if result["passed"] else 1)


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
