#!/usr/bin/env python3
"""
Stop hook: Jev reviews Claude's finished reply against the user's request.
Fails the bar -> block the stop and hand Claude the feedback to revise.
Passes, errors, or retries used up -> let the reply stand (fail open).

Limit: Claude Code has already shown the draft when this runs. A hook cannot
hide it. Revisions appear as follow-up text in the same turn.
Off switch: JEV_REVIEW=0.   Max retries per turn: JEV_REVIEW_MAX (default 2).
"""
import hashlib
import json
import os
import subprocess
import sys

MAX_RETRIES = int(os.environ.get("JEV_REVIEW_MAX", "2"))
MIN_REPLY_CHARS = 200          # short answers are not worth a judge call
PASS_BAR = 0.60                # a "bad" probability above this fails the reply
STATE_DIR = os.path.expanduser("~/.config/typesafe/review-gate")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
TIMEOUT = float(os.environ.get("JEV_REVIEW_TIMEOUT", "15"))

QUESTIONS = {
    "off_target": {
        "type": "noul",
        "instructions": "Does `reply` fail to do what `user_request` asked, or answer a different question?",
        "criteria": {"true": "Misses, dodges, or only partly answers the request.",
                     "false": "Directly does what was asked."},
    },
    "unverified_claim": {
        "type": "noul",
        "instructions": "Does `reply` claim work is done, fixed, or passing without showing evidence (test output, command result)?",
        "criteria": {"true": "Says done/works/passing with no evidence in the reply.",
                     "false": "Claims are backed by evidence, or no success claim is made."},
    },
    "ignores_style": {
        "type": "noul",
        "instructions": "Is `reply` long, jargon-heavy, or padded when `style` asks for short plain words and only what is needed?",
        "criteria": {"true": "Long, dense, or full of unexplained jargon.",
                     "false": "Short, plain, and only what is needed."},
    },
}
FEEDBACK = {
    "off_target": "Answer exactly what the user asked. Do not dodge or change the question.",
    "unverified_claim": "Show proof for any 'done' or 'works' claim (command output, test result), or say it is unverified.",
    "ignores_style": "Shorten. Use small plain words. Explain any big word. Report only: what you did, did it work, what next.",
}


def last_turn(path: str) -> tuple[str, str]:
    """(last real user prompt, assistant text after it) from the transcript jsonl."""
    prompt, reply = "", []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            content = (row.get("message") or {}).get("content")
            if row.get("type") == "user":
                text = content if isinstance(content, str) else "".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
                if text.strip() and not text.lstrip().startswith(("<system-reminder>", "<task-notification>")):
                    prompt, reply = text, []
            elif row.get("type") == "assistant" and isinstance(content, list):
                reply += [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return prompt, "\n".join(reply)


def judge(prompt: str, reply: str) -> dict:
    binary = os.path.expanduser("~/.local/bin/jev")
    body = {"state": {"user_request": prompt[:4000], "reply": reply[:8000],
                      "style": "short sentences, plain words, only what is needed"},
            "model": MODEL, "questions": QUESTIONS}
    done = subprocess.run([binary, "ask", "-f", "-", "--json"], input=json.dumps(body),
                          capture_output=True, text=True, timeout=TIMEOUT)
    if done.returncode != 0:
        raise RuntimeError("jev ask failed")
    data = json.loads(done.stdout)
    return data.get("answers", data)


def retries_used(key: str) -> int:
    try:
        with open(os.path.join(STATE_DIR, key)) as handle:
            return int(handle.read() or 0)
    except (OSError, ValueError):
        return 0


def main() -> int:
    if os.environ.get("JEV_REVIEW") == "0":
        return 0
    try:
        payload = json.load(sys.stdin)
        prompt, reply = last_turn(payload["transcript_path"])
        if len(reply) < MIN_REPLY_CHARS or not prompt.strip():
            return 0
        key = hashlib.sha1((payload.get("session_id", "") + prompt).encode()).hexdigest()[:16]
        used = retries_used(key)
        if used >= MAX_RETRIES:
            return 0
        answers = judge(prompt, reply)
        failed = [k for k in QUESTIONS if float((answers.get(k) or {}).get("noul") or 0) >= PASS_BAR]
        if not failed:
            return 0
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, key), "w") as handle:
            handle.write(str(used + 1))
        reason = "Jev review: reply is below the bar. Revise it.\n" + "\n".join(f"- {FEEDBACK[k]}" for k in failed)
        print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        return 0  # fail open: a broken reviewer must never trap the session
    return 0


if __name__ == "__main__":
    sys.exit(main())
