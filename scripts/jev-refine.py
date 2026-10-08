#!/usr/bin/env python3
"""
jev-refine.py — UserPromptSubmit interceptor backed by the TypeSafe System One API.

What it does
------------
Reads the Claude Code UserPromptSubmit hook payload from stdin, asks Jev a small
set of *typed* questions about the prompt in a single request, and emits guidance
back to Claude as `additionalContext`.

It also sizes the prompt to a model tier (haiku / sonnet / opus). A UserPromptSubmit
hook CANNOT switch the main loop model -- the only fields it may return are
additionalContext, and the PreToolUse-only permission fields -- so the route is
surfaced two ways that do work: `systemMessage` tells the user (who can run /model),
and additionalContext tells Claude which model to pass when spawning a subagent.

Why it is shaped this way
-------------------------
Jev is a System One model: it returns typed judgments (Noul probabilities, Choice
distributions, Scores) -- it does NOT generate text. So it cannot rewrite a prompt
into a "better" prompt. What it can do well is *judge* the prompt along dimensions
this script defines, and let code compose the guidance. The options live here in
code; Jev only picks among them.

Fail-open contract
------------------
Any failure -- missing key, network error, timeout, bad JSON, unexpected shape --
exits 0 with EMPTY stdout. On UserPromptSubmit, empty stdout means the prompt
reaches Claude completely untouched. The session can never be broken or stalled
by this hook.

Docs: https://docs.typesafe.ai/api
"""

import concurrent.futures
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
# UserPromptSubmit hooks block the session until they return (30s default cap).
# Keep this well under that so a slow API is never felt as a hang.
TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))
MAX_PROMPT_CHARS = 6000
MIN_PROMPT_CHARS = 12  # below this there is nothing useful to judge
# Turns the harness writes, not the user: judging them costs a Jev call and logs noise.
MACHINE_PREFIXES = ("<task-notification>", "<agent-message", "<system-reminder>",
                    "[SYSTEM NOTIFICATION", "<command-name>", "<local-command")

# Model routing. Tiers are ordered: a floor can only raise the pick, never lower it.
TIER_ORDER = ["haiku", "sonnet", "opus"]
# Short aliases are what the Agent tool's `model` parameter and `/model` both accept.
TIER_LABEL = {
    "haiku": "haiku (Haiku 4.5)",
    "sonnet": "sonnet (Sonnet 5.5)",
    "opus": "opus (Opus 5.5)",
}
ROUTER_ON = os.environ.get("JEV_ROUTER", "1") != "0"
# What the session is assumed to be running now, so the router only speaks up when
# it disagrees. The hook payload does not carry the active model, so this is a knob.
BASELINE = os.environ.get("JEV_ROUTER_BASELINE", "opus").strip().lower()
# Never route below this, whatever Jev says.
FLOOR = os.environ.get("JEV_ROUTER_FLOOR", "haiku").strip().lower()

# Open-model lane. Light and standard work that can be handed off without the
# conversation goes to an open model on ClinePass through the pi agent, so the Max
# subscription is kept for Claude-tier work. Deep, risky, or vague work never goes.
CLINE_ON = os.environ.get("JEV_CLINE", "1") != "0"
CLINE_OFFLOAD_MIN = float(os.environ.get("JEV_CLINE_OFFLOAD_MIN", "0.70"))
CLINE_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cline-worker.sh")
CLINE_LEVEL = {"haiku": "light", "sonnet": "standard"}

# Plan-then-delegate. A request that is too open for the lane as written, but that
# ends in real building work, is planned by Claude and built in pieces on ClinePass.
# Claude splits the job into written work orders; delegate.py has Jev score each
# order on its own and builds it on the matched model, or hands it back to Claude.
PLAN_DELEGATE_ON = os.environ.get("JEV_PLAN_DELEGATE", "1") != "0"
PLAN_BUILD_MIN = float(os.environ.get("JEV_PLAN_BUILD_MIN", "0.60"))
PLAN_SKIP_INTENTS = {"explain", "ops"}
DELEGATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delegate.py")
WORK_ORDER_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work-order-template.md")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cline_pause  # noqa: E402  -- the ClinePass pause switch
try:
    import jev_openrouter  # noqa: E402  -- Jev on OpenRouter, when TypeSafe can't answer
except Exception:  # never let a missing module cost the session its triage
    jev_openrouter = None
try:
    import jev_catalog  # noqa: E402  -- every installed skill, command and agent, and their review state
except Exception:
    jev_catalog = None

# When TypeSafe fails (out of credit, key, outage), Jev is asked on OpenRouter instead,
# and keeps going there for a while so each prompt doesn't wait out the failure again.
JEV_PROVIDER_FILE = os.path.expanduser("~/.config/typesafe/jev-provider.json")
JEV_FALLBACK_MINUTES = float(os.environ.get("JEV_FALLBACK_MINUTES", "30"))

# Lanes: which open model does which kind of job. Jev only judges the kind of task;
# this table decides the model, so a change of mind is an edit here, not a prompt.
#   shadow           -- log the pick, keep sending everything to LANE_DEFAULT
#   live (default)   -- hand the picked model to the worker (live since 2026-09-26)
#   off              -- don't ask the question at all
LANES_MODE = os.environ.get("JEV_LANES", "live").strip().lower()
LANE_DEFAULT = "cline-pass/deepseek-v4-pro"  # re-pointed 2026-10-05 from Qwen3.8 Max (slow, scores below its claims)
KEEP_ON_CLAUDE = "claude"  # design thinking: taste, the conversation, the design tools
IMAGE_LANE = "codex/image"  # Codex's built-in image tool, via codex-image.py
LANE_MODEL = {
    # Re-pointed 2026-10-05 after the model research: Kimi K3 is the strongest
    # non-Claude model on UI and websites (Design Arena); the Qwen Max models trailed.
    "ui-component": "cline-pass/kimi-k3",
    "ui-page": "cline-pass/kimi-k3",
    "logic": cline_pause.codex_lane_model(),  # GPT on the Codex subscription
    "long-session": "cline-pass/kimi-k3",
    "design": KEEP_ON_CLAUDE,
    "image": IMAGE_LANE,
    "other": LANE_DEFAULT,
}
# Below this, the kinds competed; use the default rather than a coin-flip lane.
LANE_MIN_CONFIDENCE = float(os.environ.get("JEV_LANE_MIN", "0.60"))
MODEL_LABEL = {
    "cline-pass/qwen3.8-max": "Qwen3.8 Max",
    "cline-pass/qwen3.7-max": "Qwen3.7 Max",
    "cline-pass/deepseek-v4-pro": "DeepSeek V4 Pro",
    "cline-pass/kimi-k3": "Kimi K3",
    cline_pause.codex_lane_model(): cline_pause.codex_name(),
}
IMAGE_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex-image.py")
ROUTING_LOG = os.environ.get("JEV_ROUTING_LOG", os.path.expanduser("~/.config/typesafe/jev-routing.jsonl"))
ROUTING_LOG_MAX = 5_000_000  # bytes; rotated once to .1 past this

# Skill ranking. `jev rank` grades every skill description against the prompt in one
# request, so the agent is told which of ~300 skills fit before it starts guessing.
RANK_ON = os.environ.get("JEV_RANK_SKILLS", "1") != "0"
RANK_TOP = int(os.environ.get("JEV_RANK_TOP", "4"))
RANK_MIN = float(os.environ.get("JEV_RANK_MIN", "0.75"))
RANK_TIMEOUT = float(os.environ.get("JEV_RANK_TIMEOUT", "6"))
RANK_QUESTION = "Would the skill in `candidate` be the right one to handle `query`?"
# Catalog notices: newly installed or changed skills, commands and agents that nobody has
# reviewed yet. Shown at most every CATALOG_NOTICE_HOURS unless the pending set changes.
CATALOG_ON = os.environ.get("JEV_CATALOG_NOTICE", "1") != "0"
CATALOG_NOTICE_FILE = os.path.expanduser("~/.config/typesafe/jev-catalog-notice.json")
CATALOG_NOTICE_HOURS = float(os.environ.get("JEV_CATALOG_NOTICE_HOURS", "6"))
CATALOG_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jev_catalog.py")
KIND_BY_NAME: dict[str, str] = {}  # filled by build_index; tells the note which tool runs each pick

DESC_CHARS = 240  # the opening sentences carry the routing signal; past 90k chars `jev rank` splits into parallel requests
# Hand-written routing lines that replace a skill's own description in the index. They
# carry what descriptions rarely say (stage, project type, required tools, model fit,
# tie-breakers) and live here so `npx skills update` can't overwrite them.
ROUTING_OVERRIDES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill-routing.json")
OVERRIDE_CHARS = 500


def routing_overrides() -> dict[str, str]:
    """{skill name: routing line}; {} if the file is missing or malformed."""
    try:
        with open(ROUTING_OVERRIDES, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {k: " ".join(str(v).split()) for k, v in (data.get("skills") or {}).items() if v}

YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"
MARKER = "[Jev Direct Gateway]: "


def notify(message: str) -> None:
    """
    Emit the confirmation line.

    stderr from a hook that exits 0 goes to the DEBUG LOG ONLY -- it is never
    shown in the transcript. So stderr alone is invisible unless you run
    `claude --debug`. We also write straight to the controlling TTY, which is
    what actually paints a visible line in an interactive terminal. Both are
    best-effort and can never raise.
    """
    line = f"{YELLOW}{MARKER}{DIM}{message}{RESET}\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass
    if os.environ.get("JEV_QUIET"):
        return
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(line)
            tty.flush()
    except Exception:
        pass  # no TTY: -p mode, CI, redirected output


def build_index(cwd: str) -> list[str]:
    """
    One "name: description" line per skill, command and agent this session can use.

    jev_catalog.discover() finds them: personal and project .claude folders, installed
    plugins (scope-aware) and claude.ai-synced plugins. The name in the line matters: it is
    what Claude is told to invoke, so commands and agents carry their kind in brackets.
    A routing line from skill-routing.json replaces the item's own description.
    """
    if jev_catalog is None:
        return []
    overrides = routing_overrides()
    lines: list[str] = []
    for item in jev_catalog.discover(cwd):
        name, kind, description = item["name"], item["kind"], item["description"]
        if not description and name not in overrides:
            continue
        KIND_BY_NAME[name] = kind
        label = name if kind == "skill" else f"{name} ({kind})"
        text = overrides[name][:OVERRIDE_CHARS] if name in overrides else description[:DESC_CHARS]
        lines.append(f"{label}: {text}")
    return lines


def catalog_notice(cwd: str) -> str:
    """
    A note asking Claude to catalogue newly installed or changed items, or "".

    Scans the catalog (cheap, ~0.1 s, run beside triage) and stays quiet when nothing is
    pending, or when the same pending set was already announced in the last few hours.
    """
    if not CATALOG_ON or jev_catalog is None:
        return ""
    try:
        pending = jev_catalog.scan(cwd)["pending"]
    except Exception:
        return ""
    if not pending:
        return ""
    key = hashlib.sha1("|".join(pending).encode()).hexdigest()
    try:
        with open(CATALOG_NOTICE_FILE, encoding="utf-8") as handle:
            last = json.load(handle)
        if last.get("key") == key and time.time() - float(last.get("at", 0)) < CATALOG_NOTICE_HOURS * 3600:
            return ""
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        os.makedirs(os.path.dirname(CATALOG_NOTICE_FILE), exist_ok=True)
        with open(CATALOG_NOTICE_FILE, "w", encoding="utf-8") as handle:
            json.dump({"key": key, "at": time.time()}, handle)
    except OSError:
        pass
    shown = ", ".join(f"`{name}`" for name in pending[:8])
    more = f" and {len(pending) - 8} more" if len(pending) > 8 else ""
    return (
        f"- Catalog: {len(pending)} newly installed or changed skills/commands/agents aren't "
        f"catalogued for Jev yet: {shown}{more}. After the user's request is handled, review "
        f"them: run `python3 {CATALOG_SCRIPT} pending`, read each file, then record either "
        f"`python3 {CATALOG_SCRIPT} review <name> --line \"<routing line>\"` (stage, project "
        f"fit, required tools, model fit, tie-breaker against similar items, not-for; 500 "
        f"chars max) or `review <name> --keep` when its own description already routes well "
        f"(`review --plugin <plugin> --keep` for a whole plugin). Tell the user in plain "
        f"English what was catalogued."
    )


def rank_skills(prompt: str, cwd: str) -> list[tuple[str, float]]:
    """
    Ask `jev rank` which installed skills, commands and agents fit this prompt.

    Shells out rather than reimplementing: `jev rank` embeds each candidate in its own
    question, which is the one addressing style measured to be accurate over long
    lists (positional `items[i]` misreads badly -- see the jev README). Returns [] on
    any failure, so ranking can never cost the session its triage.
    """
    candidates = build_index(cwd)
    if len(candidates) < 2:
        return []

    binary = jev_binary()
    if not binary or on_openrouter():
        return rank_on_openrouter(prompt, candidates)

    try:
        completed = subprocess.run(
            [binary, "rank",
             "--query", prompt[:MAX_PROMPT_CHARS],
             "--instructions", RANK_QUESTION,
             "--top", str(RANK_TOP),
             "--min", str(RANK_MIN),
             # ~460 candidates are two requests; run them side by side, not one after another
             "--concurrency", "4",
             "--json"],
            input="\n".join(candidates),
            capture_output=True, text=True, timeout=RANK_TIMEOUT,
        )
    except Exception:
        return rank_on_openrouter(prompt, candidates)

    if completed.returncode != 0:
        return rank_on_openrouter(prompt, candidates)

    try:
        results = json.loads(completed.stdout).get("results") or []
    except (ValueError, TypeError):
        return []

    ranked: list[tuple[str, float]] = []
    for row in results:
        candidate = row.get("candidate")
        if not isinstance(candidate, str):
            continue
        ranked.append((_index_name(candidate), float(row.get("p") or 0.0)))
    return ranked


def _index_name(candidate: str) -> str:
    """The invokable name from an index line: drop the description and any "(kind)" tag."""
    return candidate.split(": ", 1)[0].split(" (", 1)[0].strip()


def rank_on_openrouter(prompt: str, candidates: list[str]) -> list[tuple[str, float]]:
    """The same ranking through Jev on OpenRouter; [] if it isn't set up or fails."""
    if not openrouter_ready():
        return []
    try:
        ranked = jev_openrouter.rank(prompt, candidates, RANK_QUESTION, RANK_TOP, RANK_MIN, RANK_TIMEOUT)
    except Exception:
        return []
    return [(_index_name(candidate), p) for candidate, p in ranked]


SESSION_ID = ""  # the Claude Code session, so the dashboard can link its jobs to it


def read_payload() -> tuple[str, str]:
    """
    Return (prompt, cwd) from the hook payload.

    Claude Code sends a JSON object on stdin, not bare text:
      {"session_id":..., "cwd":..., "hook_event_name":"UserPromptSubmit",
       "prompt":"<what the user typed>"}
    We still tolerate raw text, so the script works if piped by hand.
    """
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return "", ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return raw.strip(), os.getcwd()
    if isinstance(payload, dict):
        global SESSION_ID
        SESSION_ID = str(payload.get("session_id") or "")
        prompt = payload.get("prompt") or ""
        cwd = payload.get("cwd") or os.getcwd()
        return (prompt if isinstance(prompt, str) else ""), cwd
    return raw.strip(), os.getcwd()


def build_questions() -> dict:
    """
    Independent judgments over the same state. Independent questions are
    batched into one request: they run in parallel and cost one round trip --
    so model routing rides along on the triage call for free.
    """
    questions = {
        "intent": {
            "type": "choice",
            "instructions": "What kind of work is `user_prompt` actually asking for?",
            "criteria": {
                "debug": "Something is broken, erroring, failing, or behaving wrongly, and the cause is not yet known.",
                "implement": "Build, add, or wire up new functionality that does not exist yet.",
                "refactor": "Restructure, clean up, rename, or simplify code whose behavior should not change.",
                "explain": "Understand how something works, or why it behaves as it does. No change is requested.",
                "review": "Audit or critique existing code or design and report findings.",
                "research": "Gather external facts, compare options, or check documentation before deciding.",
                "ops": "Run, configure, deploy, install, or otherwise operate tooling and environments.",
                "other": "None of the above fit the request.",
            },
        },
        "needs_repo_context": {
            "type": "noul",
            "instructions": "Would answering `user_prompt` correctly require reading this project's existing files first?",
            "criteria": {
                "true": "The answer depends on how this specific codebase is written, named, or configured.",
                "false": "It is answerable from general knowledge, or it is self-contained in the prompt.",
            },
        },
        "underspecified": {
            "type": "noul",
            "instructions": "Is `user_prompt` missing a detail that would materially change the resulting work?",
            "criteria": {
                "true": "Reasonable readings of the request lead to substantially different deliverables.",
                "false": "The request is specific enough to act on without guessing.",
            },
        },
        "complexity": {
            "type": "choice",
            "instructions": "How much reasoning capacity does answering `user_prompt` well actually require?",
            "criteria": {
                "light": "Mechanical and well-specified: a lookup, a rename, a format, a single obvious edit, a status question. One right answer, reachable without weighing tradeoffs.",
                "standard": "Ordinary engineering: a multi-file change, a routine bug, a component, a normal refactor. Judgment is needed but the path is familiar.",
                "deep": "Novel design, subtle correctness reasoning (concurrency, security, data loss), cross-cutting architecture, or genuinely competing tradeoffs with no settled answer.",
            },
        },
        "destructive": {
            "type": "noul",
            "instructions": "Does `user_prompt` imply an action that is hard to reverse or affects systems outside this machine?",
            "criteria": {
                "true": "Deleting or overwriting data, force-pushing, publishing, deploying, sending, or mutating shared state.",
                "false": "Reading, analysing, or making local edits that are easy to undo.",
            },
        },
        "offload_ok": {
            "type": "noul",
            "instructions": "Could `user_prompt` be handed to a separate coding agent that sees only the project files and a written task, not this conversation?",
            "criteria": {
                "true": "A bounded, concrete coding job -- write or change specific code, add tests, boilerplate, bulk or mechanical edits, docs -- whose result can be checked by reading the diff.",
                "false": "Needs the ongoing conversation, back-and-forth with the user, design or product judgment, secrets or private data, security-sensitive code, or is a question rather than a coding job.",
            },
        },
    }
    if PLAN_DELEGATE_ON:
        questions["has_build_work"] = {
            "type": "noul",
            "instructions": "Once its details are settled, would `user_prompt` end in writing or changing a meaningful amount of code or project files?",
            "criteria": {
                "true": "Building or changing files -- screens, components, styling, scripts, tests, bulk edits -- even if the request is vague now or needs decisions first.",
                "false": "A question, an explanation, a review with no changes, a one-line tweak, or running and configuring tools.",
            },
        }
    if LANES_MODE != "off":
        questions["task_kind"] = {
            "type": "choice",
            "instructions": "What kind of job is `user_prompt`? If it were handed to an agent, which of these is it?",
            "criteria": {
                "ui-component": "One UI piece or a visual tweak: a single component, its styling, layout, spacing, colour, or variants.",
                "ui-page": "A whole screen, page, or app scaffold: several components assembled into a layout, or a new screen or app from scratch.",
                "logic": "Non-visual code: state, data models, APIs, integrations, scripts, tests, or fixing wrong behaviour.",
                "long-session": "A long multi-step job across many files: a large refactor, a migration, or working through a whole flow step by step.",
                "design": "Design thinking rather than building: UX flows, design research or references, visual direction, critique of a design, naming or copy for the interface.",
                "image": "Making or editing a raster image: an illustration, photo, bitmap icon, texture, mockup image, or background.",
                "other": "None of the above fit.",
            },
        }
    return questions


def jev_binary() -> str:
    """Path to the `jev` CLI, or '' when it is not installed."""
    found = shutil.which("jev") or os.path.expanduser("~/.local/bin/jev")
    return found if os.path.exists(found) else ""


def request_body(prompt: str, cwd: str) -> dict:
    return {
        "state": {
            "user_prompt": prompt[:MAX_PROMPT_CHARS],
            "working_directory": cwd,
        },
        "model": MODEL,
        "questions": build_questions(),
    }


def ask_jev_cli(body: dict, binary: str) -> dict:
    """
    Triage through the `jev` CLI.

    Preferred over a direct POST for one reason: the CLI appends every call to
    ~/.config/typesafe/usage.jsonl, so `jev usage` reports what this hook actually
    costs. A bare POST is invisible to that ledger. Costs ~47 ms of interpreter
    startup, which is hidden anyway because triage runs beside the ranking call.
    """
    completed = subprocess.run(
        [binary, "ask", "-f", "-", "--json"],
        input=json.dumps(body), capture_output=True, text=True, timeout=TIMEOUT,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"jev ask exited {completed.returncode}")
    return json.loads(completed.stdout)


def ask_jev_http(body: dict, api_key: str) -> dict:
    """Direct POST. The fallback when the CLI is absent; not counted by `jev usage`."""
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "jev-interceptor/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def on_openrouter() -> bool:
    """True while TypeSafe recently failed, so Jev is asked on OpenRouter directly."""
    try:
        with open(JEV_PROVIDER_FILE, encoding="utf-8") as handle:
            return float(json.load(handle).get("until") or 0) > time.time()
    except (OSError, ValueError, AttributeError):
        return False


def _fall_back(error: Exception) -> None:
    try:
        os.makedirs(os.path.dirname(JEV_PROVIDER_FILE), exist_ok=True)
        now = time.time()
        with open(JEV_PROVIDER_FILE, "w", encoding="utf-8") as handle:
            json.dump({"provider": "openrouter", "why": f"{type(error).__name__}: {error}"[:200],
                       "since": now, "until": now + JEV_FALLBACK_MINUTES * 60}, handle)
    except OSError:
        pass


def openrouter_ready() -> bool:
    return bool(jev_openrouter) and jev_openrouter.available()


def ask_body(body: dict, api_key: str) -> tuple[dict, str]:
    """
    Ask Jev one request. Returns (response, provider): "typesafe" through the CLI (so
    the spend is recorded) or a direct POST without it; "openrouter" when TypeSafe fails
    -- out of credit, bad key, outage -- or failed within the last JEV_FALLBACK_MINUTES.
    """
    if not on_openrouter():
        try:
            binary = jev_binary()
            if binary:
                return ask_jev_cli(body, binary), "typesafe"
            if api_key:
                return ask_jev_http(body, api_key), "typesafe"
            raise RuntimeError("no TypeSafe key")
        except Exception as error:
            if not openrouter_ready():
                raise
            _fall_back(error)
    if not openrouter_ready():
        raise RuntimeError("TypeSafe is down and there is no OpenRouter key")
    return jev_openrouter.ask(body, TIMEOUT), "openrouter"


def ask_jev(prompt: str, cwd: str, api_key: str) -> tuple[dict, str]:
    """Run the triage questions: (response, provider)."""
    return ask_body(request_body(prompt, cwd), api_key)


# Guidance is composed HERE, in code -- Jev only selects the branch.
INTENT_GUIDANCE = {
    "debug": "Reproduce and locate the fault before proposing a fix; do not guess at causes.",
    "implement": "Match the conventions of the surrounding code rather than introducing a new style.",
    "refactor": "Behavior must not change. Verify equivalence before and after.",
    "explain": "This is a question, not a work order -- explain, and do not modify files unless asked.",
    "review": "Report findings with evidence; do not silently rewrite the code under review.",
    "research": "Verify against current sources rather than relying on recalled details.",
    "ops": "State what a command will do before running anything with side effects.",
    "other": "",
}


COMPLEXITY_TIER = {"light": "haiku", "standard": "sonnet", "deep": "opus"}


def _rank(tier: str) -> int:
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else 1


def route_model(answers: dict, vague: float, risky: float) -> tuple[str, str]:
    """
    Pick a model tier. Returns (tier, reason).

    Jev only chooses the complexity band; the escalation policy lives here in code,
    because "what is risky enough to deserve a bigger model" is a judgment this repo
    owns, not one the model should be improvising per prompt.
    """
    complexity = answers.get("complexity") or {}
    if complexity.get("type") != "choice":
        return "", ""

    label = complexity.get("choice")
    confidence = float(complexity.get("confidence") or 0.0)
    if not label or label not in COMPLEXITY_TIER:
        return "", ""

    # A band the options genuinely competed over is not a band worth acting on.
    if confidence < 0.55:
        return "", ""

    tier = COMPLEXITY_TIER[label]
    reason = f"{label} ({confidence:.2f})"

    # Escalations. Each can only raise the tier -- ordered so the strongest wins.
    if risky >= 0.70 and _rank(tier) < _rank("opus"):
        tier, reason = "opus", reason + " + irreversible"
    elif vague >= 0.70 and _rank(tier) < _rank("sonnet"):
        tier, reason = "sonnet", reason + " + underspecified"

    if _rank(tier) < _rank(FLOOR):
        tier, reason = FLOOR, reason + f" + floor={FLOOR}"

    return tier, reason


def pick_lane(answers: dict) -> tuple[str, float, str]:
    """
    Map Jev's task_kind to an open model. Returns (kind, confidence, model).

    An unclear kind -- missing, unknown, or below LANE_MIN_CONFIDENCE -- gets the
    default model, never a guess.
    """
    answer = answers.get("task_kind") or {}
    if answer.get("type") != "choice":
        return "", 0.0, LANE_DEFAULT
    kind = answer.get("choice") or ""
    confidence = float(answer.get("confidence") or 0.0)
    if kind not in LANE_MODEL or confidence < LANE_MIN_CONFIDENCE:
        return kind, confidence, LANE_DEFAULT
    return kind, confidence, LANE_MODEL[kind]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_route(record: dict) -> None:
    """Append one routing decision to the local log. Best-effort; never raises."""
    try:
        os.makedirs(os.path.dirname(ROUTING_LOG), exist_ok=True)
        if os.path.exists(ROUTING_LOG) and os.path.getsize(ROUTING_LOG) > ROUTING_LOG_MAX:
            os.replace(ROUTING_LOG, ROUTING_LOG + ".1")
        with open(ROUTING_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass


def compose_context(answers: dict, ranked: list[tuple[str, float]] | None = None,
                    catalog_note: str = "") -> tuple[str, list[str], dict]:
    """Turn typed answers into guidance. Returns (context, marker_facts, route_record)."""
    notes: list[str] = []
    facts: list[str] = []
    record: dict = {"event": "route", "lanes": LANES_MODE}

    intent = answers.get("intent") or {}
    if intent.get("type") == "choice":
        label = intent.get("choice")
        confidence = float(intent.get("confidence") or 0.0)
        facts.append(f"intent={label} ({confidence:.2f})")
        # Low confidence means the options genuinely competed; don't assert a branch.
        if label and confidence >= 0.55:
            guidance = INTENT_GUIDANCE.get(label, "")
            if guidance:
                notes.append(f"- Task looks like **{label}**. {guidance}")

    def noul(key: str) -> float:
        answer = answers.get(key) or {}
        return float(answer.get("noul") or 0.0) if answer.get("type") == "noul" else 0.0

    repo = noul("needs_repo_context")
    vague = noul("underspecified")
    risky = noul("destructive")
    facts.append(f"repo={repo:.2f} vague={vague:.2f} risk={risky:.2f}")

    if repo >= 0.70:
        notes.append("- Depends on this codebase: read the relevant files before answering.")
    if vague >= 0.70:
        notes.append("- Underspecified: state your assumption, or ask one focused question.")
    if risky >= 0.70:
        notes.append("- Possibly irreversible or outward-facing: confirm before executing.")

    tier, tier_reason = route_model(answers, vague, risky) if ROUTER_ON else ("", "")
    offload = noul("offload_ok")
    facts.append(f"offload={offload:.2f}")

    # Only after escalations: a tier raised to opus by risk never reaches this lane.
    # Vagueness is not a gate -- Jev scores most prompts high on it, and Claude writes
    # the worker a self-contained task anyway, which is where the gaps get resolved.
    level = CLINE_LEVEL.get(tier, "")
    kind, kind_conf, lane_pick = pick_lane(answers) if LANES_MODE != "off" else ("", 0.0, LANE_DEFAULT)
    if kind:
        facts.append(f"kind={kind} ({kind_conf:.2f})")
    record.update({
        "intent": (answers.get("intent") or {}).get("choice"),
        "complexity": (answers.get("complexity") or {}).get("choice"),
        "tier": tier, "risk": round(risky, 2), "vague": round(vague, 2),
        "offload": round(offload, 2), "kind": kind, "kind_conf": round(kind_conf, 2),
        "lane_pick": lane_pick, "used_model": "",
    })
    # Lanes: Jev's pick runs on its own free lane (ClinePass or Codex) when that is up --
    # its smaller sibling first -- and pay-as-you-go on the same family, cheap model first,
    # when that lane is at its limit (cline_pause.lane_where). Design thinking stays on Claude;
    # image jobs go to Codex's image tool.
    model = lane_pick if LANES_MODE == "live" else LANE_DEFAULT
    on_claude = model == KEEP_ON_CLAUDE
    where = cline_pause.lane_where(model) if CLINE_ON and not on_claude and model != IMAGE_LANE else ""
    closed = not where
    down = cline_pause.lanes_down() if CLINE_ON else ""
    paused = cline_pause.active() if CLINE_ON else {}
    if paused:
        record["cline_paused"] = paused.get("kind")
    if down:
        record["lanes_down"] = down
        notes.append(f"- Down right now: {down}. "
                     + (f"Lane work for this prompt runs on {where} instead; the worker "
                        f"switches back by itself when the lane is up." if where else
                        "No lane can take this, so Jev picks the Claude model."))
    if where and cline_pause.lane_paid(model):
        record["backup"] = cline_pause.backup_where()
    if on_claude:
        facts.append("lane=claude (design)")
        notes.append("- Design work (UX, research, visual direction, critique): Jev keeps it on "
                     "Claude, with the design skills and reference tools; don't hand it to a lane.")
    if (
        CLINE_ON and model == IMAGE_LANE and risky < 0.50 and os.path.exists(IMAGE_WORKER)
    ):
        record.update({"route": "image", "used_model": cline_pause.codex_lane_model()})
        facts.append("route=image")
        notes.append(
            f"- Model routing: this is an image job -- **Codex image generation** (the user's "
            f"ChatGPT plan). Write a precise image prompt (subject, style, colours, background, "
            f"aspect) and run `python3 {IMAGE_WORKER} \"<prompt>\" <project path>/<name>.png` "
            f"from the project directory; it prints the saved path. Look at the image before "
            f"reporting. Exit 75 means Codex can't take it (limit or login): use OpenRouter's "
            f"image tool instead (ToolSearch \"generate-image\"; pay-as-you-go) and say so."
        )
    elif (
        CLINE_ON and not closed and level and offload >= CLINE_OFFLOAD_MIN
        and risky < 0.50 and os.path.exists(CLINE_WORKER)
    ):
        # Shadow mode records the pick but keeps using the default, so the lanes can
        # be judged against real prompts before any of them carries work.
        label = where
        record["lane_label"] = label
        record.update({"route": f"cline-{level}", "used_model": model})
        facts.append(f"route=cline-{level}")
        facts.append(f"model={model}")
        notes.append(
            f"- Model routing: Jev sends this to the open-model lane -- **{label}** "
            f"({tier_reason}, offload {offload:.2f}). Instead of a Claude subagent, write a "
            f"self-contained task (goal, files, constraints, done-when) and run it with Bash "
            f"from the project directory: `{CLINE_WORKER} {level} \"<task>\" {model}` "
            f"(run_in_background for long jobs). If the lane hits a limit, the worker runs the "
            f"same family pay-as-you-go, cheap model first (Cline credits, then OpenRouter) by itself. Then review "
            f"the diff yourself before "
            f"reporting. Exit 4 means the worker's attempts failed; exit 75 means every lane "
            f"that could take it is down. If the first result is poor, rerun once with "
            f"--escalate (the full model, more effort); if the worker fails or that result "
            f"is poor too, fall back to a subagent with model=\"{tier}\" and say so."
        )
    elif (
        CLINE_ON and not closed and PLAN_DELEGATE_ON and os.path.exists(DELEGATE)
        and noul("has_build_work") >= PLAN_BUILD_MIN and risky < 0.50
        and (answers.get("intent") or {}).get("choice") not in PLAN_SKIP_INTENTS
    ):
        build = noul("has_build_work")
        record.update({"route": "plan-delegate", "build": round(build, 2)})
        facts.append(f"route=plan-delegate build={build:.2f}")
        notes.append(
            f"- Model routing: Jev marks this **plan here, build on the open-model lanes** "
            f"(build work {build:.2f}, offload as written {offload:.2f}). The request needs "
            f"judgment first but ends in building. (1) Plan on this model: read the files, "
            f"settle open questions with the user, and split the build into self-contained "
            f"work orders using {WORK_ORDER_TEMPLATE}. Keep with Claude any piece that needs "
            f"taste, the conversation, or is only a few minutes of work. (2) Save each order to "
            f"a file in the scratchpad and run `python3 {DELEGATE} run <order.md>` from the "
            f"project directory (run_in_background for long jobs; independent orders can run in "
            f"parallel). Jev scores the order itself: it builds on the matched lane (ClinePass "
            f"or Codex; small model first, then the full one; pay-as-you-go if that lane is at its limit), or exits 5 = "
            f"keep it with Claude, on the Claude model delegate.py names (run the piece as a "
            f"subagent with that model). (3) Quality gate: save the real result (diff, test "
            f"output) to a file and run `python3 {DELEGATE} check <id> <result-file>` -- Jev "
            f"scores it against the order's Done when; the result file lists each Done-when "
            f"check with its real output and the files changed, not a code dump -- then check "
            f"it yourself and record "
            f"`python3 {DELEGATE} review <id> pass|redo|fail \"<note>\"`. Short of the bar: "
            f"redo once with sharper feedback, as `python3 {DELEGATE} run <order.md> --escalate`; "
            f"still short: `python3 {DELEGATE} run <order.md> --up` names the next Claude model "
            f"up -- run it there and tell the user."
        )
    elif tier:
        record["route"] = tier
        facts.append(f"route={tier}")
        if tier != BASELINE:
            direction = "down" if _rank(tier) < _rank(BASELINE) else "up"
            notes.append(
                f"- Model routing: Jev sizes this {direction} to **{TIER_LABEL[tier]}** "
                f"({tier_reason}). Baseline is {BASELINE}. This hook cannot switch the "
                f"main model -- if you dispatch this to a subagent, pass model=\"{tier}\"; "
                f"otherwise mention the suggestion once and continue on the current model. "
                f"Start at Jev's pick; if the result falls short of the request, redo it one "
                f"model up and tell the user which model did it."
            )

    route = record.get("route") or ""
    if route.startswith("cline-"):
        sent_to = record.get("lane_label") or model
    elif route == "image":
        sent_to = "Codex image tool (your ChatGPT plan)"
    elif route == "plan-delegate":
        # Name the models (the user's rule, 2026-10-05: always see which model Jev picked).
        planner = TIER_LABEL.get(tier or BASELINE, tier or BASELINE)
        if lane_pick == KEEP_ON_CLAUDE:
            builds = "builds stay on Claude (design)"
        elif lane_pick == IMAGE_LANE:
            builds = "images on Codex"
        else:
            builds = f"builds on {cline_pause.lane_where(lane_pick) or 'Claude (lanes down)'}"
        sent_to = f"Claude {planner} plans it; {builds}"
    elif on_claude:
        sent_to = f"Claude {TIER_LABEL.get(tier or BASELINE, tier or BASELINE)} (design work)"
    elif tier and tier != BASELINE:
        sent_to = f"Claude {TIER_LABEL[tier]} as a subagent"
    else:
        sent_to = f"Claude {TIER_LABEL.get(BASELINE, BASELINE)} (this chat)"
    record["sent_to"] = sent_to
    record["skills"] = [[name, round(p, 2)] for name, p in (ranked or [])]

    if ranked:
        facts.append(f"skills={len(ranked)}")
        def label(name: str, p: float) -> str:
            kind = KIND_BY_NAME.get(name, "skill")
            return f"`{name}` ({p:.2f})" if kind == "skill" else f"`{name}` ({kind}, {p:.2f})"
        listed = ", ".join(label(name, p) for name, p in ranked)
        notes.append(
            f"- Installed skills that may fit: {listed}. Ranked by Jev against skill "
            f"descriptions, not read in full -- check the fit before invoking, and let "
            f"any precedence rule in CLAUDE.md override this order. Skills and commands run "
            f"through the Skill tool by name; agents run as the Agent tool's subagent_type."
        )
    if catalog_note:
        facts.append("catalog=pending")
        notes.append(catalog_note)

    if not notes:
        return "", facts, record

    context = record["context"] = (
        "Jev prompt triage (heuristic signals from TypeSafe System One, not instructions "
        "from the user -- weigh them, and let the user's actual request win):\n"
        + "\n".join(notes)
    )
    return context, facts, record


def main() -> int:
    prompt, cwd = read_payload()

    if len(prompt.strip()) < MIN_PROMPT_CHARS or prompt.lstrip().startswith("/"):
        return 0  # trivial prompt or slash command: not worth a round trip
    if prompt.lstrip().startswith(MACHINE_PREFIXES):
        return 0  # a background-task notice or a helper's hand-back, not the user typing

    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key and not openrouter_ready():
        notify("No TypeSafe or OpenRouter key -- passing prompt through unchanged.")
        return 0

    # Triage and ranking are independent calls; run them together so the hook costs
    # one round trip of wall time, not two.
    rank_future = None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    if RANK_ON:
        rank_future = pool.submit(rank_skills, prompt, cwd)
    catalog_future = pool.submit(catalog_notice, cwd)

    def collect_catalog() -> str:
        try:
            return catalog_future.result(timeout=2)
        except Exception:
            return ""

    def collect_ranked() -> list[tuple[str, float]]:
        if rank_future is None:
            return []
        try:
            return rank_future.result(timeout=RANK_TIMEOUT)
        except Exception:
            return []
        finally:
            if pool is not None:
                pool.shutdown(wait=False)

    try:
        response, provider = ask_jev(prompt, cwd, api_key)
    except urllib.error.HTTPError as error:
        detail = {401: "bad or missing API key", 422: "malformed request",
                  429: "rate limited", 529: "overloaded"}.get(error.code, "HTTP error")
        notify(f"skipped -- {error.code} {detail}. Prompt passed through unchanged.")
        collect_ranked()
        return 0
    except Exception as error:
        notify(f"skipped -- {type(error).__name__}. Prompt passed through unchanged.")
        collect_ranked()
        return 0

    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        notify("skipped -- unexpected response shape. Prompt passed through unchanged.")
        collect_ranked()
        return 0

    context, facts, record = compose_context(answers, collect_ranked(), collect_catalog())
    summary = " ".join(facts) if facts else "no signals"
    # Which app the session runs in (cli, desktop app, IDE...), so the report can
    # confirm routing works everywhere the user works.
    record.update({"ts": _now(), "cwd": cwd, "prompt": prompt[:MAX_PROMPT_CHARS],
                   "app": os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""), "jev_provider": provider,
                   "session_id": SESSION_ID})
    log_route(record)

    payload: dict = {}
    if context:
        payload["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }

    # systemMessage is the one channel that reaches the *user*, who is the only
    # party able to run /model. Stay quiet unless the router actually disagrees.
    route = next((f[len("route="):] for f in facts if f.startswith("route=")), "")
    if route.startswith("cline-"):
        paid = " (pay-as-you-go: the free lanes are down)" if record.get("backup") else ""
        payload["systemMessage"] = (
            f"Jev router: this prompt goes to {record.get('lane_label')}{paid} -- "
            f"Claude will review the result."
        )
    elif route == "image":
        payload["systemMessage"] = "Jev router: image job -- Codex makes it, Claude checks it."
    elif route.startswith("plan-delegate"):
        payload["systemMessage"] = "Jev router: Claude checks each built piece; anything short goes one model up."
    elif route and route != BASELINE:
        payload["systemMessage"] = (
            f"Jev router: this prompt sizes to {TIER_LABEL[route]} "
            f"(baseline {BASELINE}) -- /model {route} to switch."
        )

    down = record.get("lanes_down")
    if down and not route.startswith(("cline-", "plan-delegate", "image")):
        payload["systemMessage"] = (
            f"Jev router: {down}. "
            + payload.get("systemMessage", "").replace("Jev router: ", "")
        ).strip()

    # Every prompt gets one line naming where it went and the skills that went with it
    # (the user's rule: always see the routing). The full record is on the dashboard.
    skills = ", ".join(name for name, _ in record.get("skills") or []) or "none"
    via = " · Jev via OpenRouter" if provider == "openrouter" else ""
    line = f"Jev → {record.get('sent_to')} · skills: {skills}{via}"
    extra = payload.get("systemMessage", "").replace("Jev router: ", "")
    payload["systemMessage"] = f"{line}\n{extra}" if extra else line

    # Jev's notice in yellow: real colour in a terminal; the desktop app shows plain
    # text, so there it gets a yellow marker instead.
    if payload.get("systemMessage"):
        message = payload["systemMessage"]
        if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli":
            payload["systemMessage"] = f"\033[33m{message}\033[0m"
        else:
            payload["systemMessage"] = f"🟡 {message}"

    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    notify(f"{summary} -- {'context injected' if context else 'routing line only'}.")
    return 0


if __name__ == "__main__":
    # sys.exit() must stay OUTSIDE the guard: it raises SystemExit, which a
    # bare BaseException catch would swallow and report as an internal error.
    try:
        exit_code = main()
    except BaseException as error:  # never break the session, for any reason
        try:
            notify(f"internal error ({type(error).__name__}) -- prompt passed through.")
        except Exception:
            pass
        exit_code = 0
    sys.exit(exit_code)
