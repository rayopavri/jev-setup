#!/usr/bin/env python3
"""
jev-dashboard.py -- one local HTML page showing every Jev routing decision.

    python3 jev-dashboard.py [--out PATH] [--days N] [--open]

Reads ~/.config/typesafe/jev-routing.jsonl (or $JEV_ROUTING_LOG), plus the rotated
copy `<log>.1` first because it holds the older rows, and writes a single
self-contained page: no external CSS, JS, fonts or network calls, so it opens from
disk and stays readable offline.

The page answers, task by task and in plain English: what you sent, which model took
it, which skills went with it, every attempt a model made, what Jev's quality check
scored, what Claude's review decided, and whether a job was redone or escalated.
Cards are one prompt each, newest first; `claude_limit` rows get their own red card;
pieces and attempts without a prompt in the previous six hours are collected in a
"Jobs started outside a prompt" card so nothing in the log goes unshown.

The log is only ever read. Every string taken from it is HTML-escaped on the way into
the page (prompts routinely contain HTML and `<script>` text), and the inline JS only
ever reads `textContent` -- no log text reaches `innerHTML`.
"""

import argparse
import collections
import html
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

LOG = os.path.expanduser(os.environ.get("JEV_ROUTING_LOG", "~/.config/typesafe/jev-routing.jsonl"))
OUT = os.path.expanduser("~/.config/typesafe/jev-dashboard.html")
MISSING = "—"  # any field can be absent from a log row; the page says so plainly

# Plain-English names for the numbers a worker exits with (cline-worker.sh).
EXIT_WORDS = {0: "done", 4: "failed", 75: "limit hit, moved on", 142: "timed out", 143: "stuck"}
# What Jev decided to do when Claude ran out of allowance.
CHOICE_WORDS = {"same": "tried the same model again", "cheaper": "moved to a cheaper model",
                "wait": "waited for the limit to lift", "none": "nothing else was available"}
# `decision` on a piece, in words.
DECISION_WORDS = {"cline": "built on an open-model lane", "claude": "kept with Claude",
                  "dry-run": "rehearsal — nothing was sent"}
# Jev's signals on a prompt, with the question each one answers.
SIGNALS = (("intent", "What you were doing"), ("complexity", "How big the job looked"),
           ("kind", "Kind of work"), ("risk", "Risk — how hard to undo"),
           ("vague", "How vague the prompt was"), ("offload", "Could another model take it"))
NUMERIC = ("risk", "vague", "offload")
PROMPT_SKIP_PREFIXES = ("<task-notification>", "<agent-message", "<system-reminder>",
                        "[SYSTEM NOTIFICATION", "<command-name>", "<local-command")
ROUTE_AGE = timedelta(hours=6)

MODEL_WORDS = {
    "cline-pass/qwen3.8-max": "Qwen3.8 Max on ClinePass",
    "cline-pass/qwen3.7-max": "Qwen3.7 Max on ClinePass",
    "cline-pass/kimi-k3": "Kimi K3 on ClinePass",
    "cline-pass/deepseek-v4-pro": "DeepSeek V4 Pro on ClinePass",
    "codex/gpt-6-luna": "GPT-6-Luna on your Codex subscription",
}
TIER_WORDS = {"haiku": "Claude Haiku 4.5 (suggested)",
              "sonnet": "Claude Sonnet 5 (suggested)",
              "opus": "Claude Opus 5.5 (this chat)"}


# ---------------------------------------------------------------- reading the log

def read_log() -> list[dict]:
    """Every parseable row of the log, oldest file first. Unparseable lines are skipped.

    A row whose `ts` is not text (a truncated or hand-edited line) keeps going with an
    empty stamp rather than breaking the sort -- it simply falls outside every period.
    """
    rows = []
    for path in (LOG + ".1", LOG):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if not isinstance(row.get("ts"), str):
                    row["ts"] = ""
                rows.append(row)
    return rows


def cutoff(days: int) -> str:
    """The UTC stamp rows must not be older than, as the log writes it."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_time(ts) -> str:
    """A log stamp (UTC) as the clock on this machine shows it."""
    if not ts:
        return MISSING
    try:
        moment = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return e(ts)
    local = moment.astimezone()
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b %Y, %H:%M')}"


def folder(cwd) -> str:
    """The project folder's own name -- the part you recognise."""
    if not cwd:
        return MISSING
    return e(os.path.basename(str(cwd).rstrip("/")) or cwd)


def e(value) -> str:
    """The one door log text goes through: escaped, with a dash for anything absent."""
    if value is None or value == "" or value == [] or value == {}:
        return MISSING
    return html.escape(str(value), quote=True)


def snippet(text, limit: int = 160) -> str:
    """The first `limit` characters of a prompt, on one line, for the card face."""
    if not text:
        return MISSING
    flat = " ".join(str(text).split())
    return e(flat[:limit] + ("…" if len(flat) > limit else ""))


def score_word(value) -> str:
    """A 0-1 score as a word, next to the number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return " (low)" if number < 0.34 else (" (high)" if number >= 0.67 else " (medium)")


def label(row: dict, *keys: str) -> str:
    """The first plain label present, then a readable model name."""
    for key in keys:
        if row.get(key):
            return e(row[key])
    return model_label(row.get("model"))


def model_label(model) -> str:
    """Turn a model identifier into a short, readable label."""
    model = str(model or "").strip()
    if not model or model == MISSING:
        return e("Claude (this chat)")
    if model in MODEL_WORDS:
        return e(MODEL_WORDS[model])

    suffix = ""
    name = model
    if model.startswith("cline-credits/"):
        name = model[len("cline-credits/"):]
        suffix = " on Cline credits"
    elif model.startswith("openrouter/"):
        name = model[len("openrouter/"):]
        suffix = " on OpenRouter"
    elif "/" in model:
        name = model.rsplit("/", 1)[-1]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]

    known_names = {key.rsplit("/", 1)[-1]: value.split(" on ", 1)[0]
                   for key, value in MODEL_WORDS.items()}
    plain = known_names.get(name)
    if not plain:
        plain = " ".join(part.upper() if part.lower() in {"gpt", "ai"} else part.capitalize()
                         for part in name.replace("_", "-").split("-") if part)
    return e(plain + suffix)


def route_label(route: dict) -> str:
    """Plain words for the route's destination, including older log rows."""
    sent_to = str(route.get("sent_to") or "").strip()
    if sent_to and sent_to != MISSING:
        return e(sent_to)

    choice = str(route.get("route") or "").strip().lower()
    tier = str(route.get("tier") or "").strip().lower()
    if choice in ("cline-light", "cline-standard"):
        model = route.get("lane_pick") or route.get("used_model")
        if model and str(model).strip() != MISSING:
            return model_label(model)
    if choice == "plan-delegate":
        return e("Claude plans it; build pieces go to the open-model lanes")
    if choice == "image":
        return e("Codex image tool")
    if choice in TIER_WORDS:
        return e(TIER_WORDS[choice])
    if tier in TIER_WORDS:
        return e(TIER_WORDS[tier])
    return e("Claude (this chat)")


def skipped_prompt(row: dict) -> bool:
    prompt = row.get("prompt")
    return isinstance(prompt, str) and prompt.lstrip().startswith(PROMPT_SKIP_PREFIXES)


def stamp(value):
    """Parse the UTC stamp format written in the routing log."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def exit_words(code) -> str:
    if code is None or code == "":
        return MISSING
    try:
        return EXIT_WORDS.get(int(code), f"stopped (exit {int(code)})")
    except (TypeError, ValueError):
        return e(code)


def exit_tone(code) -> str:
    """Colour for an attempt: green when it finished, amber for a limit, red otherwise."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "muted"
    if code == 0:
        return "pass"
    return "warn" if code == 75 else "fail"


def step_of(row: dict) -> int:
    """Which try a piece is on: 1 first try, 2 a redo, 3 and up an escalation."""
    try:
        return int(row.get("step") or 1)
    except (TypeError, ValueError):
        return 1


def seconds(value) -> str:
    try:
        return f"{int(value)}s"
    except (TypeError, ValueError):
        return MISSING


# ------------------------------------------------------------------- grouping

def group(rows: list[dict]) -> dict:
    """Sort the period's rows into the cards the page shows.

    Rows logged since 2026-10-04 carry exact links: a prompt and its jobs share the
    Claude Code `session_id`, and each attempt names its `piece`. Older rows fall back to
    time: the most recent prompt from the same folder, then any folder, no more than six
    hours earlier.
    """
    routes = sorted([r for r in rows if r.get("event") == "route" and not skipped_prompt(r)],
                    key=lambda r: r.get("ts") or "")
    by_folder = collections.defaultdict(list)
    for route in routes:
        by_folder[route.get("cwd") or ""].append(route)

    windows = []  # one per prompt: the folder, stamp, and what landed in it
    for cwd, group_routes in by_folder.items():
        for route in group_routes:
            windows.append({"route": route, "cwd": cwd, "start": route.get("ts") or "",
                            "pieces": [], "loose": []})
    windows.sort(key=lambda window: window["start"])

    def window_for(row: dict):
        """The latest matching prompt at or before this row, within six hours."""
        moment = stamp(row.get("ts"))
        if moment is None:
            return None
        eligible = []
        for window in windows:
            route_moment = stamp(window["start"])
            if route_moment is None or route_moment > moment or moment - route_moment > ROUTE_AGE:
                continue
            eligible.append((route_moment, window))
        if not eligible:
            return None
        session = row.get("session_id")
        same_session = [item for item in eligible if session and item[1]["route"].get("session_id") == session]
        if same_session:
            return max(same_session, key=lambda item: item[0])[1]
        same_folder = [item for item in eligible if item[1]["cwd"] == (row.get("cwd") or "")]
        return max(same_folder or eligible, key=lambda item: item[0])[1]

    def latest_piece_before(pieces: list[dict], row: dict):
        """The latest piece in a card that started no more than six hours earlier."""
        moment = stamp(row.get("ts"))
        if moment is None:
            return None
        eligible = []
        for piece in pieces:
            piece_moment = stamp(piece.get("ts"))
            if piece_moment is None or piece_moment > moment or moment - piece_moment > ROUTE_AGE:
                continue
            eligible.append((piece_moment, piece))
        return max(eligible, key=lambda item: item[0])[1] if eligible else None

    orphans = {"pieces": [], "loose": []}
    by_id = {}
    for piece in sorted([r for r in rows if r.get("event") == "piece"], key=lambda r: r.get("ts") or ""):
        window = window_for(piece)
        (window or orphans)["pieces"].append(piece)
        if piece.get("id"):
            by_id[piece["id"]] = piece

    # An attempt that names its piece goes straight to it. Older attempts carry no piece
    # id, so each belongs to the last piece started before it in the selected route
    # card; ones with no piece behind them stay on the card.
    for worker in sorted([r for r in rows if r.get("event") == "worker"], key=lambda r: r.get("ts") or ""):
        if worker.get("piece") in by_id:
            by_id[worker["piece"]].setdefault("attempts", []).append(worker)
            continue
        window = window_for(worker)
        bucket = window or orphans
        piece = latest_piece_before(bucket["pieces"], worker)
        if piece:
            piece.setdefault("attempts", []).append(worker)
        else:
            bucket["loose"].append(worker)

    checks, reviews = collections.defaultdict(list), collections.defaultdict(list)
    for row in rows:
        if row.get("event") == "check":
            checks[row.get("id")].append(row)
        elif row.get("event") == "review":
            reviews[row.get("id")].append(row)
    for table in (checks, reviews):  # "passed first time" means the earliest verdict was a pass
        for rows_for_piece in table.values():
            rows_for_piece.sort(key=lambda row: row.get("ts") or "")

    limits = sorted([r for r in rows if r.get("event") == "claude_limit"], key=lambda r: r.get("ts") or "")
    return {"windows": windows, "orphans": orphans, "checks": checks, "reviews": reviews,
            "limits": limits, "rows": rows}


def counts(rows: list[dict], grouped: dict) -> list[tuple[str, int]]:
    """The plain numbers for the period."""
    pieces = [r for r in rows if r.get("event") == "piece" and r.get("decision") != "dry-run"]
    delegated = [p for p in pieces if p.get("decision") == "cline" or p.get("exit") is not None]
    ids = {p.get("id") for p in delegated if p.get("id")}
    reviews, checks = grouped["reviews"], grouped["checks"]

    def verdicts(table, piece_id, wanted):
        return any(row.get("verdict") == wanted for row in table.get(piece_id, []))

    first_pass = sum(1 for piece_id in ids
                     if reviews.get(piece_id) and reviews[piece_id][0].get("verdict") == "pass")
    redone = {i for i in ids if verdicts(reviews, i, "redo") or verdicts(checks, i, "redo")}
    redone |= {p.get("id") for p in pieces if step_of(p) >= 2 and p.get("id")}
    escalated = {i for i in ids if verdicts(checks, i, "escalate")}
    escalated |= {p.get("id") for p in pieces if (p.get("escalate") or step_of(p) >= 3) and p.get("id")}
    limits = (sum(1 for r in rows if r.get("event") == "worker" and r.get("exit") == 75)
              + len(grouped["limits"]))
    openrouter = sum(1 for r in rows if r.get("jev_provider") == "openrouter")

    return [("Prompts", sum(1 for r in rows if r.get("event") == "route" and not skipped_prompt(r))),
            ("Jobs delegated", len(delegated)),
            ("Passed first time", first_pass),
            ("Redone", len(redone)),
            ("Escalated", len(escalated)),
            ("Limit hits", limits),
            ("Jev calls via OpenRouter", openrouter)]


def tags(pieces: list[dict], workers: list[dict], route: dict = None, limit: dict = None) -> str:
    """Which filter chips a card answers to: claude, open, paid, fail."""
    found = set()
    chosen = str(route.get("route") or "") if route else ""
    text = " ".join(str(route.get(key) or "") for key in ("route", "sent_to", "tier", "context")).lower() \
        if route else ""
    if limit or "claude" in text or chosen.startswith(("sonnet", "opus", "haiku")):
        found.add("claude")
    if chosen.startswith("cline") or chosen == "plan-delegate":
        found.add("open")
    for row in list(pieces) + list(workers):
        model = str(row.get("model") or "")
        words = f"{row.get('where') or ''} {row.get('label') or ''} {model}".lower()
        if model.startswith("cline-pass/") or model.startswith("codex/") or "clinepass" in words \
                or "codex" in words:
            found.add("open")
        if model.startswith(("openrouter/", "cline-credits/")) or "openrouter" in words \
                or "cline credits" in words:
            found.add("paid")
        if row.get("decision") == "claude" and row.get("exit") is None:
            found.add("claude")
    bad = any(row.get("exit") not in (0, None) for row in workers)
    bad |= any(bool(row.get("escalate")) or step_of(row) >= 2 for row in pieces)
    if limit or bad:
        found.add("fail")
    return " ".join(sorted(found))


# ------------------------------------------------------------------ the page

CSS = """
:root {
  --bg: #f7f7f5; --card: #ffffff; --ink: #1b1b19; --muted: #6d6d66; --line: #e4e4de;
  --accent: #4a5bd7; --accent-ink: #ffffff; --soft: #f1f1ec; --code: #f3f3ef;
  --pass: #1c7a45; --pass-bg: #e7f3ec; --warn: #8a6100; --warn-bg: #fbf2dc;
  --fail: #b3261e; --fail-bg: #fbeae8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131315; --card: #1a1a1d; --ink: #ececea; --muted: #9c9c95; --line: #2c2c31;
    --accent: #93a1ff; --accent-ink: #14141c; --soft: #212126; --code: #212126;
    --pass: #63c98f; --pass-bg: #16281e; --warn: #ddb257; --warn-bg: #2a2214;
    --fail: #ef8b84; --fail-bg: #2c1917;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); line-height: 1.5;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
  "Apple Color Emoji", sans-serif; }
.wrap { max-width: 960px; margin: 0 auto; padding: 0 16px 64px; }
header { padding: 40px 0 8px; }
h1 { margin: 0; font-size: 28px; letter-spacing: -0.01em; }
.sub { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
.counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin: 24px 0; padding: 0; }
.count { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; }
.count b { display: block; font-size: 22px; font-weight: 600; }
.count span { display: block; color: var(--muted); font-size: 12.5px; margin-top: 2px; }
.tools { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 12px 0;
  border-bottom: 1px solid var(--line); margin-bottom: 20px; }
#q { width: 100%; padding: 11px 12px; font-size: 15px; color: var(--ink);
  background: var(--card); border: 1px solid var(--line); border-radius: 10px; }
#q:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; align-items: center; }
.chip { font: inherit; font-size: 13px; color: var(--ink); background: var(--card);
  border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px; cursor: pointer; }
.chip.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.shown { color: var(--muted); font-size: 12.5px; margin-left: auto; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 16px; margin-bottom: 14px; overflow-wrap: anywhere; }
.card[hidden] { display: none; }
.card.limit { background: var(--fail-bg); border-color: var(--fail); }
.when { color: var(--muted); font-size: 12.5px; }
.head { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: baseline; }
.place { font-weight: 600; font-size: 15px; }
.pills { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 0; padding: 0; }
.pill { display: inline-block; font-size: 12.5px; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--soft); color: var(--ink); }
.pill.accent { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.pill.pass { background: var(--pass-bg); border-color: var(--pass); color: var(--pass); }
.pill.warn { background: var(--warn-bg); border-color: var(--warn); color: var(--warn); }
.pill.fail { background: var(--fail-bg); border-color: var(--fail); color: var(--fail); }
.said { margin: 10px 0 0; font-size: 14.5px; }
h2 { font-size: 17px; margin: 0; }
h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
  margin: 18px 0 6px; font-weight: 600; }
details.more > summary { cursor: pointer; font-size: 13.5px; color: var(--accent);
  margin-top: 12px; list-style: none; }
details.more > summary::-webkit-details-marker { display: none; }
.s-open { display: none; }
details[open] > summary .s-open { display: inline; }
details[open] > summary .s-closed { display: none; }
pre { margin: 0; padding: 10px 12px; background: var(--code); border: 1px solid var(--line);
  border-radius: 8px; font-size: 12.5px; line-height: 1.45; white-space: pre-wrap;
  word-break: break-word; max-height: 400px; overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.signals { margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 6px 14px; }
.signals div { font-size: 13.5px; }
.signals dt { color: var(--muted); font-size: 12px; }
.signals dd { margin: 0; }
.piece { border-top: 1px solid var(--line); margin-top: 16px; padding-top: 14px; }
.piece-head { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: baseline; }
.piece-title { font-weight: 600; font-size: 14.5px; }
.meta { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
.attempt { display: flex; flex-wrap: wrap; gap: 4px 10px; font-size: 13px; margin-top: 6px; }
.attempt .who { font-weight: 600; }
.muted { color: var(--muted); }
.note { font-size: 13.5px; margin: 6px 0 0; }
details.order > summary { cursor: pointer; font-size: 13px; color: var(--accent); margin-top: 10px; }
.scores { margin: 8px 0 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap;
  gap: 4px 12px; font-size: 13px; }
.empty { color: var(--muted); padding: 24px 0; }
.hint { color: var(--muted); font-size: 12.5px; }
footer { color: var(--muted); font-size: 12.5px; margin-top: 24px; }
@media (max-width: 480px) {
  header { padding-top: 28px; }
  h1 { font-size: 24px; }
  .counts { grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }
}
"""

JS = """
(function () {
  var box = document.getElementById("q");
  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip"));
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var shown = document.getElementById("shown");
  var filter = "all";

  function apply() {
    var text = (box.value || "").trim().toLowerCase();
    var visible = 0;
    cards.forEach(function (card) {
      var wanted = filter === "all" ||
        (" " + (card.getAttribute("data-tags") || "") + " ").indexOf(" " + filter + " ") >= 0;
      // textContent only: the log's own text is never handed to innerHTML.
      var hit = !text || (card.textContent || "").toLowerCase().indexOf(text) >= 0;
      card.hidden = !(wanted && hit);
      if (!card.hidden) { visible += 1; }
    });
    shown.textContent = visible + (visible === 1 ? " card" : " cards");
  }

  box.addEventListener("input", apply);
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      chips.forEach(function (other) {
        other.classList.remove("on");
        other.setAttribute("aria-pressed", "false");
      });
      chip.classList.add("on");
      chip.setAttribute("aria-pressed", "true");
      filter = chip.getAttribute("data-filter") || "all";
      apply();
    });
  });
  apply();
})();
"""

CHIPS = (("all", "All"), ("claude", "Claude"), ("open", "Open-model lanes"),
         ("paid", "Pay-as-you-go"), ("fail", "Failed/Redo"))


MORE = ('<summary><span class="s-closed">Show details</span>'
        '<span class="s-open">Hide details</span></summary>')


def pill(text: str, tone: str = "") -> str:
    return f'<span class="pill{" " + tone if tone else ""}">{text}</span>'


def pre(text) -> str:
    return f"<pre>{e(text)}</pre>"


def render_attempt(worker: dict) -> str:
    """One try by one model: who, which try, how it ended, how long it took."""
    attempt = worker.get("attempt")
    which = f"try {e(attempt)}" if attempt not in (None, "") else "try " + MISSING
    return (f'<div class="attempt"><span class="who">{label(worker, "label")}</span>'
            f'<span class="muted">{which}</span>'
            f'<span class="pill {exit_tone(worker.get("exit"))}">{exit_words(worker.get("exit"))}</span>'
            f'<span class="muted">{seconds(worker.get("seconds"))}</span></div>')


def render_check(check: dict) -> str:
    """Jev's own quality check of a finished piece."""
    scores = check.get("scores") if isinstance(check.get("scores"), dict) else {}
    rows = "".join(f'<li><span class="muted">{e(str(key).replace("_", " "))}</span> '
                   f'{e(value)}</li>' for key, value in sorted(scores.items()))
    verdict = str(check.get("verdict") or "")
    tone = "pass" if verdict == "pass" else ("fail" if verdict == "fail" else "warn")
    nxt = f'<p class="note">{e(check.get("next"))}</p>' if check.get("next") else ""
    return (f'<h3>Jev’s quality check</h3>{pill(e(verdict), tone) if verdict else pill(MISSING)}'
            f'<ul class="scores">{rows or f"<li class=\"muted\">{MISSING}</li>"}</ul>'
            f'<p class="note muted">{local_time(check.get("ts"))}</p>{nxt}')


def render_review(review: dict) -> str:
    """Claude's verdict on the piece, and the reason."""
    verdict = str(review.get("verdict") or "")
    tone = "pass" if verdict == "pass" else ("warn" if verdict == "redo" else "fail")
    note = f'<p class="note">{e(review.get("note"))}</p>' if review.get("note") else ""
    return (f'<h3>Claude’s review</h3>{pill(e(verdict), tone) if verdict else pill(MISSING)}'
            f'<p class="note muted">{local_time(review.get("ts"))}</p>{note}')


def render_piece(piece: dict, grouped: dict) -> str:
    """One delegated job: the order sent, every attempt, the check, the review."""
    piece_id = piece.get("id")
    attempts = piece.get("attempts") or []
    step = piece.get("step")
    try_line = "" if step in (None, "") else (
        "first try" if step_of(piece) == 1 else ("redo" if step_of(piece) == 2 else "escalation"))
    facts = [DECISION_WORDS.get(str(piece.get("decision")), None) or e(piece.get("decision")),
             try_line and f"try {e(step)} ({try_line})",
             piece.get("seconds") is not None and f"took {seconds(piece.get('seconds'))}",
             piece.get("escalate") and "asked for more (escalated)",
             piece.get("claude_tier") and f"Claude model: {e(piece.get('claude_tier'))}",
             piece.get("level") and f"depth {e(piece.get('level'))}",
             piece.get("exit") is not None and f"worker exit {e(piece.get('exit'))}"]
    meta = " · ".join(str(part) for part in facts if part)
    why = f'<p class="note muted">Why: {e(piece.get("why"))}</p>' if piece.get("why") else ""
    order = (f'<details class="order"><summary>Work order sent</summary>{pre(piece.get("order_text"))}'
             f'</details>') if piece.get("order_text") else ""
    tries = "".join(render_attempt(worker) for worker in attempts)
    tries = f"<h3>Attempts</h3>{tries}" if tries else ""
    checks = "".join(render_check(check) for check in (grouped["checks"].get(piece_id) or [])) \
        if piece_id else ""
    reviews = "".join(render_review(review) for review in (grouped["reviews"].get(piece_id) or [])) \
        if piece_id else ""
    provider = pill("Jev via OpenRouter", "warn") if piece.get("jev_provider") == "openrouter" else ""
    return (f'<div class="piece"><div class="piece-head">'
            f'<span class="piece-title">{e(piece.get("title"))}</span>'
            f'{pill(label(piece, "where"))}{provider}</div>'
            f'<p class="meta">{meta or MISSING}</p>{why}{order}{tries}{checks}{reviews}</div>')


def render_group(pieces: list[dict], loose: list[dict], grouped: dict) -> str:
    """The jobs of one card, with attempts that belong to no job listed after them."""
    parts = [render_piece(piece, grouped) for piece in pieces]
    if loose:
        parts.append("<h3>Attempts</h3>" + "".join(render_attempt(worker) for worker in loose))
    return "".join(parts)


def render_route(window: dict, grouped: dict) -> str:
    """One card per prompt: what was sent, where it went, what came of it."""
    route = window["route"]
    pieces, loose = window["pieces"], window["loose"]
    workers = loose + [worker for piece in pieces for worker in piece.get("attempts") or []]
    prompt = route.get("prompt")
    skills = "".join(pill(f"{e(name)} {percent(probability)}")
                     for name, probability in pairs(route.get("skills")))
    provider = pill("Jev via OpenRouter", "warn") if route.get("jev_provider") == "openrouter" else ""
    sent = pill(route_label(route), "accent")
    signals = "".join(f"<div><dt>{title}</dt><dd>{signal(route, key)}</dd></div>"
                      for key, title in SIGNALS)
    context = (f"<h3>What Jev added</h3>{pre(route.get('context'))}"
               if route.get("context") else "")
    body = render_group(pieces, loose, grouped)
    return (f'<article class="card" data-tags="{tags(pieces, workers, route=route)}">'
            f'<div class="head"><span class="when">{local_time(route.get("ts"))}</span>'
            f'<span class="place">{folder(route.get("cwd"))}</span></div>'
            f'<div class="pills">{sent}{skills}{provider}</div>'
            f'<p class="said">{snippet(prompt)}</p>'
            f'<details class="more">{MORE}'
            f"<h3>What you sent</h3>{pre(prompt)}"
            f'<h3>What Jev saw</h3><dl class="signals">{signals}</dl>'
            f"{context}{body}</details></article>")


def render_orphans(grouped: dict) -> str:
    """Jobs and attempts with no prompt behind them in this period."""
    orphans = grouped["orphans"]
    if not orphans["pieces"] and not orphans["loose"]:
        return ""
    workers = orphans["loose"] + [w for p in orphans["pieces"] for w in p.get("attempts") or []]
    first = min([row.get("ts") or "9999" for row in orphans["pieces"] + workers], default="")
    return (f'<article class="card" data-tags="{tags(orphans["pieces"], workers)}">'
            f'<div class="head"><span class="when">{local_time(first)}</span>'
            f'<h2>Jobs started outside a prompt</h2></div>'
            f'<p class="said muted">No prompt in this period was within six hours before these jobs started.</p>'
            f'<details class="more">{MORE}'
            f'{render_group(orphans["pieces"], orphans["loose"], grouped)}</details></article>')


def render_limit(limit: dict) -> str:
    """Claude ran out of allowance: what it said and what was chosen instead."""
    choice = str(limit.get("choice") or "")
    chosen = CHOICE_WORDS.get(choice) or (e(choice) if choice else MISSING)
    detail = " · ".join(str(part) for part in
                        [limit.get("model") and f"model: {e(limit.get('model'))}",
                         limit.get("error") and f"{e(limit.get('error'))}"] if part)
    return (f'<article class="card limit" data-tags="{tags([], [], limit=limit)}">'
            f'<div class="head"><span class="when">{local_time(limit.get("ts"))}</span>'
            f'<h2>Claude hit its limit</h2></div>'
            f'<p class="said">{e(limit.get("message"))}</p>'
            f'<p class="note">What happened next: {chosen}</p>'
            f'<p class="note muted">{detail or MISSING}</p></article>')


def pairs(skills) -> list[tuple]:
    """`skills` arrives as a list of [name, probability]; anything else is ignored."""
    if not isinstance(skills, list):
        return []
    return [(item[0], item[1]) for item in skills
            if isinstance(item, (list, tuple)) and len(item) >= 2]


def percent(probability) -> str:
    try:
        number = float(probability)
    except (TypeError, ValueError):
        return MISSING
    return f"{round(number * 100) if number <= 1 else round(number)}%"


def signal(route: dict, key: str) -> str:
    value = route.get(key)
    return e(value) + (score_word(value) if key in NUMERIC else "")


def render(grouped: dict, days: int) -> str:
    """The whole page, newest card first."""
    cards = [(window["route"].get("ts") or "", render_route(window, grouped))
             for window in grouped["windows"]]
    cards += [(limit.get("ts") or "", render_limit(limit)) for limit in grouped["limits"]]
    orphans = render_orphans(grouped)
    if orphans:
        cards.append(("", orphans))  # the leftovers sit at the bottom, oldest end
    cards.sort(key=lambda pair: pair[0], reverse=True)
    body = "".join(markup for _, markup in cards) \
        or '<p class="empty">Nothing was logged in this period.</p>'

    stats = "".join(f'<div class="count"><b>{value}</b><span>{e(title)}</span></div>'
                    for title, value in counts(grouped["rows"], grouped))
    chips = "".join(f'<button class="chip{" on" if key == "all" else ""}" type="button" '
                    f'data-filter="{key}" aria-pressed="{"true" if key == "all" else "false"}">'
                    f'{e(title)}</button>' for key, title in CHIPS)
    now = datetime.now().astimezone()
    generated = f"{now.strftime('%a')} {now.day} {now.strftime('%b %Y, %H:%M')}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jev routing</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
<h1>Jev routing</h1>
<p class="sub">Last {e(days)} days · generated {e(generated)}</p>
</header>
<div class="counts">{stats}</div>
<div class="tools">
<label for="q" class="hint">Search every card</label>
<input id="q" type="search" placeholder="Search prompts, models, skills, verdicts…">
<div class="chips">{chips}<span class="shown" id="shown"></span></div>
</div>
{body}
<footer>Written by jev-dashboard.py from {e(LOG)}. Nothing here was sent anywhere.</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write one HTML page of every Jev routing decision.")
    parser.add_argument("--out", default=OUT, help=f"where to write the page (default {OUT})")
    parser.add_argument("--days", type=int, default=7, help="how far back to look (default 7)")
    parser.add_argument("--open", action="store_true", dest="open_it",
                        help="open the page in the browser when it is written")
    args = parser.parse_args(argv)

    since = cutoff(args.days)
    rows = [row for row in read_log() if (row.get("ts") or "") >= since]
    page = render(group(rows), args.days)

    out = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(page)

    cards = page.count('<article class="card')
    print(f"{len(rows)} log rows in the last {args.days} days -> {cards} cards.")
    if args.open_it:
        subprocess.call(["open", out])
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
