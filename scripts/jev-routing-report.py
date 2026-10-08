#!/usr/bin/env python3
"""
jev-routing-report.py — summarise the routing log before switching lanes live.

    python3 jev-routing-report.py [--since 2026-09-26] [--examples 3]

Reads ~/.config/typesafe/jev-routing.jsonl (or $JEV_ROUTING_LOG), written by
jev-refine.py (event=route) and cline-worker.sh (event=worker).
"""

import argparse
import collections
import json
import os

LOG = os.environ.get("JEV_ROUTING_LOG", os.path.expanduser("~/.config/typesafe/jev-routing.jsonl"))


def short(model: str, backup: str = "") -> str:
    """Short model name. Runs on a backup (Codex, pay-as-you-go) count apart from ClinePass."""
    model = model or "-"
    name = model.split("/")[-1]
    if model.startswith("codex/"):
        return f"{name} (Codex)"
    if backup and "Codex" in backup:
        return "Codex lane"
    if backup:
        return f"{name} (pay-as-you-go)"
    if "/" in model and not model.startswith("cline-pass/"):
        return f"{name} ({model.split('/')[0]})"
    return name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="", help="ISO date; older entries are ignored")
    parser.add_argument("--examples", type=int, default=3, help="sample prompts per lane")
    args = parser.parse_args()

    routes, workers, pieces, reviews = [], [], [], {}
    for path in (LOG + ".1", LOG):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ts", "") < args.since:
                    continue
                event = row.get("event")
                if event == "route":
                    routes.append(row)
                elif event == "piece":
                    if row.get("decision") != "dry-run":  # rehearsals aren't planned pieces
                        pieces.append(row)
                elif event == "review":
                    reviews[row.get("id")] = row.get("verdict")  # latest verdict wins
                elif event in ("worker", "image"):  # "check" rows are Jev's scores, not runs
                    workers.append(row)

    if not routes and not workers and not pieces:
        print(f"No entries in {LOG}")
        return

    print(f"Prompts judged: {len(routes)}")
    apps = collections.Counter(r.get("app") or "unknown (before 2026-09-26)" for r in routes)
    print("Where you worked:  " + ", ".join(f"{k} {v}" for k, v in apps.most_common()))
    by_route = collections.Counter(r.get("route") or "none" for r in routes)
    print("Where they went:  " + ", ".join(f"{k} {v}" for k, v in by_route.most_common()))

    lane = [r for r in routes if (r.get("route") or "").startswith("cline-")]
    print(f"\nOpen-model lane: {len(lane)} prompts")
    picks = collections.defaultdict(list)
    for r in lane:
        picks[(r.get("kind") or "?", short(r.get("lane_pick")))].append(r)
    for (kind, model), rows in sorted(picks.items(), key=lambda kv: -len(kv[1])):
        differs = sum(1 for r in rows if r.get("lane_pick") != r.get("used_model"))
        print(f"  {kind:13} -> {model:16} {len(rows):3}  (would change model on {differs})")
        for r in rows[: args.examples]:
            print(f"      \"{(r.get('prompt') or '')[:90]}\"")

    if workers:
        print("\nWorker runs by model:")
        stats = collections.defaultdict(lambda: {"runs": 0, "ok": 0, "timeouts": 0, "stuck": 0, "retries": 0,
                                                 "secs": 0, "refused": 0})
        for w in workers:
            s = stats[short(w.get("model"))]
            # Exit 75: the lane refused (limit, login, credit) and switched itself off, so
            # the job moved on. That is the lane being down, not the model failing the job.
            if w.get("exit") == 75:
                s["refused"] += 1
                continue
            s["runs"] += 1
            s["ok"] += w.get("exit") == 0
            s["timeouts"] += w.get("exit") == 142
            s["stuck"] += w.get("exit") == 143
            s["retries"] += (w.get("attempt") or 1) >= 2
            s["secs"] += int(w.get("seconds") or 0)
        for model, s in sorted(stats.items(), key=lambda kv: -kv[1]["runs"]):
            print(f"  {model:30} runs {s['runs']:3}  ok {s['ok']:3}  timeouts {s['timeouts']:2}  "
                  f"stuck {s['stuck']:2}  as retry {s['retries']:2}  avg {s['secs'] // max(s['runs'], 1)}s"
                  f"  refused {s['refused']:2}")

    if pieces:
        built = [p for p in pieces if p.get("decision") == "cline"]
        kept = [p for p in pieces if p.get("decision") == "claude"]
        print(f"\nPlanned pieces: {len(pieces)}  built on the open-model lane {len(built)}  kept with Claude {len(kept)}")
        quality = collections.defaultdict(collections.Counter)
        for p in built:
            quality[short(p.get("model"), p.get("backup", ""))][reviews.get(p.get("id"), "unchecked")] += 1
        for model, c in sorted(quality.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {model:30} pass {c['pass']:3}  redo {c['redo']:3}  fail {c['fail']:3}  unchecked {c['unchecked']:3}")
        tiers = collections.Counter(p.get("claude_tier") or "?" for p in kept)
        if kept:
            print("  sent back to Claude on: " + ", ".join(f"{t} {n}" for t, n in tiers.most_common()))
        whys = collections.Counter((p.get("why") or "").split(" -- ")[-1] for p in kept)
        for why, n in whys.most_common(3):
            print(f"  kept: {why} ({n})")


if __name__ == "__main__":
    main()
