#!/usr/bin/env python3
"""
claude-limit.py -- ask before paying for Claude on OpenRouter when Claude runs out.

When a Claude Code turn fails because the usage limit is hit (the plan limit and the
plan's extra usage are both used up), this shows a macOS notification and a dialog
with three choices. Nothing is spent until the user picks one:

  1. Same model on OpenRouter (pay per use): reopen this conversation in Terminal on
     the same Claude model, through OpenRouter.
  2. Cheaper model on OpenRouter: the same, one model down.
  3. Wait for the reset: do nothing.

Three subcommands:

    python3 claude-limit.py hook [--dry-run]
        The StopFailure hook. Reads the hook JSON on stdin, decides whether the error
        is Claude's usage limit, logs it, and starts the chooser detached. Always exits
        0 and prints nothing to stdout, so it can never break a session. --dry-run only
        says on stderr what it would ask, and starts nothing.
    python3 claude-limit.py choose --session S --cwd C --model M --message T
        Shows the notification and the dialog, logs the answer, and on same/cheaper
        opens Terminal running `launch` in the session's folder. Started by `hook`.
    python3 claude-limit.py launch --session S --model SLUG
        Runs in that Terminal window: reads the OpenRouter key (never printed, never on
        a command line) and resumes the conversation with claude, pointed at OpenRouter.

Hook registration (hooks.json):

    "StopFailure": [{"matcher": "rate_limit|billing_error", "hooks": [{"type": "command", "command": "python3 \\"${CLAUDE_PLUGIN_ROOT}/scripts/claude-limit.py\\" hook", "timeout": 10}]}]
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

ROUTING_LOG = os.environ.get("JEV_ROUTING_LOG", os.path.expanduser("~/.config/typesafe/jev-routing.jsonl"))
ROUTING_LOG_MAX = 5_000_000  # bytes; rotated once to .1 past this
STATE_FILE = os.path.expanduser("~/.config/typesafe/claude-limit.json")
PI_AUTH = os.path.expanduser("~/.pi/agent/auth.json")
ASK_AGAIN_AFTER = 600  # seconds: one chooser per 10 minutes
DIALOG_GIVE_UP = 600  # seconds: an unanswered dialog counts as "none"
DEFAULT_MODEL = "claude-opus-5-5"
TRANSCRIPT_TAIL = 2_000_000  # bytes of the transcript's end to scan for the model

LIMIT_ERRORS = ("rate_limit", "billing_error")
LIMIT_TEXT = re.compile(
    r"hit your (session|weekly|opus|sonnet|fable|monthly)? ?(limit|spend limit|usage limit)"
    r"|spend limit|usage limit|usage credits|out of (extra )?usage|credit balance")
NOT_LIMIT_TEXT = ("temporary capacity", "temporarily limiting requests", "529")
RESET_TEXT = re.compile(r"resets?\s+(?:at\s+|in\s+)?([^\u00b7.\n]+)", re.I)

OPENROUTER = {   # Claude Code model id prefix -> (same model on OpenRouter, cheaper model, cheaper label)
    "claude-fable-5-1":  ("anthropic/claude-fable-5.1",  "anthropic/claude-opus-5.5",   "Opus 5.5"),
    "claude-opus-5-5":   ("anthropic/claude-opus-5.5",   "anthropic/claude-sonnet-5.5", "Sonnet 5.5"),
    "claude-sonnet-5-5": ("anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5",  "Haiku 4.5"),
    "claude-sonnet-5":   ("anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5",  "Haiku 4.5"),
    "claude-haiku-4-5":  ("anthropic/claude-haiku-4.5",  "",                           ""),
}
PRICE = {  # $ per million tokens in / out, shown in the dialog
    "anthropic/claude-fable-5.1": (10, 50), "anthropic/claude-opus-5.5": (4, 20),
    "anthropic/claude-sonnet-5.5": (2, 10), "anthropic/claude-haiku-4.5": (1, 5),
}
LABEL = {
    "anthropic/claude-fable-5.1": "Fable 5.1", "anthropic/claude-opus-5.5": "Opus 5.5",
    "anthropic/claude-sonnet-5.5": "Sonnet 5.5", "anthropic/claude-haiku-4.5": "Haiku 4.5",
}
HAIKU = "anthropic/claude-haiku-4.5"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_row(record: dict) -> None:
    """Append one row to the routing log, same way jev-refine.py does. Never raises."""
    try:
        os.makedirs(os.path.dirname(ROUTING_LOG), exist_ok=True)
        if os.path.exists(ROUTING_LOG) and os.path.getsize(ROUTING_LOG) > ROUTING_LOG_MAX:
            os.replace(ROUTING_LOG, ROUTING_LOG + ".1")
        with open(ROUTING_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass


def models_for(model: str) -> tuple[str, str, str]:
    """(same slug, cheaper slug, cheaper label) for a session model id, by longest prefix;
    ('', '', '') when the model is not in the table."""
    best = ""
    for prefix in OPENROUTER:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return OPENROUTER[best] if best else ("", "", "")


def is_limit(data: dict) -> tuple[bool, str]:
    """(is it Claude's usage limit, the error text)."""
    text = f"{data.get('last_assistant_message') or ''} {data.get('error_details') or ''}".strip()
    lower = text.lower()
    if data.get("error") not in LIMIT_ERRORS:
        return False, text
    if any(word in lower for word in NOT_LIMIT_TEXT):
        return False, text
    return bool(LIMIT_TEXT.search(lower)), text


def session_model(transcript_path: str) -> str:
    """The claude-* model id of the last assistant turn in the transcript, else the default."""
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return DEFAULT_MODEL
    for line in reversed(lines):
        try:
            model = ((json.loads(line) or {}).get("message") or {}).get("model")
        except Exception:
            continue
        if isinstance(model, str) and model.startswith("claude-"):
            return model
    return DEFAULT_MODEL


def asked_recently() -> bool:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return time.time() - float(json.load(handle).get("asked") or 0) < ASK_AGAIN_AFTER
    except Exception:
        return False


def mark_asked() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"asked": time.time()}, handle)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


# ---- hook ---------------------------------------------------------------------------

def hook(dry_run: bool) -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return
    if not isinstance(data, dict):
        return
    limit, text = is_limit(data)
    if not limit:
        return
    model = session_model(str(data.get("transcript_path") or ""))
    same, cheaper, _ = models_for(model)
    session = str(data.get("session_id") or "")
    cwd = str(data.get("cwd") or "")
    row = {"ts": now_iso(), "event": "claude_limit", "error": data.get("error"),
           "message": text[:300], "model": model, "choice": "pending", "cwd": cwd,
           "session_id": session}
    if dry_run:
        log_row(row)
        reset = RESET_TEXT.search(text)
        print(f"would ask: model={model} same={same or '?'} cheaper={cheaper or '?'} "
              f"reset={reset.group(1).strip() if reset else '?'}", file=sys.stderr)
        return
    if asked_recently():
        row["choice"] = "skipped_recent"
        log_row(row)
        return
    log_row(row)
    mark_asked()
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "choose", f"--session={session}", f"--cwd={cwd}",
         f"--model={model}", f"--message={text[:300]}"],  # "=" form: values may start with "-"
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


# ---- choose -------------------------------------------------------------------------

def as_str(text: str) -> str:
    """An AppleScript string expression for text: backslashes and quotes escaped,
    newlines turned into `return`."""
    parts = []
    for piece in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        parts.append('"' + piece.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return " & return & ".join(parts)


def osascript(*scripts: str, timeout: float | None = None) -> str:
    """Run AppleScript lines (one -e each) without a shell; returns stdout, '' on failure."""
    args = ["osascript"]
    for script in scripts:
        args += ["-e", script]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    return done.stdout if done.returncode == 0 else ""


def price_text(slug: str) -> str:
    price_in, price_out = PRICE.get(slug, (0, 0))
    return f"${price_in}/${price_out} per M"


def choose(session: str, cwd: str, model: str, message: str) -> None:
    same, cheaper, cheaper_label = models_for(model)
    short = message.replace("\n", " ")[:120]
    osascript(f"display notification {as_str(short)} with title {as_str('Jev: Claude hit its limit')}")
    if not same:
        log_row({"ts": now_iso(), "event": "claude_limit", "choice": "none", "model": model})
        return

    wait = "Wait for reset"
    same_button = f"{LABEL[same]} on OpenRouter ({price_text(same)})"
    cheaper_button = f"{cheaper_label} on OpenRouter ({price_text(cheaper)})" if cheaper else ""
    buttons = [wait] + ([cheaper_button] if cheaper else []) + [same_button]
    body = ("Claude and your plan's extra usage are used up.\n\n"
            f"{message}\n\nNothing is spent until you choose.")
    button_list = "{" + ", ".join(as_str(b) for b in buttons) + "}"
    answer = osascript(
        f"display dialog {as_str(body)} buttons {button_list} default button {as_str(wait)} "
        f"with title {as_str('Jev')} giving up after {DIALOG_GIVE_UP}",
        timeout=DIALOG_GIVE_UP + 60)
    pressed = ""
    match = re.search(r"button returned:(.*?)(?:, gave up:(true|false))?\s*$", answer.strip())
    if match and match.group(2) != "true":
        pressed = match.group(1).strip()
    if cheaper and pressed == cheaper_button:
        choice, slug = "cheaper", cheaper
    elif pressed == same_button:
        choice, slug = "same", same
    elif pressed == wait:
        choice, slug = "wait", ""
    else:
        choice, slug = "none", ""
    log_row({"ts": now_iso(), "event": "claude_limit", "choice": choice, "model": model})
    if not slug:
        return

    command = (f"cd {shlex.quote(cwd or os.path.expanduser('~'))} && "
               f"python3 {shlex.quote(os.path.abspath(__file__))} launch "
               f"--session {shlex.quote(session)} --model {shlex.quote(slug)}")
    osascript(f'tell application "Terminal" to do script {as_str(command)}',
              'tell application "Terminal" to activate')


# ---- launch -------------------------------------------------------------------------

def openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    try:
        with open(PI_AUTH, encoding="utf-8") as handle:
            return str((json.load(handle).get("openrouter") or {}).get("key") or "")
    except Exception:
        return ""


def launch(session: str, slug: str) -> None:
    key = openrouter_key()
    if not key:
        print("No OpenRouter key found -- run set-backup-key.py openrouter")
        sys.exit(1)
    print(f"Continuing this conversation on {LABEL.get(slug, slug)} through OpenRouter "
          "(pay per use). Close this window to stop.")
    env = dict(os.environ)
    env.update({
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": slug,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": slug,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": slug,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU,
        "CLAUDE_CODE_SUBAGENT_MODEL": slug,
    })
    argv = ["claude", "--resume", session] if session else ["claude"]
    os.execvpe("claude", argv, env)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask before paying for Claude on OpenRouter.")
    sub = parser.add_subparsers(dest="command", required=True)
    hook_parser = sub.add_parser("hook")
    hook_parser.add_argument("--dry-run", action="store_true")
    choose_parser = sub.add_parser("choose")
    for name in ("session", "cwd", "model", "message"):
        choose_parser.add_argument(f"--{name}", default="")
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--session", default="")
    launch_parser.add_argument("--model", required=True)

    if sys.argv[1:2] == ["hook"]:  # a hook must never break the session
        try:
            args, _ = parser.parse_known_args()
            hook(args.dry_run)
        except BaseException:
            pass
        return 0
    args = parser.parse_args()
    if args.command == "choose":
        choose(args.session, args.cwd, args.model, args.message)
    elif args.command == "launch":
        launch(args.session, args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
