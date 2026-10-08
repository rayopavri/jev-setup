#!/usr/bin/env python3
"""
codex-progress.py -- run one job on the Codex CLI and publish its live progress.

    codex-progress.py <codex/model> <effort> <limit-seconds> <level> <attempt> <task>

The Codex counterpart of pi-progress.py, with the same arguments, status file and
exit codes, so cline-worker.sh runs either one the same way. It runs `codex exec`
on the user's ChatGPT plan (their Codex subscription), in the current directory,
in the workspace-write sandbox: it can edit files under the project, but not reach
the network or write outside it.

<effort> is Codex's reasoning effort: low, medium, high or xhigh.

Codex's own config (~/.codex/config.toml) is kept -- the ChatGPT login lives there --
but the model and effort are set here and its desktop notifier is switched off. Runs
are --ephemeral, so worker jobs don't fill the Codex app's session history.

A turn that fails on the usage limit or an expired login pauses the Codex lane
(cline_pause.codex_pause) and exits 75, so the worker hands the job on. Any other
failure exits non-zero. Stuck, not slow: no activity for CODEX_IDLE seconds (default
600 -- Codex reports its thinking only when a step ends) -> exit 143; the ceiling
-> exit 142.

Stdout gets Codex's final answer. Stderr gets one line per step.
"""

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
CONTEXT = {"gpt-6-luna": 272_000, "gpt-5.6-terra": 272_000}
IDLE = int(os.environ.get("CODEX_IDLE", "600"))
TOOL_IDLE = int(os.environ.get("CODEX_TOOL_IDLE", "900"))
CEILING_EXIT, STUCK_EXIT = 142, 143


def title_of(task: str) -> str:
    for line in task.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            if line.lower().startswith("work order:"):
                line = line[len("work order:"):].strip()
            return line[:60]
    return "task"


def describe(item: dict) -> str:
    kind = item.get("type")
    if kind == "command_execution":
        command = str(item.get("command") or "")
        # codex wraps each command as: /bin/zsh -lc "<command>"
        if " -lc " in command:
            command = command.split(" -lc ", 1)[1].strip().strip("'\"")
        return f"running {command.splitlines()[0][:40]}" if command else "running"
    if kind == "file_change":
        paths = [os.path.basename(c.get("path", "")) for c in item.get("changes") or []]
        return f"editing {', '.join(p for p in paths if p)[:40]}".strip()
    return kind or ""


def stop(proc: subprocess.Popen) -> None:
    """End codex and everything it started, politely then firmly."""
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
    model, effort, limit, level, attempt, task = sys.argv[1:7]
    codex_model = model.split("/", 1)[1] if model.startswith("codex/") else model
    codex = cline_pause.codex_bin()
    if not codex:
        print("  · the codex CLI is not installed", file=sys.stderr)
        return 127
    os.makedirs(RUNNING, exist_ok=True)
    status_path = os.path.join(RUNNING, f"{os.getpid()}.json")
    status = {
        "pid": os.getpid(), "model": model,
        "label": f"{cline_pause.CODEX_NAME.get(codex_model, codex_model)} · Codex",
        "title": title_of(task), "level": level, "attempt": int(attempt),
        "started": time.time(), "limit": int(limit), "steps": 0, "now": "starting",
        "context": 0, "context_max": CONTEXT.get(codex_model, 0), "cwd": os.getcwd(),
    }

    def publish() -> None:
        tmp = status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(status, handle)
        os.replace(tmp, status_path)

    command = [
        codex, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "-C", os.getcwd(), "-s", "workspace-write", "-m", codex_model,
        "-c", f'model_reasoning_effort="{effort}"', "-c", "notify=[]",
        task,
    ]
    try:
        # stdin closed: with a prompt argument, codex still reads a piped stdin and
        # would wait forever on the open pipe Claude Code's Bash tool leaves.
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                start_new_session=True)
    except OSError as error:
        print(f"  · could not start codex: {error}", file=sys.stderr)
        return 127
    publish()
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
                clock["stopped"], reason = CEILING_EXIT, f"reached the {ceiling // 60}m ceiling"
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
            item = event.get("item") or {}
            if kind == "item.started" and item.get("type") in ("command_execution", "file_change"):
                clock["in_tool"] = item.get("type") == "command_execution"
                status["steps"] += 1
                status["now"] = describe(item)
                print(f"  · step {status['steps']}: {status['now']}", file=sys.stderr, flush=True)
                publish()
            elif kind == "item.completed":
                if item.get("type") == "command_execution":
                    clock["in_tool"] = False
                elif item.get("type") == "file_change" and not status["steps"]:
                    status["steps"] += 1
                elif item.get("type") == "agent_message" and str(item.get("text") or "").strip():
                    final = item["text"]
                    status["now"] = "thinking"
                publish()
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                used = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                if used:
                    status["context"] = used
                publish()
            elif kind == "turn.failed":
                error = str((event.get("error") or {}).get("message") or "")
                pause_kind = cline_pause.codex_classify(error)
                if pause_kind:
                    record = cline_pause.codex_pause(pause_kind, error)
                    print(f"  · Codex refused: {cline_pause.describe(record)}", file=sys.stderr, flush=True)
                    clock["stopped"] = cline_pause.PAUSE_EXIT
                    stop(proc)
                    break
                print(f"  · Codex error: {error[:200]}", file=sys.stderr, flush=True)
                clock["stopped"] = clock["stopped"] or 1
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
