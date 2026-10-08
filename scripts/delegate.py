#!/usr/bin/env python3
"""
delegate.py -- build one planned piece of work on ClinePass, or hand it back to Claude.

    python3 delegate.py run <order.md> [--dry-run] [--escalate | --up]
    python3 delegate.py check <id> <result-file>
    python3 delegate.py review <id> pass|redo|fail ["note"]

`run` asks Jev to score the written work order itself (not the user's original
request): can a separate coding agent do it from the files alone, what kind of job
is it, how deep, how risky. A clear order goes to the matched ClinePass model through
cline-worker.sh; anything else exits 5 so Claude keeps it.

`check` is Jev's quality gate: it scores the real result against the order's "Done
when", and says pass, redo or escalate. The result file is short and concrete: each
Done-when check with its real output (run it yourself), plus the files changed.
`review` records Claude's own verdict after looking at the real output, so the routing
report can say whether lane quality is holding up, model by model.

The quality ladder (the user's rule, 2026-10-04), three attempts at most:
  1. `run`             -- the lane Jev matched.
  2. `run --escalate`  -- a redo with sharper feedback: more effort, the strongest model.
  3. `run --up`        -- the next Claude model up from Jev's pick (at least Sonnet).

Whenever a piece does not end up built on ClinePass -- the order needs judgment,
ClinePass is paused, or both attempts failed -- it goes back to Jev, which picks the
Claude model for it from the order's complexity and risk (the same rules the hook
uses). The last line says which: "Claude model: sonnet (Sonnet 5)".

A piece builds on its lane (ClinePass or Codex), small model first (the user's rule,
2026-10-05); if that lane is at its limit, on the same family pay-as-you-go, cheap model
first -- Cline credits, then OpenRouter (see cline_pause.py). Design
pieces and image jobs always stay with Claude.
--escalate asks for more: extra reasoning effort on Codex, the full model on a
pay-as-you-go backup -- for a redo after the first result fell short.

Exit codes: 0 built, 2 usage, 4 the worker's attempts failed, 5 keep with Claude
(incl. ClinePass paused with no backup), 6 Jev unreachable. Every non-zero exit names a Claude model.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cline_pause  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("jev_refine", os.path.join(HERE, "jev-refine.py"))
jev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jev)

WORKER = os.path.join(HERE, "cline-worker.sh")
KEEP = 5
NO_JEV = 6


def score(order: str, cwd: str) -> dict:
    """Jev's judgment of the work order, using the same questions and gates as the hook."""
    wanted = ("offload_ok", "task_kind", "complexity", "destructive", "underspecified")
    questions = {k: v for k, v in jev.build_questions().items() if k in wanted}
    for key in wanted:
        if key in questions:
            questions[key] = dict(questions[key])
            questions[key]["instructions"] = questions[key]["instructions"].replace(
                "`user_prompt`", "the work order in `user_prompt`")
    body = {"state": {"user_prompt": order[: jev.MAX_PROMPT_CHARS], "working_directory": cwd},
            "model": jev.MODEL, "questions": questions}
    response, provider = jev.ask_body(body, os.environ.get("TYPESAFE_API_KEY", "").strip())
    answers = response.get("answers") or {}
    answers["_provider"] = provider
    return answers


def noul(answers: dict, key: str) -> float:
    a = answers.get(key) or {}
    return float(a.get("noul") or 0.0) if a.get("type") == "noul" else 0.0


def claude_model(answers: dict) -> str:
    """Jev's Claude tier for a piece that stays with Claude. Unsure means the baseline."""
    tier, reason = jev.route_model(answers, noul(answers, "underspecified"), noul(answers, "destructive"))
    if not tier:
        tier, reason = jev.BASELINE, "Jev unsure of the size, so the baseline"
    return f"{jev.TIER_LABEL.get(tier, tier)} -- {reason}", tier


def hand_back(record: dict, answers: dict, why: str, code: int, up: bool = False) -> int:
    label, tier = claude_model(answers)
    if up:  # the quality ladder's last rung: one Claude model above Jev's pick, Sonnet at least
        tier = jev.TIER_ORDER[min(len(jev.TIER_ORDER) - 1, max(jev._rank(tier) + 1, jev._rank("sonnet")))]
        label = f"{jev.TIER_LABEL[tier]} -- one step up the quality ladder"
    record["where"] = f"Claude {jev.TIER_LABEL.get(tier, tier)}"
    record.update({"decision": "claude", "why": why, "claude_tier": tier})
    jev.log_route(record)
    print(f"piece {record['id']}: keep with Claude ({why}).")
    print(f"Claude model: {label}. Run this piece as a subagent with model=\"{tier}\".")
    return code


def run(path: str, dry: bool, escalate: bool = False, up: bool = False) -> int:
    with open(path, encoding="utf-8") as handle:
        order = handle.read()
    title = next((l.lstrip("# ").strip() for l in order.splitlines() if l.strip()), path)[:120]
    cwd = os.getcwd()
    piece = uuid.uuid4().hex[:8]
    record = {"ts": jev._now(), "event": "piece", "id": piece, "title": title, "order": path, "cwd": cwd,
              "order_text": order[:20000], "step": 3 if up else (2 if escalate else 1),
              "escalate": escalate, "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", "")}

    try:
        answers = score(order, cwd)
    except Exception as error:
        record.update({"decision": "claude", "why": f"jev unreachable ({type(error).__name__})"})
        jev.log_route(record)
        print(f"piece {piece}: Jev unreachable -- keep this piece with Claude.")
        return NO_JEV

    record["jev_provider"] = answers.pop("_provider", "")
    if up:
        return hand_back(record, answers, "the lane's result fell short twice -- next model up", 5, up=True)
    offload, risk = noul(answers, "offload_ok"), noul(answers, "destructive")
    depth = (answers.get("complexity") or {}).get("choice") or ""
    depth_conf = float((answers.get("complexity") or {}).get("confidence") or 0.0)
    kind, kind_conf, model = jev.pick_lane(answers)
    level = "light" if depth == "light" else "standard"
    record.update({"offload": round(offload, 2), "risk": round(risk, 2), "complexity": depth,
                   "kind": kind, "kind_conf": round(kind_conf, 2), "model": model, "level": level})

    why = ""
    # Where the piece would build now: its lane, the other free lane if that is down,
    # pay-as-you-go only if both are (cline_pause.lane_where).
    where = cline_pause.lane_where(model, escalate) if model.startswith(("cline-pass/", "codex/")) else ""
    if where and cline_pause.lane_paid(model):
        record.update({"backup": cline_pause.backup_where(), "escalate": escalate})
    if model == jev.KEEP_ON_CLAUDE:
        why = "design work (UX, research, visual direction) -- keep on Claude"
    elif model == jev.IMAGE_LANE:
        why = f"an image job -- run it with {jev.IMAGE_WORKER}, not as a work order"
    elif not where:
        why = cline_pause.lanes_down() or "no lane can take it"
    elif offload < jev.CLINE_OFFLOAD_MIN:
        why = f"offload {offload:.2f} < {jev.CLINE_OFFLOAD_MIN:.2f} -- the order still needs judgment or the conversation"
    elif risk >= 0.50:
        why = f"risk {risk:.2f} -- hard to undo or reaches outside this machine"
    elif depth == "deep" and depth_conf >= 0.55:
        why = "deep reasoning -- keep on Claude"
    if why:
        return hand_back(record, answers, why, KEEP)

    label = record["where"] = where
    down = cline_pause.lanes_down()
    if down:
        label += f" ({down})"
    if dry:
        record.update({"decision": "dry-run"})
        jev.log_route(record)
        print(f"piece {piece}: would build on {label} ({level}, kind {kind or '?'}, offload {offload:.2f}).")
        return 0

    print(f"piece {piece}: building on {label} ({level}, kind {kind or '?'}, offload {offload:.2f}).", flush=True)
    start = time.time()
    # The worker logs each attempt under this piece, so the dashboard links them exactly.
    code = subprocess.call([WORKER, level, order, model] + (["--escalate"] if escalate else []),
                           env={**os.environ, "JEV_PIECE_ID": piece})
    record.update({"exit": code, "seconds": int(time.time() - start)})
    if code == cline_pause.PAUSE_EXIT:
        why = cline_pause.lanes_down() or "every lane refused"
        stopped = cline_pause.backups_stopped()
        if stopped:
            why += f"; pay-as-you-go stopped on {', '.join(stopped)} (credit or key)"
        return hand_back(record, answers, why, KEEP)
    if code != 0:
        return hand_back(record, answers, f"the worker's attempts failed (exit {code})", code)
    record["decision"] = "cline"
    jev.log_route(record)
    print(f"piece {piece}: built. Check the result, then: "
          f"python3 {os.path.abspath(__file__)} review {piece} pass|redo|fail \"<note>\"")
    return code


def find_piece(piece: str) -> dict:
    """The logged record of a piece (the last one with this id), or {}."""
    found = {}
    for path in (jev.ROUTING_LOG + ".1", jev.ROUTING_LOG):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if f'"{piece}"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("event") == "piece" and row.get("id") == piece:
                        found = row
        except OSError:
            continue
    return found


# Jev's quality gate: typed judgments of the result against the order. Advisory --
# Claude still looks at the real output (Jev reads text; it can't run or see anything).
CHECKS = {
    "meets_done_when": "Does `result` show that every check in the \"Done when\" section of `work_order` is met?",
    "complete": "Is the work described in `result` complete -- nothing the `work_order` asks for left out or half-done?",
    "out_of_scope": "Does `result` change things `work_order` did not ask for, or that its \"Do not\" list forbids?",
}
# Tuned 2026-10-04 on real pieces: a good result scored meets 0.92-0.94 / complete 0.65-0.68,
# a broken one 0.01 / 0.01. Jev judges evidence, so the result file must be short and
# concrete -- each Done-when check with its real output, and the files changed -- not a
# code dump (a full listing pulled a good result down to 0.39).
PASS_AT = {"meets_done_when": 0.80, "complete": 0.60}
SCOPE_MAX = 0.30


def check(piece: str, result_path: str) -> int:
    record = find_piece(piece)
    if not record:
        print(f"piece {piece}: not found in the routing log", file=sys.stderr)
        return 2
    order = record.get("order_text") or ""
    if not order and record.get("order") and os.path.exists(record["order"]):
        with open(record["order"], encoding="utf-8") as handle:
            order = handle.read()
    with open(result_path, encoding="utf-8", errors="replace") as handle:
        result = handle.read()
    body = {"state": {"work_order": order[:30000], "result": result[:60000]}, "model": jev.MODEL,
            "questions": {k: {"type": "noul", "instructions": q} for k, q in CHECKS.items()}}
    try:
        response, provider = jev.ask_body(body, os.environ.get("TYPESAFE_API_KEY", "").strip())
    except Exception as error:
        print(f"piece {piece}: Jev unreachable ({type(error).__name__}) -- Claude checks it alone.")
        return NO_JEV
    answers = response.get("answers") or {}
    scores = {k: round(noul(answers, k), 2) for k in CHECKS}
    ok = all(scores[k] >= v for k, v in PASS_AT.items()) and scores["out_of_scope"] < SCOPE_MAX
    step = int(record.get("step") or 1)
    order_path = record.get("order") or "<order.md>"
    if ok:
        verdict, next_step = "pass", "Claude checks the real output, then records the review."
    elif step < 2:
        verdict = "redo"
        next_step = (f"Redo once with sharper feedback: edit the order to name what is wrong, then "
                     f"python3 {os.path.abspath(__file__)} run {order_path} --escalate")
    else:
        verdict = "escalate"
        next_step = f"Next model up: python3 {os.path.abspath(__file__)} run {order_path} --up"
    jev.log_route({"ts": jev._now(), "event": "check", "id": piece, "scores": scores,
                   "verdict": verdict, "next": next_step, "jev_provider": provider})
    shown = ", ".join(f"{k} {v:.2f}" for k, v in scores.items())
    print(f"piece {piece}: Jev's check -- {shown} -> {verdict.upper()}.")
    print(next_step)
    return 0 if ok else 1


def review(piece: str, verdict: str, note: str) -> int:
    if verdict not in ("pass", "redo", "fail"):
        print("verdict must be pass, redo or fail", file=sys.stderr)
        return 2
    jev.log_route({"ts": jev._now(), "event": "review", "id": piece, "verdict": verdict, "note": note[:300]})
    print(f"piece {piece}: recorded {verdict}.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "run":
        return run(argv[1], "--dry-run" in argv[2:], "--escalate" in argv[2:], "--up" in argv[2:])
    if len(argv) >= 3 and argv[0] == "check":
        return check(argv[1], argv[2])
    if len(argv) >= 3 and argv[0] == "review":
        return review(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
    print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
