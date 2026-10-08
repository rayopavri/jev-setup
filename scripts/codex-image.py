#!/usr/bin/env python3
"""
codex-image.py -- ask Codex to generate one image and save it to a path.

    python3 codex-image.py "<prompt>" <output.png>

Runs Codex's built-in image tool on the user's ChatGPT plan and copies its newest
PNG to the requested path. Existing files are kept; the next free -v2, -v3, ...
name is used instead.

Exit codes: 0 saved, 2 usage, 4 no image, 75 Codex paused or refused, 142 time limit.
"""

import datetime
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cline_pause  # noqa: E402

TIME_LIMIT = 600
PAUSED = ("== Codex can't take image jobs right now (paused, off or logged out). "
          "Use the OpenRouter image tool instead.")


def stop(proc: subprocess.Popen) -> None:
    """End codex and its process group, then force it down after the grace period."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def output_name(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    parent, filename = os.path.dirname(path), os.path.basename(path)
    os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(filename)
    version = 2
    while True:
        candidate = os.path.join(parent, f"{stem}-v{version}{extension}")
        if not os.path.exists(candidate):
            return candidate
        version += 1


def log_run(code: int, seconds: int, out: str) -> None:
    path = os.path.expanduser(os.environ.get(
        "JEV_ROUTING_LOG", "~/.config/typesafe/jev-routing.jsonl"))
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": "image", "model": f"codex/{cline_pause.CODEX_MODEL}",
        "exit": code, "seconds": seconds, "out": out,
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> int:
    started = time.time()
    out = ""

    def finish(code: int) -> int:
        log_run(code, int(time.time() - started), out)
        return code

    if len(sys.argv) != 3:
        print("Usage: python3 codex-image.py \"<prompt>\" <output.png>", file=sys.stderr)
        return finish(2)
    if not cline_pause.codex_ready():
        print(PAUSED, file=sys.stderr)
        return finish(cline_pause.PAUSE_EXIT)

    prompt, requested = sys.argv[1:]
    target = output_name(requested)
    parent = os.path.dirname(target)
    instructions = (
        "Use your built-in image generation tool exactly once to create this image: "
        f"{prompt}\n\nDo not save, copy, convert or write any files, and do not run any "
        "shell commands: the image is collected automatically. When the image is made, "
        "reply with one sentence describing it."
    )
    command = [
        cline_pause.codex_bin(), "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "-C", parent, "-s", "workspace-write", "-m", cline_pause.CODEX_MODEL,
        "-c", 'model_reasoning_effort="low"', "-c", "notify=[]", instructions,
    ]
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
            cwd=parent,
        )
    except OSError as error:
        print(f"== Codex error: {str(error)[:200]}", file=sys.stderr)
        return finish(1)

    state = {"thread_id": "", "sentence": "", "timed_out": False,
             "refused": False, "failed": False}

    def watchdog() -> None:
        if proc.poll() is None:
            remaining = TIME_LIMIT - (time.time() - started)
            if remaining > 0:
                time.sleep(remaining)
            if proc.poll() is None:
                state["timed_out"] = True
                stop(proc)

    threading.Thread(target=watchdog, daemon=True).start()
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            state["thread_id"] = str(event.get("thread_id") or "")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                state["sentence"] = str(item.get("text") or "").strip()
        elif kind == "turn.failed":
            message = str((event.get("error") or {}).get("message") or "")
            pause_kind = cline_pause.codex_classify(message)
            if pause_kind:
                record = cline_pause.codex_pause(pause_kind, message)
                print(f"== Codex refused: {cline_pause.describe(record)}", file=sys.stderr)
                state["refused"] = True
                stop(proc)
            else:
                print(f"== Codex error: {message[:200]}", file=sys.stderr)
                state["failed"] = True

    proc.wait()
    if state["timed_out"]:
        return finish(142)
    if state["refused"]:
        return finish(cline_pause.PAUSE_EXIT)

    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    image_dir = os.path.join(home, "generated_images", state["thread_id"])
    images = glob.glob(os.path.join(image_dir, "*.png")) if state["thread_id"] else []
    if not images:
        print("== Codex made no image.", file=sys.stderr)
        return finish(4)
    newest = max(images, key=os.path.getmtime)
    try:
        shutil.copy2(newest, target)
    except OSError as error:
        print(f"== Codex error: {str(error)[:200]}", file=sys.stderr)
        return finish(1)
    out = target
    print(out)
    if state["sentence"]:
        print(state["sentence"])
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
