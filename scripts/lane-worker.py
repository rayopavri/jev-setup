#!/usr/bin/env python3
"""
lane-worker.py -- hand one self-contained coding task to an open-model lane.

    lane-worker.py <light|standard> "<task>" [model] [--escalate]

(cline-worker.sh is a wrapper around this, so existing commands keep working.)
Runs in the current directory, so the lane can read and edit the project's files.

[model] is the lane Jev picked: a ClinePass model (cline-pass/...) or the Codex lane
(codex/...). Anything not on the allowlist falls back to the default for the level.

The ladder (see cline_pause.free_steps), small first (the user's rule, 2026-10-05):
  1. The picked lane, free, if it is up -- a Kimi K3 job tries the user's Kimi Code
     plan first, then ClinePass's Kimi K3 -- its smaller ClinePass sibling first when it
     has one (cline_pause.CLINE_SMALL), then the picked model if that fails. Otherwise
     the other free lane may take one more try (a ClinePass job goes to Codex; a Codex
     job to DeepSeek on ClinePass). At most two free attempts that actually fail; a lane
     that refuses (limit, login) pauses itself and doesn't count.
  2. When the picked lane is down -- paused on a limit or login, not merely failed --
     the same family runs pay-as-you-go, cheap model first, then the full model if the
     cheap one fails: Cline credits first, OpenRouter if those run out. --escalate skips
     the small models: the full model on ClinePass, the family's strongest on
     pay-as-you-go, and more reasoning effort on Codex.
  3. Otherwise exit 4 (the attempts failed) or 75 (nothing could take it), so Claude
     takes the task back.

Each attempt is appended to the routing log, so the lanes can be judged later.
"""

import datetime
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cline_pause  # noqa: E402

# Models this worker may start from: ones that passed check-tools.sh (read, bash and
# write verified on disk), plus the Codex lane. Add a model only after it passes.
ALLOWED = {"cline-pass/qwen3.8-max", "cline-pass/qwen3.7-max", "cline-pass/deepseek-v4-pro",
           "cline-pass/deepseek-v4.1-flash", "cline-pass/qwen3.7-plus",
           "cline-pass/kimi-k3", cline_pause.KIMI_LANE, cline_pause.codex_lane_model()}
DEFAULT_MODEL = os.environ.get("CLINE_DEFAULT_MODEL", "cline-pass/deepseek-v4-pro")
LOG = os.environ.get("JEV_ROUTING_LOG", os.path.expanduser("~/.config/typesafe/jev-routing.jsonl"))
FREE_TRIES = 2
STUCK = {142: "reached the time ceiling", 143: "stopped making progress (stuck)"}


def say(message: str) -> None:
    print(f"== {message}", file=sys.stderr, flush=True)


def log(level: str, model: str, attempt: int, code: int, seconds: int, task: str = "") -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event": "worker", "level": level, "model": model, "attempt": attempt,
                "label": cline_pause.step_label(model), "exit": code, "seconds": seconds,
                "cwd": os.getcwd(), "task": task[:20000], "piece": os.environ.get("JEV_PIECE_ID", ""),
                "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", "")}) + "\n")
    except OSError:
        pass


def effort(model: str, level: str, escalate: bool) -> str:
    """Codex's reasoning effort, or pi's thinking level for everything else."""
    if model.startswith("codex/"):
        return "xhigh" if escalate else ("high" if level == "standard" else "medium")
    return "high" if level == "standard" else "low"


def run(model: str, level: str, task: str, attempt: int, limit: int, escalate: bool) -> int:
    runner = "codex-progress.py" if model.startswith("codex/") else "pi-progress.py"
    thinking = effort(model, level, escalate)
    say(f"Worker: {cline_pause.step_label(model)} ({model}, thinking: {thinking}, "
        f"limit {limit}s) in {os.getcwd()}")
    start = time.time()
    code = subprocess.call([sys.executable, os.path.join(HERE, runner), model, thinking,
                            str(limit), level, str(attempt), task], stdin=subprocess.DEVNULL)
    log(level, model, attempt, code, int(time.time() - start), task)
    if code in STUCK:
        say(f"{model} {STUCK[code]}")
    return code


def paid(model: str, level: str, task: str, attempt: int, limit: int, escalate: bool) -> int:
    """Pay-as-you-go on the same family, small first: the cheap model, then the full one
    if it fails (the family's strongest with --escalate). Cline credits first, OpenRouter
    when those refuse. Returns the exit code."""
    tiers = ["up"] if escalate else ["cheap", "same"]
    refusals, ran = 0, set()
    while refusals < 3 and tiers:
        stand_in = cline_pause.backup_model(model, tiers[0])
        if not stand_in and len(tiers) > 1:
            tiers.pop(0)  # no cheap model on any backup that is on: go to the full one
            continue
        if not stand_in:
            say("No pay-as-you-go backup is on (or it's out of credit). Hand this task back to Claude.")
            return cline_pause.PAUSE_EXIT
        if stand_in in ran:  # the cheap and full model are the same here (GPT-6-Luna)
            break
        say(f"{cline_pause.step_label(model)} is down; running pay-as-you-go, small first: "
            f"{cline_pause.step_label(stand_in)}")
        code = run(stand_in, level, task, attempt, limit, escalate)
        attempt += 1
        ran.add(stand_in)
        if code == 0:
            return 0
        if code == cline_pause.PAUSE_EXIT:
            refusals += 1
            say("That backup refused (credit or key) and switched itself off; trying the next one.")
            continue
        tiers.pop(0)
        if tiers:
            say(f"{cline_pause.step_label(stand_in)} didn't finish; trying the full model")
    say("The pay-as-you-go run couldn't finish this task. Hand it back to Claude.")
    return 4


def main(argv: list[str]) -> int:
    escalate = "--escalate" in argv
    args = [a for a in argv if a != "--escalate"]
    if len(args) < 2 or not args[0] or not args[1]:
        print('usage: cline-worker.sh <light|standard> "<task>" [model] [--escalate]', file=sys.stderr)
        return 2
    level, task = args[0], args[1]
    if level not in ("light", "standard"):
        print(f"unknown level: {level} (use light or standard)", file=sys.stderr)
        return 2
    model = os.environ.get("CLINE_LIGHT_MODEL" if level == "light" else "CLINE_STANDARD_MODEL", DEFAULT_MODEL)
    if len(args) > 2:
        if args[2] in ALLOWED:
            model = args[2]
        else:
            say(f"{args[2]} is not on the allowlist; using {model}")
    limit = int(os.environ.get("CLINE_TIMEOUT", "1200" if level == "light" else "3600"))

    # pi lives under nvm's Node, which isn't on PATH in every shell (Claude Code's included).
    nvm = [d for d in sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")))
           if os.path.exists(os.path.join(d, "pi"))]
    os.environ["PATH"] = os.pathsep.join(nvm + [os.environ.get("PATH", "")])

    down = cline_pause.lanes_down()
    if down:
        say(f"Down right now: {down}.")
    tried, failures, attempt = set(), 0, 1
    while failures < FREE_TRIES:
        free = cline_pause.free_steps(model)
        if free and not escalate:  # small first: the ClinePass sibling before the picked model
            free = cline_pause.small_first(free[0]) + free[1:]
        steps = [s for s in free if s not in tried]
        if not steps:
            break
        step = steps[0]
        tried.add(step)
        code = run(step, level, task, attempt, limit, escalate)
        attempt += 1
        if code == 0:
            return 0
        if code == cline_pause.PAUSE_EXIT:
            say(f"{cline_pause.step_label(step)} refused (limit or login) and is paused; moving on.")
            continue
        failures += 1
        if failures < FREE_TRIES and steps[1:]:
            say(f"{step} didn't finish; trying the next free lane")

    if failures:
        say(f"The free lanes' attempts failed ({', '.join(sorted(tried))}). Hand this task back to Claude.")
        return 4
    # The picked lane is down (limit or login): the same model, pay-as-you-go.
    return paid(model, level, task, attempt, limit, escalate)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
