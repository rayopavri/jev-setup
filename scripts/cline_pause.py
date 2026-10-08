#!/usr/bin/env python3
"""
cline_pause.py -- the ClinePass pause switch, and its pay-as-you-go backups.

When ClinePass refuses work because the plan is out of allowance, the subscription
has lapsed, or the login expired, every further job would bounce straight back. So
the first refusal pauses the whole ClinePass lane: Jev routes to Claude models
instead, and the status line says why and until when. The pause lifts by itself,
at the reset time the refusal names when it names one.

The user's rule (2026-10-04): a model that hits its limit is not swapped for a
different or cheaper one. While ClinePass is paused, its jobs run on the SAME model
pay-as-you-go: Cline credits first, then OpenRouter when those run out. The Codex lane
(the user's ChatGPT plan, through the Codex CLI) has its own pause and the same rule:
GPT-6-Luna pay-as-you-go while it is paused. When a pause lifts, work goes back to the
free lane.

    python3 cline_pause.py status               # exit 0 + message if paused, 1 if not
    python3 cline_pause.py status --line        # the same, as the status line shows it
    python3 cline_pause.py clear                # lift it now (e.g. after `pi /login`)
    python3 cline_pause.py codex                # the Codex lane (exit 0 if it can take work)
    python3 cline_pause.py codex clear          # lift a Codex pause now (e.g. after `codex login`)
    python3 cline_pause.py backup               # the backups (exit 0 if any is on)
    python3 cline_pause.py backup on|off <cline|openrouter>
    python3 cline_pause.py backup order <first> <second>

Imported by pi-progress.py and codex-progress.py (set it), cline-worker.sh (checks
it), jev-refine.py and delegate.py (route around it), and the status line (shows it).
"""

import json
import os
import re
import shutil
import sys
import time

PAUSE_FILE = os.path.expanduser("~/.config/typesafe/cline-pause.json")
PAUSE_EXIT = 75  # EX_TEMPFAIL: ClinePass refused; don't retry on another ClinePass model

# Same classes, same match words as the provider's own classifier
# (pi-clinepass-provider/src/errors.ts). Rate limits clear on their own; the other
# two need the user (renew, or `pi /login`), so they pause for a day and say so.
KINDS = {
    "rate_limited": (["429", "rate limit", "too many requests", "rate_limit"],
                     int(os.environ.get("CLINE_PAUSE_MINUTES", "60")) * 60,
                     "ClinePass usage limit reached"),
    "not_subscribed": (["403", "forbidden", "subscription required", "not subscribed"],
                       24 * 3600, "ClinePass subscription needed (app.cline.bot)"),
    "auth_expired": (["401", "unauthorized", "invalid api key", "invalid_api_key"],
                     24 * 3600, "ClinePass login expired (run: pi /login)"),
}

# How long a refusal says to wait: "The limit resets in 4h 50m". ClinePass caps run in
# 5-hour windows, so a fixed hour would keep probing a lane that is still shut.
RESET_IN = re.compile(r"resets? in\s+(?:(\d+)\s*d\w*\s*)?(?:(\d+)\s*h\w*\s*)?(?:(\d+)\s*m\w*)?", re.I)
RESET_MARGIN = 120  # seconds past the stated reset, so the first job back isn't early

# Pay-as-you-go backups, tried in order. Both sell the same models under the same IDs
# (Cline's pay-as-you-go catalog is OpenRouter's), billed per token. Cline credits
# are billed to the same Cline account as ClinePass and are not held back by its
# 5-hour cap. Each backup is off until set-backup-key.py stores its key and its models
# pass check-tools.sh; one that refuses (out of credit, key problem) switches itself
# off and the next one takes the job.
BACKUPS = {
    "cline": {"label": "Cline credits", "prefix": "cline-credits/", "auth": "cline-credits",
              "env": "CLINE_CREDITS_API_KEY"},
    "openrouter": {"label": "OpenRouter", "prefix": "openrouter/", "auth": "openrouter",
                   "env": "OPENROUTER_API_KEY"},
}
BACKUP_ORDER = ["cline", "openrouter"]  # `backup order openrouter cline` flips it
# Small first (the user's rule, 2026-10-05, replacing "same model, never cheaper"):
# pay-as-you-go tries the family's cheap model first ("cheap"); if that falls short the
# worker runs the full model ("same"), and a redo that asks for more (--escalate) goes
# straight to the family's strongest ("up").
BACKUP_SAME = {
    "cline-pass/qwen3.8-max":     "qwen/qwen3.8-max-0902",
    "cline-pass/qwen3.7-max":     "qwen/qwen3.7-max",
    "cline-pass/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "cline-pass/kimi-k3":         "moonshotai/kimi-k3",
    "codex/gpt-6-luna":           "openai/gpt-6-luna",
}
BACKUP_TIERS = {
    #  ClinePass model:           (cheap model, tried first,      full model, if needed)
    "cline-pass/qwen3.8-max":     ("qwen/qwen3.8-flash",          "qwen/qwen3.8-max-0902"),
    "cline-pass/qwen3.7-max":     ("qwen/qwen3.7-flash",          "qwen/qwen3.7-max"),
    # DeepSeek goes straight to V4 Pro pay-as-you-go (the user's rule, 2026-10-05): on
    # OpenRouter Pro ($0.21/$0.42) undercut Flash ($0.03/$1.28) per job that day. If
    # Pro's price rises again, put deepseek-v4-flash back as the cheap first try.
    "cline-pass/deepseek-v4-pro": ("deepseek/deepseek-v4-pro",    "deepseek/deepseek-v4-pro"),
    # Kimi stays on K3 even pay-as-you-go (the user's rule, 2026-10-05): K2.7 Code has a
    # quarter of the context and weaker scores, so it is never the small first try.
    "cline-pass/kimi-k3":         ("moonshotai/kimi-k3",          "moonshotai/kimi-k3"),
    # The Codex lane's family on OpenRouter: the same GPT-6-Luna, per token, then Sol.
    "codex/gpt-6-luna":           ("openai/gpt-6-luna",           "openai/gpt-6-sol"),
}
BACKUP_NAME = {
    "qwen/qwen3.8-flash": "Qwen3.8 Flash",
    "qwen/qwen3.8-max-0902": "Qwen3.8 Max",
    "qwen/qwen3.7-flash": "Qwen3.7 Flash",
    "qwen/qwen3.7-max": "Qwen3.7 Max",
    "deepseek/deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "moonshotai/kimi-k2.7-code": "Kimi K2.7 Code",
    "moonshotai/kimi-k3": "Kimi K3",
    "openai/gpt-6-luna": "GPT-6-Luna",
    "openai/gpt-6-sol": "GPT-6-Sol",
}
UPSTREAM = [model for pair in BACKUP_TIERS.values() for model in pair]
BACKUP_FILE = os.path.expanduser("~/.config/typesafe/cline-backup.json")
PI_AUTH = os.path.expanduser("~/.pi/agent/auth.json")
# Refusals that retrying can't fix: out of credit, a key's own spending limit, a
# revoked key, a cap. The backup that gave it switches itself off and says why.
BACKUP_STOP = re.compile(r"\b40[12]\b|insufficient|more credits|out of credits?|\bbalance\b|key limit"
                         r"|unauthorized|invalid api key|no auth credentials|user not found|inference_cap", re.I)

# The Codex lane: the user's ChatGPT plan, through `codex exec` (codex-progress.py).
# It is tried before any pay-as-you-go backup, because it costs nothing per job; it
# shares its 5-hour and weekly allowance with the user's own Codex use. The model
# must be one the ChatGPT login accepts (gpt-5.6-sol, for one, is API-only).
CODEX_ON = os.environ.get("JEV_CODEX", "1") != "0"
CODEX_MODEL = os.environ.get("JEV_CODEX_MODEL", "gpt-6-luna")
CODEX_NAME = {"gpt-6-luna": "GPT-6-Luna", "gpt-5.6-terra": "GPT-5.6-Terra",
              "gpt-5.6-luna": "GPT-5.6-Luna", "gpt-5.5": "GPT-5.5"}
CODEX_LABEL = "your Codex subscription"
CODEX_PAUSE_FILE = os.path.expanduser("~/.config/typesafe/codex-pause.json")
CODEX_AUTH = os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "auth.json")
# Only a turn that failed outright is classified; codex also emits "Reconnecting...
# 401" events on the way, and those are not refusals yet.
CODEX_KINDS = {
    "rate_limited": (["usage limit", "rate limit", "429", "too many requests", "quota"],
                     int(os.environ.get("CODEX_PAUSE_MINUTES", "60")) * 60,
                     "Codex usage limit reached"),
    "auth_expired": (["401", "unauthorized", "not logged in", "log in again", "refresh token"],
                     24 * 3600, "Codex login expired (run: codex login)"),
}
# "try again in 2 days 4 hours 3 minutes", or "try again at 3:45 PM" / "at Oct 3rd, 2026 3:45 PM".
CODEX_IN = re.compile(r"(?:try again|resets?) in\s+(?:(\d+)\s*(?:days?|d)\b[\s,]*)?"
                      r"(?:(\d+)\s*(?:hours?|hrs?|h)\b[\s,]*)?(?:(\d+)\s*(?:minutes?|mins?|m)\b)?", re.I)
CODEX_AT = re.compile(r"try again at\s+([^.]+)", re.I)


def classify(error_message: str) -> str:
    """The pause kind for a provider error, or '' for an ordinary one-off failure."""
    lower = (error_message or "").lower()
    for kind, (words, _, _) in KINDS.items():
        if any(word in lower for word in words):
            return kind
    return ""


def reset_seconds(detail: str) -> int:
    """Seconds until the limit resets, as the refusal states it; 0 if it doesn't say."""
    match = RESET_IN.search(detail or "")
    if not match:
        return 0
    days, hours, minutes = (int(group or 0) for group in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60


def _write(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(tmp, path)


def pause(kind: str, detail: str = "") -> dict:
    _, seconds, reason = KINDS[kind]
    told = reset_seconds(detail) if kind == "rate_limited" else 0
    if told:
        seconds = told + RESET_MARGIN
    record = {"kind": kind, "reason": reason, "detail": detail[:200],
              "since": time.time(), "until": time.time() + seconds}
    _write(PAUSE_FILE, record)
    return record


def active() -> dict:
    """The current pause, or {} when ClinePass is open. An expired pause is removed."""
    try:
        with open(PAUSE_FILE, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {}
    if float(record.get("until") or 0) <= time.time():
        clear()
        return {}
    return record


def clear() -> None:
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass


def until_text(record: dict) -> str:
    """'4:30 PM' today, 'Fri 12:04 PM' on another day (weekly caps run for days)."""
    when = time.localtime(float(record["until"]))
    same_day = time.strftime("%Y-%m-%d", when) == time.strftime("%Y-%m-%d")
    return time.strftime("%-I:%M %p" if same_day else "%a %-I:%M %p", when)


def describe(record: dict) -> str:
    return f"{record.get('reason', 'ClinePass paused')} -- paused until {until_text(record)}"


def codex_bin() -> str:
    """The codex CLI, found on PATH or where its installer puts it; '' if missing."""
    found = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
    return found if os.access(found, os.X_OK) else ""


def codex_classify(error_message: str) -> str:
    """The Codex pause kind for a failed turn, or '' for an ordinary failure."""
    lower = (error_message or "").lower()
    for kind, (words, _, _) in CODEX_KINDS.items():
        if any(word in lower for word in words):
            return kind
    return ""


def codex_reset_seconds(detail: str) -> int:
    """Seconds until Codex's limit resets, as its refusal states it; 0 if it doesn't say."""
    match = CODEX_IN.search(detail or "")
    if match and any(match.groups()):
        days, hours, minutes = (int(group or 0) for group in match.groups())
        return days * 86400 + hours * 3600 + minutes * 60
    match = CODEX_AT.search(detail or "")
    if not match:
        return 0
    when = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", match.group(1)).strip()
    now = time.localtime()
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d %I:%M %p", "%I:%M %p", "%H:%M"):
        try:
            parsed = time.strptime(when, fmt)
        except ValueError:
            continue
        year = parsed.tm_year if "%Y" in fmt else now.tm_year
        month, day = (parsed.tm_mon, parsed.tm_mday) if "%d" in fmt else (now.tm_mon, now.tm_mday)
        target = time.mktime((year, month, day, parsed.tm_hour, parsed.tm_min, 0, 0, 0, -1))
        if "%d" not in fmt and target <= time.time():
            target += 86400  # a bare time already past today means tomorrow
        return max(0, int(target - time.time()))
    return 0


def codex_pause(kind: str, detail: str = "") -> dict:
    _, seconds, reason = CODEX_KINDS[kind]
    told = codex_reset_seconds(detail) if kind == "rate_limited" else 0
    if told:
        seconds = told + RESET_MARGIN
    record = {"kind": kind, "reason": reason, "detail": detail[:200],
              "since": time.time(), "until": time.time() + seconds}
    _write(CODEX_PAUSE_FILE, record)
    return record


def codex_active() -> dict:
    """The current Codex pause, or {} when it is open. An expired pause is removed."""
    try:
        with open(CODEX_PAUSE_FILE, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {}
    expired = float(record.get("until") or 0) <= time.time()
    try:  # a fresh `codex login` since the refusal lifts a login pause at once
        relogged = (record.get("kind") == "auth_expired"
                    and os.path.getmtime(CODEX_AUTH) > float(record.get("since") or 0))
    except OSError:
        relogged = False
    if expired or relogged:
        codex_clear()
        return {}
    return record


def codex_clear() -> None:
    try:
        os.remove(CODEX_PAUSE_FILE)
    except OSError:
        pass


def codex_ready() -> bool:
    """The Codex lane can take work: switched on, installed, logged in, not paused."""
    return CODEX_ON and bool(codex_bin()) and os.path.exists(CODEX_AUTH) and not codex_active()


def codex_name() -> str:
    return CODEX_NAME.get(CODEX_MODEL, CODEX_MODEL)


def codex_status() -> str:
    if not CODEX_ON:
        return "Codex lane: off (JEV_CODEX=0)"
    if not codex_bin():
        return "Codex lane: codex CLI not found"
    if not os.path.exists(CODEX_AUTH):
        return "Codex lane: not logged in (run: codex login)"
    record = codex_active()
    if record:
        return f"Codex lane: {describe(record)} -- then: python3 {os.path.abspath(__file__)} codex clear"
    return f"Codex lane: on ({codex_name()}, on your ChatGPT plan)"


def _state() -> dict:
    try:
        with open(BACKUP_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state.get("providers"), dict):
        state["providers"] = {}
    return state


def backup_order() -> list[str]:
    order = [p for p in _state().get("order", []) if p in BACKUPS]
    return order + [p for p in BACKUP_ORDER if p not in order]


def backup_key(provider: str) -> bool:
    """Whether pi has this backup's key: in its auth store, or in the environment."""
    spec = BACKUPS[provider]
    if os.environ.get(spec["env"]):
        return True
    try:
        with open(PI_AUTH, encoding="utf-8") as handle:
            return spec["auth"] in json.load(handle)
    except (OSError, ValueError):
        return False


def provider_on(provider: str) -> bool:
    """This backup can take work: switched on, not stopped by a refusal, key stored."""
    entry = _state()["providers"].get(provider) or {}
    return bool(entry.get("models")) and not entry.get("stopped") and backup_key(provider)


def backups_on() -> list[str]:
    return [p for p in backup_order() if provider_on(p)]


def backup_on() -> bool:
    return bool(backups_on())


def backup_where() -> str:
    """Where pay-as-you-go work goes: 'OpenRouter (Cline credits if that runs out)', or ''."""
    labels = [BACKUPS[p]["label"] for p in backups_on()]
    if len(labels) > 1:
        return f"{labels[0]} ({', then '.join(labels[1:])} if that runs out)"
    return labels[0] if labels else ""


def backup_steps(model: str, escalate: bool = False) -> str:
    """'Qwen3.8 Flash, then Qwen3.8 Max' -- the models a lane job runs on pay-as-you-go,
    small first -- or ''."""
    if escalate:
        return model_name(backup_model(model, "up"))
    names = []
    for tier in ("cheap", "same"):
        name = model_name(backup_model(model, tier))
        if name and name not in names:
            names.append(name)
    return names[0] + (f", then {names[1]} if it falls short" if len(names) > 1 else "") if names else ""


def backup_model(model: str, tier: str = "same") -> str:
    """The pi model to run in place of a lane model: the same model ("same"), the
    family's strongest ("up" / "full"), or its cheap one ("cheap"), on the first backup
    that is on and whose model passed check-tools.sh; '' if none."""
    family = paid_family(model)
    pair = BACKUP_TIERS.get(family)
    if not pair:
        return ""
    upstream = {"same": BACKUP_SAME.get(family, pair[1]), "cheap": pair[0]}.get(tier, pair[1])
    providers = _state()["providers"]
    for p in backups_on():
        if upstream in (providers.get(p) or {}).get("models", []):
            return BACKUPS[p]["prefix"] + upstream
    return ""


def provider_of(pi_model: str) -> str:
    return next((p for p, spec in BACKUPS.items() if pi_model.startswith(spec["prefix"])), "")


def model_name(pi_model: str) -> str:
    """'Qwen3.8 Flash' for cline-credits/qwen/qwen3.8-flash; '' for no model."""
    provider = provider_of(pi_model)
    upstream = pi_model[len(BACKUPS[provider]["prefix"]):] if provider else pi_model
    return BACKUP_NAME.get(upstream, upstream.split("/")[-1]) if upstream else ""


def backup_models(provider: str) -> list[str]:
    return [BACKUPS[provider]["prefix"] + upstream for upstream in UPSTREAM]


def backup_start(provider: str, models: list[str]) -> None:
    """Switch one backup on for these models (pi models or bare upstream IDs)."""
    prefix = BACKUPS[provider]["prefix"]
    upstream = [m[len(prefix):] if m.startswith(prefix) else m for m in models]
    state = _state()
    state["providers"][provider] = {"models": [m for m in upstream if m in UPSTREAM], "since": time.time()}
    _write(BACKUP_FILE, state)


def backup_stop(pi_model: str, detail: str) -> str:
    """Switch off the backup that refused this model's job. Returns its label."""
    provider = provider_of(pi_model)
    state = _state()
    entry = state["providers"].get(provider)
    if entry and not entry.get("stopped"):
        entry.update({"stopped": time.time(), "reason": detail[:200]})
        _write(BACKUP_FILE, state)
    return BACKUPS[provider]["label"] if provider else "the backup"


def backup_off(provider: str = "") -> None:
    state = _state()
    for p in [provider] if provider else list(state["providers"]):
        state["providers"].pop(p, None)
    _write(BACKUP_FILE, state)


def backups_stopped() -> list[str]:
    """Labels of the backups a refusal switched off (out of credit, key problem)."""
    providers = _state()["providers"]
    return [BACKUPS[p]["label"] for p in backup_order() if (providers.get(p) or {}).get("stopped")]


def backup_refusal(error_message: str) -> bool:
    # One upstream model being rate-limited ("temporarily rate-limited upstream") is
    # not the account refusing: it must not switch the whole backup off.
    if re.search(r"\b429\b|rate.?limited upstream|temporarily rate.?limited", error_message or "", re.I):
        return False
    return bool(BACKUP_STOP.search(error_message or ""))


def backup_status() -> str:
    state = _state()
    lines = []
    for p in backup_order():
        label, entry = BACKUPS[p]["label"], state["providers"].get(p)
        if not entry:
            lines.append(f"{label}: off")
        elif entry.get("stopped"):
            lines.append(f"{label}: stopped ({entry.get('reason', '')[:100]}) -- top up or fix the "
                         f"key, then: python3 {os.path.abspath(__file__)} backup on {p}")
        elif not backup_key(p):
            lines.append(f"{label}: switched on, but pi has no key for it (run set-backup-key.py {p})")
        else:
            lines.append(f"{label}: on ({', '.join(BACKUP_NAME.get(m, m) for m in entry['models'])})")
    order = " -> ".join(BACKUPS[p]["label"] for p in backup_order())
    return (f"{codex_status()}\n"
            f"Pay-as-you-go backups, used only while a job's own free lane is down, running the "
            f"same model, in this order: {order}\n  " + "\n  ".join(lines))


def status_line(record: dict) -> str:
    """ClinePass is paused (record): say where its jobs go instead."""
    codex = codex_active()
    why = f"; {describe(codex)}" if codex else ""
    where = backup_where()
    if where:
        return (f"{describe(record)}{why}. ClinePass jobs run on the same model, pay-as-you-go "
                f"on {where}, until then.")
    stopped = backups_stopped()
    if stopped:
        return (f"{describe(record)}{why}. Pay-as-you-go stopped on {', '.join(stopped)} (credit or key), "
                f"so Jev is routing to Claude models.")
    return f"{describe(record)}{why}. Jev is routing to Claude models."


# ---- Lanes: which free lane takes a job now, and when pay-as-you-go steps in -------
# Jev picks a lane model: a ClinePass model (cline-pass/...) or the Codex lane
# (codex/...). The job runs on that lane if it is up. If that lane is down (paused on
# a limit or login), the job runs on the SAME model pay-as-you-go -- Cline credits,
# then OpenRouter -- never on a different or cheaper model (the user's rule). While
# the lane is up, a run that fails outright may move on to the other free lane once.
# Ordinary failures never go pay-as-you-go: after two attempts the job goes to Claude.
CLINE_FALLBACK = os.environ.get("CLINE_FALLBACK_MODEL", "cline-pass/kimi-k3")
CODEX_PEER = "cline-pass/deepseek-v4-pro"  # ClinePass's stand-in for a Codex job
CLINE_NAME = {"cline-pass/qwen3.8-max": "Qwen3.8 Max", "cline-pass/qwen3.7-max": "Qwen3.7 Max",
              "cline-pass/deepseek-v4-pro": "DeepSeek V4 Pro", "cline-pass/kimi-k3": "Kimi K3",
              "cline-pass/deepseek-v4.1-flash": "DeepSeek V4.1 Flash", "cline-pass/qwen3.7-plus": "Qwen3.7 Plus"}
# Small first on ClinePass too (2026-10-05): the smaller sibling ClinePass offers for a
# lane model, tried before it to stretch the ClinePass limit. Only same-family siblings,
# and only ones that passed check-tools.sh. Qwen3.8 Max and Kimi K3 have none there.
CLINE_SMALL = {"cline-pass/deepseek-v4-pro": "cline-pass/deepseek-v4.1-flash",
               "cline-pass/qwen3.7-max": "cline-pass/qwen3.7-plus"}


def small_first(step: str) -> list[str]:
    """[small sibling, step] for a ClinePass model that has one, else [step]."""
    small = CLINE_SMALL.get(step)
    return [small, step] if small else [step]


def codex_lane_model() -> str:
    return f"codex/{CODEX_MODEL}"


def paid_family(model: str) -> str:
    """The BACKUP_TIERS key for a lane model: the Codex lane maps to its GPT family."""
    if model.startswith("codex/"):
        return "codex/gpt-6-luna"
    return model


def free_steps(model: str) -> list[str]:
    """The free lanes that can take this job right now, in the order to try them.
    Empty when the picked lane itself is down: then the same model goes pay-as-you-go."""
    cline_open = not active()
    codex = codex_lane_model() if codex_ready() else ""
    if not (codex if model.startswith("codex/") else cline_open):
        return []
    if model.startswith("codex/"):
        steps = [codex, CODEX_PEER if cline_open else ""]
    else:
        fallback = CLINE_FALLBACK if CLINE_FALLBACK != model else CODEX_PEER
        steps = [model if cline_open else "", codex, fallback if cline_open else ""]
    return [step for step in steps if step]


def step_label(step: str) -> str:
    """'GPT-6-Luna on your Codex subscription', 'Kimi K3 on ClinePass', 'Qwen3.8 Flash on OpenRouter'."""
    if step.startswith("codex/"):
        return f"{CODEX_NAME.get(step[6:], step[6:])} on {CODEX_LABEL}"
    if step.startswith("cline-pass/"):
        return f"{CLINE_NAME.get(step, step.split('/')[-1])} on ClinePass"
    provider = provider_of(step)
    return f"{model_name(step)} on {BACKUPS[provider]['label']}" if provider else step


def lane_where(model: str, escalate: bool = False) -> str:
    """Where a job for this lane model runs now, for Claude and the user; '' = nowhere,
    so it stays with Claude. Names the small model first when there is one."""
    free = free_steps(model)
    if free:
        first = free[0] if escalate else small_first(free[0])
        if len(first) > 1:
            return f"{step_label(first[0])}, then {CLINE_NAME.get(first[1], first[1])} if it falls short"
        return step_label(first[0])
    steps = backup_steps(model, escalate)
    return f"{steps}, pay-as-you-go on {paid_where(model, escalate)}" if steps else ""


def paid_where(model: str, escalate: bool = False) -> str:
    """The backups that would run this lane model, in order: 'Cline credits (OpenRouter
    if that runs out)'. Only the ones that carry that exact model."""
    family = paid_family(model)
    pair = BACKUP_TIERS.get(family) or ("", "")
    wanted = [pair[1]] if escalate else [pair[0], BACKUP_SAME.get(family, pair[1])]
    providers = _state()["providers"]
    labels = [BACKUPS[p]["label"] for p in backups_on()
              if set(wanted) & set((providers.get(p) or {}).get("models", []))]
    if len(labels) > 1:
        return f"{labels[0]} ({', then '.join(labels[1:])} if that runs out)"
    return labels[0] if labels else ""


def lane_paid(model: str) -> bool:
    """True when this job would go pay-as-you-go (its own free lane is down)."""
    return not free_steps(model) and bool(backup_steps(model))


def lanes_down() -> str:
    """'ClinePass usage limit reached -- paused until 12:04 PM; ...' for what is down, or ''."""
    return "; ".join(describe(r) for r in (active(), codex_active()) if r)


if __name__ == "__main__":
    args = sys.argv[1:] or ["status"]
    command = args[0]
    if command == "clear":
        clear()
        print("ClinePass pause lifted.")
        sys.exit(0)
    if command == "codex":  # the Codex lane: status (exit 0 if it can take work), or clear
        if len(args) > 1 and args[1] == "clear":
            codex_clear()
        print(codex_status())
        sys.exit(0 if codex_ready() else 1)
    if command == "where":  # where a job for <lane model> runs now; exit 1 if only Claude can
        where = lane_where(args[1] if len(args) > 1 else "cline-pass/deepseek-v4-pro", "--escalate" in args)
        print(where or "Claude (every lane that could take it is down)")
        sys.exit(0 if where else 1)
    if command == "codex-model":  # for cline-worker.sh
        print(CODEX_MODEL)
        sys.exit(0)
    if command == "backup":
        action = args[1] if len(args) > 1 else ""
        target = args[2] if len(args) > 2 else ""
        if action in ("on", "order") and target not in BACKUPS:
            print(f"Say which backup: {' or '.join(BACKUPS)}")
            sys.exit(2)
        if action == "on":
            if not backup_key(target):
                print(f"pi has no key for {BACKUPS[target]['label']} yet -- run set-backup-key.py {target} first.")
                sys.exit(1)
            backup_start(target, args[3:] or UPSTREAM)
        elif action == "off":
            backup_off(target if target in BACKUPS else "")
        elif action == "order":
            state = _state()
            state["order"] = [p for p in args[2:] if p in BACKUPS]
            _write(BACKUP_FILE, state)
        print(backup_status())
        sys.exit(0 if backup_on() or action in ("off", "order") else 1)
    if command == "backup-model":  # for cline-worker.sh: <model> [cheap|full] -> stand-in, or exit 1
        mapped = backup_model(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else "full")
        if mapped:
            print(mapped)
        sys.exit(0 if mapped else 1)
    if command == "backup-models":  # for check-tools.sh: <provider> -> its pi models
        providers = [args[1]] if len(args) > 1 and args[1] in BACKUPS else list(BACKUPS)
        print(" ".join(m for p in providers for m in backup_models(p)))
        sys.exit(0)
    record = active()
    if record:
        print(status_line(record) if "--line" in args else describe(record))
        sys.exit(0)
    print("ClinePass is open.")
    sys.exit(1)
