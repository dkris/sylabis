# Prototype → MVP Plan

## What the prototype proves (this week)
Run the loop on yourself with a real course. The prototype exists to answer
four questions, in order of risk:
1. Does compiled sequencing hold up on a topic you don't know? (biggest risk)
2. Does the claim audit catch real overclaiming, not just fixture overclaiming?
3. Does artifact gravity survive week two? (the completion thesis)
4. Is the event schema sufficient to reconstruct a learner's path? (graph seed)

If 1 or 3 fails, fix the compiler prompts or the artifact specs before
writing any MVP code. MVP polish on a broken loop is wasted money.

## MVP scope — 6 to 8 weeks, 10–20 external users

**In:**
- Hosted grading: GitHub App — learner pushes artifact, Action runs grader,
  feedback arrives as a PR review comment. Zero new UI; GitHub IS the UI.
- Compiler as a web form → generates repo in the learner's GitHub account.
- Tier 3 exemplar scoring for ONE non-technical course (survey synthesis) —
  seed the exemplar set yourself, 4–6 hours, per the knowledge base.
- Technical rubric script generation for ONE technical course (M4 distillation).
- Source verification in harvest: fetch every URL, drop dead ones, flag stale.
- Events shipped to one central store (SQLite or Postgres, nothing fancier).
- Public portfolio page rendered from portfolio/state.yaml (static site).

**Out (resist all of these):**
- Native app, chat UI, dashboards
- More than two courses
- Enterprise anything (SAP integration waits for a referenceable user base)
- Payments (charge nothing until completion rate is known)
- The pathway graph as a feature (collect events; build nothing on them yet)

## The two numbers that decide everything
- **Completion rate** of the 10–20 cohort. MOOCs run 3–15%. If artifact
  gravity is real, you should see 40%+ on a hand-recruited, motivated cohort.
  Below 25%: the thesis is in trouble — diagnose before scaling.
- **Grader trust**: of graded submissions, how many learners disputed the
  grade and were right? Above ~15% and the grader is the product's weak point.

## Week-by-week
1–2  GitHub App + Action grading; compile-to-repo flow
3    Exemplar set for survey synthesis; Tier 3 scoring live
4    Rubric script generation for M4 course; source verification
5    Recruit cohort (10–20, hand-picked, mixed technical/non-technical)
6–8  Cohort runs; you fix what breaks daily; weekly 20-min user calls

## Cost reality
LLM spend per compiled course: roughly $1–4 (compile) + $0.10–0.50 per
grading pass at current Sonnet pricing. At MVP scale this is lunch money.
The real cost is your time seeding exemplars and doing user calls. Budget
half your week for cohort support during weeks 6–8 — that support IS the
research.

## Exit criteria for the MVP
Proceed to a paid beta only if: completion ≥ 40%, grader dispute rate ≤ 15%,
and at least 3 users independently share their portfolio page somewhere
public. The third one is the earliest observable signal of the credential
layer working.
