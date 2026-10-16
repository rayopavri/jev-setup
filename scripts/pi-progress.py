#!/usr/bin/env python3
"""
pi-progress.py -- run one pi job and publish its live progress for the status line.

    pi-progress.py <model> <thinking> <limit-seconds> <level> <attempt> <task>

Runs pi in JSON mode and follows its event stream. While it runs, a small status
file in ~/.config/typesafe/running/ says which model is working, on what, how many
steps it has taken, what it is doing now, and how full its context window is.
~/.claude/statusline.sh reads those files. The file is removed when the job ends.

Stuck, not slow: a job is stopped when it stops making progress, not when a clock
runs out. Every event pi emits (a token, a step, a tool result) counts as activity.
  - idle: no activity for CLINE_IDLE seconds (default 180) -> stopped, exit 143.
    While one of the model's own commands runs (a build, a test suite), the allowance
    is CLINE_TOOL_IDLE (default 600), because those are legitimately quiet.
  - ceiling: <limit-seconds> total, sized by the caller from the task -> exit 142.

<model> is a ClinePass model (cline-pass/...), the Kimi Code plan (kimi-coding/k3), or,
on a pay-as-you-go backup, a full pi model that names its provider (cline-credits/...,
openrouter/...). See cline_pause.py.

Stdout gets pi's final answer, as in text mode, so callers see the same result.
Stderr gets one line per step. Exit code is pi's, or 142 (ceiling) / 143 (stuck).
"""

import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cline_pause  # noqa: E402

RUNNING = os.path.expanduser("~/.config/typesafe/running")
# Context windows from `pi --list-models cline-pass`. Unknown models show tokens only.
CONTEXT = {
    "cline-pass/qwen3.8-max": 1_000_000,
    "cline-pass/qwen3.7-max": 262_100,
    "cline-pass/deepseek-v4-pro": 1_000_000,
    "cline-pass/kimi-k3": 1_000_000,
    "cline-pass/deepseek-v4.1-flash": 1_000_000,
    "cline-pass/qwen3.7-plus": 1_000_000,
    "kimi-coding/k3": 1_048_576,
}
LABEL = {
    "cline-pass/qwen3.8-max": "Qwen3.8 Max",
    "cline-pass/qwen3.7-max": "Qwen3.7 Max",
    "cline-pass/deepseek-v4-pro": "DeepSeek V4 Pro",
    "cline-pass/kimi-k3": "Kimi K3",
    "cline-pass/deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "cline-pass/qwen3.7-plus": "Qwen3.7 Plus",
    "kimi-coding/k3": "Kimi K3 · Kimi Code plan",
}
# The pay-as-you-go backups' models, named so the status line says which and where.
for _spec in cline_pause.BACKUPS.values():
    for _upstream, _name in cline_pause.BACKUP_NAME.items():
        LABEL[_spec["prefix"] + _upstream] = f"{_name} · {_spec['label']}"
IDLE =int(os.environ.get("CLINE_IDLE", "180"))
TOOL_IDLE = int(os.environ.get("CLINE_TOOL_IDLE", "600"))
CEILING_EXIT, STUCK_EXIT = 142, 143
VERB = {"read": "reading", "edit": "editing", "write": "writing", "bash": "running"}


def title_of(task: str) -> str:
    for line in task.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            if line.lower().startswith("work order:"):
                line = line[len("work order:"):].strip()
            return line[:60]
    return "task"


def describe(tool: str, args: dict) -> str:
    target = args.get("path") or args.get("command") or ""
    target = os.path.basename(target) if args.get("path") else target.split("\n")[0]
    return f"{VERB.get(tool, tool)} {target[:40]}".strip()


def stop(proc: subprocess.Popen) -> None:
    """End pi and everything it started (a build, a test run), politely then firmly."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def main() -> int:
    model, thinking, limit, level, attempt, task = sys.argv[1:7]
    os.makedirs(RUNNING, exist_ok=True)
    status_path = os.path.join(RUNNING, f"{os.getpid()}.json")
    status = {
        "pid": os.getpid(), "model": model, "label": LABEL.get(model, model.split("/")[-1]),
        "title": title_of(task), "level": level, "attempt": int(attempt),
        "started": time.time(), "limit": int(limit), "steps": 0, "now": "starting",
        "context": 0, "context_max": CONTEXT.get(model, 0), "cwd": os.getcwd(),
    }

    def publish() -> None:
        tmp = status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(status, handle)
        os.replace(tmp, status_path)  # atomic: the status line never reads half a file

    # pi lives under nvm's Node, which is not on every caller's PATH.
    nvm = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")))
    env = dict(os.environ, PATH=os.pathsep.join([d for d in nvm if os.path.exists(os.path.join(d, "pi"))]
                                                 + [os.environ.get("PATH", "")]))
    # A ClinePass model goes through the clinepass provider; a backup model already
    # names its own provider (openrouter/...), and its refusals are not ClinePass's.
    on_clinepass = model.startswith("cline-pass/")
    on_kimi = model.startswith("kimi-coding/")  # the user's Kimi Code plan: its own pause
    pi_model = f"clinepass/{model}" if on_clinepass else model
    try:
        proc = subprocess.Popen(
            [os.environ.get("CLINE_PI_BIN", "pi"), "--model", pi_model, "--thinking", thinking, "--mode", "json", "-p", task],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, bufsize=1, env=env,
            start_new_session=True,  # its own process group, so stopping it stops its commands too
        )
    except OSError as error:
        print(f"  · could not start pi: {error}", file=sys.stderr)
        return 127
    publish()  # only once pi is really running, so a failed start leaves no ghost job
    final = ""
    clock = {"last": time.time(), "in_tool": False, "stopped": 0}

    def watchdog() -> None:
        ceiling = int(limit)
        while proc.poll() is None:
            time.sleep(2)
            now = time.time()
            quiet = now - clock["last"]
            allowance = TOOL_IDLE if clock["in_tool"] else IDLE
            reason = ""
            if now - status["started"] > ceiling:
                clock["stopped"], reason = CEILING_EXIT, (f"reached the {ceiling // 60}m ceiling" if ceiling >= 60 else f"reached the {ceiling}s ceiling")
            elif quiet > allowance:
                clock["stopped"], reason = STUCK_EXIT, f"no progress for {int(quiet)}s -- stuck"
            if reason:
                print(f"  · stopped: {reason}", file=sys.stderr, flush=True)
                stop(proc)
                return
            status["idle"] = int(quiet)
            publish()

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for line in proc.stdout:
            clock["last"] = time.time()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "tool_execution_end":
                clock["in_tool"] = False
            if kind == "tool_execution_start":
                clock["in_tool"] = True
                status["steps"] += 1
                status["now"] = describe(event.get("toolName", ""), event.get("args") or {})
                print(f"  · step {status['steps']}: {status['now']}", file=sys.stderr, flush=True)
                publish()
            elif kind == "message_end":
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                if message.get("stopReason") == "error":
                    error = str(message.get("errorMessage") or "")
                    pause_kind = cline_pause.classify(error) if on_clinepass else ""
                    if pause_kind:
                        record = cline_pause.pause(pause_kind, error)
                        print(f"  · ClinePass refused: {cline_pause.describe(record)}", file=sys.stderr, flush=True)
                        clock["stopped"] = cline_pause.PAUSE_EXIT
                        stop(proc)
                        break
                    kimi_kind = cline_pause.kimi_classify(error) if on_kimi else ""
                    if kimi_kind:
                        record = cline_pause.kimi_pause(kimi_kind, error)
                        print(f"  · Kimi Code plan refused: {cline_pause.describe(record)}", file=sys.stderr, flush=True)
                        clock["stopped"] = cline_pause.PAUSE_EXIT
                        stop(proc)
                        break
                    if not on_clinepass and not on_kimi and cline_pause.backup_refusal(error):
                        label = cline_pause.backup_stop(model, error)
                        print(f"  · {label} refused ({error[:160]}); that backup switched off",
                              file=sys.stderr, flush=True)
                        clock["stopped"] = cline_pause.PAUSE_EXIT
                        stop(proc)
                        break
                    print(f"  · provider error: {error[:200]}", file=sys.stderr, flush=True)
                usage = message.get("usage") or {}
                # What the model had in view on this call: fresh input + cached prefix + its reply.
                used = sum(int(usage.get(k) or 0) for k in ("input", "cacheRead", "cacheWrite", "output"))
                if used:
                    status["context"] = used
                text = "".join(part.get("text", "") for part in message.get("content") or []
                               if part.get("type") == "text")
                if text.strip():
                    final = text
                    status["now"] = "thinking"
                publish()
        code = proc.wait()
    except KeyboardInterrupt:
        stop(proc)
        code = proc.wait()
    finally:
        try:
            os.remove(status_path)
        except OSError:
            pass
    if final:
        print(final.strip())
    if clock["stopped"]:
        return clock["stopped"]
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
