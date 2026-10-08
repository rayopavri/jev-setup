
# Jev routing (all projects)

The Jev hook judges every prompt and adds a "Jev prompt triage" note. Follow its route unless the user's request says otherwise.
Scripts live in `~/.claude/skills/jev-interceptor/scripts/`. Explain routing in plain English.

- **Claude model suggested** ("Jev sizes this … to sonnet"): run the substantive work in a subagent with that `model`. Trivial one-line answers can stay inline.
- **ClinePass lane** ("sends this to the open-model lane"): write a self-contained task and run the `cline-worker.sh` command the note gives. Review the result before reporting.
- **Plan then delegate** ("plan here, build on ClinePass"):
  1. Plan on Claude and ask the user for any decisions.
  2. Write work orders from `work-order-template.md` and run each with `delegate.py run <order.md>`.
  3. Check every result and record it with `delegate.py review <id> pass|redo|fail`.
  4. Keep few-minute jobs on Claude.
- **Anything ClinePass hands back** (delegate.py exit 4, 5 or 6, or worker exit 4 or 75): use the Claude model Jev names in the output ("Claude model: …"), as a subagent. Tell the user it came back.
- **Review gate:** any work Jev sends to another model (a Claude subagent, ClinePass, Codex, OpenRouter) is a draft, never a final answer. Before showing it to the user or building on it:
  1. Check it against the user's actual request and, for delegated jobs, the work order's "Done when". Look at the real output (read the diff, run the tests, view the screenshot), not the worker's own claim of success. For delegated jobs, also run Jev's check: write each Done-when check with its real output (plus files changed) to a short file and run `delegate.py check <id> <file>`.
  2. Judge requirement and quality: nothing missing, nothing out of scope added, matches the surrounding style, no invented facts, no broken or half-finished parts.
  3. Pass only if both hold. Record delegated jobs with `delegate.py review <id> pass`.
  4. If it fails, send it back with specific feedback naming what is wrong (`review <id> redo`, then `delegate.py run <order.md> --escalate` with a sharper order).
  5. If the second attempt still fails, or the same model is clearly out of its depth, go one model up: `delegate.py run <order.md> --up` names the Claude model; run it as a subagent there.
  6. If a stronger model also fails, do it on Claude and tell the user. Never pass off a failed result. Three attempts at most per job.
  7. Tell the user in plain English what was checked and whether anything was redone or escalated.
- **Small first:** lane jobs try the smaller model first and move to the full model only if the result falls short. Claude work starts at Jev's pick and goes one model up only if it falls short. Risky jobs start at Jev's pick. Always tell the user which model did the work. When Claude and its extra usage run out, `claude-limit.py` (StopFailure hook) asks the user before any Claude-on-OpenRouter spend.
- **Catalog notice** ("Catalog: N newly installed or changed …"): finish the user's request first, then review each pending item with `jev_catalog.py pending`. Read the file, then record a routing line (`review <name> --line "…"`) when it competes with other skills, or `review <name> --keep` when its own description already routes well.
- **Commands:**
  - Dashboard of every routing decision: `python3 jev-dashboard.py --open`.
  - Skill/plugin catalog: `python3 jev_catalog.py status|pending`.
  - ClinePass pause: `python3 cline_pause.py status|clear`.
  - Scorecard: `python3 jev-routing-report.py`.
  - Off switches (env): `JEV_CLINE=0`, `JEV_PLAN_DELEGATE=0`, `JEV_LANES=shadow|off`.
