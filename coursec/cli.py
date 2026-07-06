"""
coursec — course compiler CLI.

  coursec compile "topic" --out DIR [--hours N] [--hardware STR] [--mock]
  coursec grade DIR MILESTONE_ID [--hours-actual N] [--mock]
  coursec next DIR            # what should the learner do now?
"""
import argparse
import sys
from pathlib import Path

import yaml

from .compiler import compile_course
from .grader import grade as run_grade
from .llm import LLM
from .path_engine import decide


def main():
    p = argparse.ArgumentParser(prog="coursec")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile")
    c.add_argument("topic")
    c.add_argument("--out", required=True)
    c.add_argument("--hours", type=int, default=5)
    c.add_argument("--hardware", default="")
    c.add_argument("--prior", default="", help="comma-separated known concepts")
    c.add_argument("--mock", action="store_true")

    g = sub.add_parser("grade")
    g.add_argument("course_dir")
    g.add_argument("milestone_id")
    g.add_argument("--hours-actual", type=float, default=None)
    g.add_argument("--mock", action="store_true")

    n = sub.add_parser("next")
    n.add_argument("course_dir")

    args = p.parse_args()

    if args.cmd == "compile":
        profile = {"weekly_hours": args.hours, "hardware": args.hardware,
                   "prior_knowledge": [s.strip() for s in args.prior.split(",") if s.strip()]}
        compile_course(args.topic, profile, Path(args.out), LLM(mock=args.mock))

    elif args.cmd == "grade":
        course_dir = Path(args.course_dir)
        result = run_grade(course_dir, args.milestone_id, LLM(mock=args.mock),
                           hours_actual=args.hours_actual)
        print("\n" + result["feedback"])
        manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
        milestone = next(m for m in manifest["milestones"]
                         if m["id"] == args.milestone_id)
        decisions = decide(course_dir, milestone, result)
        for d in decisions:
            print(f"\n>> {d['action']}: {d['target']}")
        sys.exit(0 if result["passed"] else 1)

    elif args.cmd == "next":
        course_dir = Path(args.course_dir)
        manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
        for m in manifest["milestones"]:
            gpath = course_dir / m["id"] / "grade.yaml"
            done = gpath.exists() and yaml.safe_load(gpath.read_text()).get("passed")
            if not done:
                deps_ok = all(
                    (course_dir / d / "grade.yaml").exists() and
                    yaml.safe_load((course_dir / d / "grade.yaml").read_text()).get("passed")
                    for d in m.get("depends_on", []))
                if deps_ok:
                    print(f"Next: {m['id']} — {m['title']} "
                          f"(~{m['estimated_hours']}h)")
                    print(f"Read: {m['id']}/LESSON.md")
                    return
                print(f"Blocked: {m['id']} waiting on {m['depends_on']}")
                return
        print("Course complete. Check portfolio/state.yaml.")


if __name__ == "__main__":
    main()
