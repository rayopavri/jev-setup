#!/usr/bin/env python3
"""
set-backup-key.py -- switch on a pay-as-you-go backup for the ClinePass lane.

    python3 set-backup-key.py cline        # Cline credits: key from app.cline.bot -> Settings -> API Keys
    python3 set-backup-key.py openrouter   # OpenRouter: key from openrouter.ai -> Settings -> Keys

1. Gets the key, typed or pasted (it does not show on screen). For OpenRouter it offers
   to reuse the key already saved in the Cline app.
2. Checks the key with the service.
3. Saves it for pi in ~/.pi/agent/auth.json (private to your user account).
4. Has each of the service's models -- a cheap and a full one per lane family -- do a
   tiny tool-use task (check-tools.sh --backup, roughly 30 cents), and switches the backup
   on for the models that pass.

While ClinePass is paused, lane jobs then run pay-as-you-go: Cline credits first, then
OpenRouter, the cheap model of each family first; when the pause lifts, back to
ClinePass. To turn one off:  python3 cline_pause.py backup off <cline|openrouter>
"""

import getpass
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cline_pause  # noqa: E402

CLINE_APP_SETTINGS = os.path.expanduser("~/.cline/data/settings/providers.json")


def call(url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(url, data=data, method="POST" if body else "GET",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def check_openrouter(key: str) -> str:
    info = call("https://openrouter.ai/api/v1/key", key).get("data") or {}
    limit, used = info.get("limit"), float(info.get("usage") or 0)
    text = ("Key works. It has no spending limit -- you can add one at openrouter.ai/settings/keys."
            if limit is None else f"Key works. Spending limit on this key: ${float(limit):.2f} (used ${used:.2f}).")
    try:
        credits = call("https://openrouter.ai/api/v1/credits", key).get("data") or {}
        left = float(credits.get("total_credits") or 0) - float(credits.get("total_usage") or 0)
        text += f" Credit left on the account: ${left:.2f}."
    except (OSError, ValueError):
        pass
    return text


def check_cline(key: str) -> str:
    # Cline has no key-info endpoint, so ask the cheapest model for one word (~$0.00003).
    reply = call("https://api.cline.bot/api/v1/chat/completions", key,
                 {"model": "qwen/qwen3.7-flash", "max_tokens": 5,
                  "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
    reply = reply.get("data", reply)  # Cline wraps non-streamed replies in {"data": ...}
    if not reply.get("choices"):
        raise ValueError(f"unexpected reply: {json.dumps(reply)[:200]}")
    return "Key works, and the account has Cline credit (a one-word test cost a tiny fraction of a cent)."


def saved_openrouter_key() -> str:
    try:
        with open(CLINE_APP_SETTINGS, encoding="utf-8") as handle:
            return json.load(handle)["providers"]["openrouter"]["settings"]["apiKey"].strip()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""


def save(provider: str, key: str) -> None:
    """Add the key to pi's auth store, keeping everything else in it (the ClinePass login)."""
    path = cline_pause.PI_AUTH
    try:
        with open(path, encoding="utf-8") as handle:
            auth = json.load(handle)
    except FileNotFoundError:
        auth = {}
    auth[cline_pause.BACKUPS[provider]["auth"]] = {"type": "api_key", "key": key}
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(auth, handle, indent=2)
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    provider = argv[0] if argv else ""
    if provider not in cline_pause.BACKUPS:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    label = cline_pause.BACKUPS[provider]["label"]

    key = ""
    if provider == "openrouter" and saved_openrouter_key():
        answer = input("Your Cline app already has an OpenRouter key saved. Use that one? [Y/n] ").strip().lower()
        key = saved_openrouter_key() if answer in ("", "y", "yes") else ""
    if not key:
        where = "app.cline.bot -> Settings -> API Keys" if provider == "cline" else "openrouter.ai -> Settings -> Keys"
        key = getpass.getpass(f"Paste your {label} API key from {where} (it won't show), then press Enter: ").strip()
    if not key:
        print("No key entered. Nothing changed.")
        return 1

    try:
        print(check_cline(key) if provider == "cline" else check_openrouter(key))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        if error.code == 402 or re.search(r"insufficient|credit|balance", detail, re.I):
            print(f"The key works, but {label} says there is no credit to spend ({detail}). "
                  f"Add credit, then run this again. Nothing saved.")
        else:
            print(f"{label} didn't accept that key (HTTP {error.code}: {detail}). Nothing saved.")
        return 1
    except (OSError, ValueError) as error:
        print(f"Couldn't check the key with {label} ({error}). Nothing saved.")
        return 1

    save(provider, key)
    print("Saved for pi.\n")

    print(f"Checking that each model can use its tools on {label} (about a minute, roughly 30 cents)...")
    check = subprocess.run(["sh", os.path.join(HERE, "check-tools.sh"), "--backup", provider],
                           capture_output=True, text=True)
    print(check.stdout.strip())
    passed = [line.split()[1] for line in check.stdout.splitlines() if line.startswith("PASS")]
    if not passed:
        print(f"\nNo model passed, so {label} stays off. Ask Claude to look at the logs above.")
        return 1
    cline_pause.backup_start(provider, passed)
    print("\n" + cline_pause.backup_status())
    print("Backups take over only while ClinePass is paused, and hand back when the pause lifts.")
    missing = [cline_pause.model_name(m) for m in cline_pause.backup_models(provider) if m not in passed]
    if missing:
        print(f"Left out on {label} (failed the tool check): " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
