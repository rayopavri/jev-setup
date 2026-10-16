# jev-interceptor

A `UserPromptSubmit` hook that routes every prompt you send. It asks
[TypeSafe Jev](https://docs.typesafe.ai) a few typed questions about the prompt, then
tells Claude where the work should run: on Claude, or on an open-model lane
(ClinePass or Codex). The answers go back as `additionalContext`, and one line tells
you which model got the job.

Jev never writes text. It returns calibrated probabilities. This hook turns those
numbers into routing; all the wording and every threshold live in the scripts, not
in the model.

## Install on a new Mac

This section is for someone who does not write code. Follow the steps in order. Each step is one action. Copy and paste the commands exactly.

A "terminal" below means the Terminal app on your Mac. "Claude Code" means the Claude Code program you already use.

### What you need before you start

Tick each one off. Only the first group is required. Everything else switches on a feature, and the "Without it" column says what you lose.

**Required**

| Tick | What | What it is for | Where to get it | Where it is stored |
|---|---|---|---|---|
| [ ] | **Claude Code** | The program this plugin runs inside. | Your existing Claude Code install. | Nothing to store. |
| [ ] | **git** | Downloads this repo. | Already on most Macs (`/usr/bin/git`). | Nothing to store. |
| [ ] | **Python 3.10 or newer** | Every script here is Python. No extra packages needed. | python.org, or any Python 3 you already have. | Nothing to store. |
| [ ] | **A TypeSafe API key** | Lets Jev judge each prompt. | <https://console.typesafe.ai/keys> | `TYPESAFE_API_KEY` in the `env` block of `~/.claude/settings.json` (Step 4). |

Without a TypeSafe key, and without an OpenRouter key, the hook does nothing and your prompt goes through untouched. With an OpenRouter key but no TypeSafe key, Jev is asked through OpenRouter instead.

**Optional**

| Tick | What | What it is for | Without it | Where to get it | Where it is stored |
|---|---|---|---|---|---|
| [ ] | **`jev` command** (the TypeSafe command-line tool) | Skill ranking, and the spend log (`jev usage`). | Triage still works by a direct request. Skill ranking is skipped. | Not included in this repo (Step 4). | Your key: `jev auth set paste-your-key-here` saves it to `~/.config/typesafe/api_key`. Or it reads `TYPESAFE_API_KEY`. |
| [ ] | **Node and npm** (install Node with `nvm`) | `pi` is a Node program. The scripts look for `pi` inside `~/.nvm/versions/node/*/bin`. | No ClinePass lane. | nvm: <https://github.com/nvm-sh/nvm> | Nothing to store. |
| [ ] | **`pi` agent command** | Runs the jobs on ClinePass. | No ClinePass lane. Work stays on Claude or Codex. | npm (Step 5). | Its login is saved in `~/.pi/agent/auth.json`. |
| [ ] | **ClinePass subscription** (a Cline account at app.cline.bot) | The flat-fee lane for Kimi K3, DeepSeek and others. | The ClinePass lane stays off. | app.cline.bot | Log in once with `pi /login`. |
| [ ] | **Kimi Code membership** (a Moderato, Allegretto or higher plan at kimi.com/code) | The free first lane for Kimi K3 jobs (1M context from Allegretto up). | Kimi jobs run on ClinePass. | kimi.com/code | Log in once with `pi /login`, then pick Kimi Code. The login lives in `~/.pi/agent/auth.json`. |
| [ ] | **Codex command and a ChatGPT plan** | The Codex lane (GPT-6-Luna) and the image tool. | No Codex lane, no image route. | OpenAI Codex: <https://github.com/openai/codex>, with your ChatGPT login | Log in with `codex login`. The login lives in `~/.codex/auth.json`. |
| [ ] | **Cline credits key** | Pay-as-you-go backup 1, used only while ClinePass is at its limit. | No Cline-credits backup. | app.cline.bot, then Settings, then API Keys | `python3 set-backup-key.py cline` saves it in `~/.pi/agent/auth.json`. Or set `CLINE_CREDITS_API_KEY`. |
| [ ] | **OpenRouter key** | Pay-as-you-go backup 2. Also asks Jev when TypeSafe is down, and powers the "Claude ran out" prompt. | No second backup. No fallback if TypeSafe is down. | openrouter.ai, then Settings, then Keys | `python3 set-backup-key.py openrouter` saves it in `~/.pi/agent/auth.json`. Or set `OPENROUTER_API_KEY`. |

Notes:

- Python is "standard library only", so there is nothing to `pip install`.
- You do not need all lanes. One lane is enough. Skipped lanes are skipped automatically.
- `uuidgen` and `perl` are used by `check-tools.sh`. Both come with macOS.

### Quick install (one command)

This repo lists every Jev piece of the original Mac in `setup/manifest.json`, and one command installs them all: the router, the skills, the plugins (with their settings), the MCP servers, the browser tool, the ClinePass tools and the routing rules. It asks for your TypeSafe key (hidden as you paste it) and saves it only in `~/.claude/settings.json`. Re-run it any time; finished steps are skipped.

```
mkdir -p ~/.claude/skills
git clone https://github.com/rayopavri/jev-setup.git ~/.claude/skills/jev-interceptor
~/.claude/skills/jev-interceptor/setup.sh install
```

It ends with the few things it can't do for you: logging in to ClinePass and Codex, the optional pay-as-you-go keys, and restarting Claude Code. Add `--dry-run` to see what it would do without changing anything. Other commands:

- `setup.sh check`: what is installed and what is missing.
- `setup.sh sync`: on the original Mac, copy its live skills and hooks back into this repo so the backup stays current. Then commit and push.
- `setup.sh publish`: build the public copy (without the private pieces) and push it.

The `jev` command, the `jev` skill and the rule guard from the original Mac are not included (they have no licence that allows sharing), and `setup.sh` skips them. Alternatives: TypeSafe's official plugin `typesafe@typesafe-ai` (installed by `setup.sh`) teaches Claude to call Jev, and [Abide](https://github.com/coldteadotai/abide) or [Toolgate](https://github.com/RiskAverseTech/toolgate) can take the rule guard's place.

### Manual install (what the setup command does, step by step)

**Step 1. Download the repo to the right place.** It must go inside `~/.claude/skills`.

```
mkdir -p ~/.claude/skills
git clone https://github.com/rayopavri/jev-setup.git ~/.claude/skills/jev-interceptor
```

**Step 2. Switch the hooks on.** There is nothing to type. The folder has a `.claude-plugin/plugin.json` file, so Claude Code treats it as a plugin, and its `hooks/hooks.json` wires two hooks: one on every prompt (`UserPromptSubmit`) and one for when Claude hits its usage limit (`StopFailure`). Quit Claude Code fully and open it again. (To test without restarting your normal setup, you can start it once with `claude --plugin-dir ~/.claude/skills/jev-interceptor`.)

**Step 3. Put your Jev key in Claude Code's settings.** Open `~/.claude/settings.json` in a text editor:

```
open -e ~/.claude/settings.json
```

Find the `"env"` block, or add one. Add the key inside it. Replace the placeholder with your real key:

```
"env": {
  "TYPESAFE_API_KEY": "paste-your-key-here"
}
```

If the file already has an `"env"` block, add a comma and put the new line inside it. Do not make a second `"env"`. Never share this file.

**Step 4. The `jev` command (optional, not included).** If you have a `jev` command on your `PATH`, the hook uses it to record spend and to rank skills. Without it the hook still works: it asks Jev directly, and ranks skills through OpenRouter when you have that key. For TypeSafe's official agent skill, see <https://github.com/typesafe-ai/skills>.

**Step 5. Set up ClinePass (skip if you have no ClinePass plan).**

1. Install Node with nvm (<https://github.com/nvm-sh/nvm>), then run `nvm install --lts`. The scripts only look for `pi` inside nvm's folder.
2. Install `pi` and its ClinePass add-on:

```
npm install -g @earendil-works/pi-coding-agent
pi install npm:pi-clinepass-provider
```

3. Log in:

```
pi /login
```

4. If you ever get a "ClinePass login expired" message, repeat the login, then run:

```
python3 ~/.claude/skills/jev-interceptor/scripts/cline_pause.py clear
```

**Step 6. Set up Codex (skip if you have no ChatGPT plan with Codex).** Install Codex from <https://github.com/openai/codex>, then log in:

```
codex login
```

**Step 7. Add the pay-as-you-go backups (optional).** Each command asks you to paste a key. The key does not show on screen. The script checks the key, saves it, then runs a small tool test that costs roughly 30 cents. Run only the ones you want.

```
cd ~/.claude/skills/jev-interceptor/scripts
python3 set-backup-key.py cline
python3 set-backup-key.py openrouter
```

To turn one off later:

```
python3 cline_pause.py backup off cline
```

(Use `openrouter` instead of `cline` for the other one.)

**Step 8. Tell Claude to follow Jev's note.** This adds a ready-made section, `Jev routing (all projects)`, to your personal Claude rules file. Run it once only:

```
cat ~/.claude/skills/jev-interceptor/claude-md-snippet.md >> ~/.claude/CLAUDE.md
```


**Step 9. Install smart compaction (`fast-jev-compaction`).** Optional. This is a copy of [tamaratran/fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction) (MIT).

1. Copy the folder:

```
cp -R ~/.claude/skills/jev-interceptor/jev-pack ~/.claude/jev-pack
```

2. It needs Claude Code's early-access "function hooks". Add this line to the same `"env"` block as Step 3:

```
"CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"
```

3. Then run these two commands:

```
claude plugin marketplace add ~/.claude/jev-pack/plugins/fast-jev-compaction
claude plugin install fast-jev-compaction@fast-jev-compaction
```

It reads `TYPESAFE_API_KEY` from the same `env` block. If anything goes wrong it falls back to Claude's normal compaction.

**Step 10. Install the companion skills (optional).** The `companions/` folder holds copies of two related skills. Copy the ones you want:

```
cp -R ~/.claude/skills/jev-interceptor/companions/jev-browser ~/.claude/skills/jev-browser
cp -R ~/.claude/skills/jev-interceptor/companions/jev-skill-suggestion ~/.claude/skills/jev-skill-suggestion
```

- `jev-browser`: how to use [Ying-Kai Liao's jev-browser](https://github.com/Ying-Kai-Liao/jev-browser) (MIT). This copy is only the instructions. Install the browser tool itself like this:

  ```
  git clone https://github.com/Ying-Kai-Liao/jev-browser ~/.local/share/jev-browser
  cd ~/.local/share/jev-browser && npm install
  claude mcp add --scope user jev-browser -- node ~/.local/share/jev-browser/bin/jev-browser-mcp.mjs
  ```
- `jev-skill-suggestion`: lets Jev pick one skill per prompt. Needs `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` (same line as in Step 9). It takes its own key as a plugin option, `typesafeApiKey`, or a Vercel AI Gateway key, `gatewayApiKey`. With neither it uses Claude's built-in classifier.

**Step 11. Install the community Jev plugins (optional).** These are other people's projects (all MIT), used alongside this one. Install them from their own repos:

| Plugin | What it does | Install |
|---|---|---|
| [Jev Belay](https://github.com/valentynkit/jev-belay) | A Stop hook that blocks an unverified "done": it reads the transcript for evidence first. | `claude plugin marketplace add valentynkit/jev-belay` then `claude plugin install jev-belay@jev-belay` |
| [Jev Steer or Queue](https://github.com/Larkspur-Wang/Jev_steer_or_queue) | Decides whether a message you send mid-turn should steer, queue or interrupt. | `claude plugin marketplace add Larkspur-Wang/Jev_steer_or_queue` then `claude plugin install jev-steer-or-queue@jev-steer-or-queue` |
| [Compact Adviser](https://github.com/kunchenguid/compact-adviser) | Suggests `/compact` when work looks finished, to save tokens. | `claude plugin marketplace add kunchenguid/compact-adviser` then `claude plugin install compact-adviser@compact-adviser` |

TypeSafe's official plugin, [typesafe@typesafe-ai](https://github.com/typesafe-ai/skills), teaches Claude to call Jev well: `claude plugin marketplace add typesafe-ai/skills` then `claude plugin install typesafe@typesafe-ai`.

The `jev` MCP server, [jev-use](https://github.com/shitianfang/jev-use) (MIT), hands steps that need no text output to Jev: `claude mcp add -s user jev -- npx -y jev-use@0.7.1 serve`.

The browser tool, [jev-browser](https://github.com/Ying-Kai-Liao/jev-browser), is covered in Step 10.

**Step 12. Restart Claude Code** once more so everything loads.

### Check it works

Run these in order. You can ask Claude to run each one for you.

**1. The health check.**

```
sh ~/.claude/skills/jev-interceptor/scripts/check-setup.sh
```

Success: every line starts with a tick, and the last line says `All good.` A cross means something needs attention, and the last line says so.

Things to know:

- It reads your Jev key from the session, so run it from inside Claude Code. In a plain terminal it will say the key is missing unless you export `TYPESAFE_API_KEY` there.
- It also checks for the CLAUDE.md section from Step 8.
- If you skipped ClinePass, the ClinePass lines show a cross. That is fine.
- The last Jev decision line only turns green after you have sent at least one prompt.

**2. Is ClinePass paused?**

```
python3 ~/.claude/skills/jev-interceptor/scripts/cline_pause.py status
```

Success: it prints `ClinePass is open.`

If it says paused, it also says why and until when. After you fix the cause (for example, you logged in again), run `python3 ~/.claude/skills/jev-interceptor/scripts/cline_pause.py clear`.

**3. Can the models really use tools? (ClinePass only.)**

```
sh ~/.claude/skills/jev-interceptor/scripts/check-tools.sh
```

Each model must read a file, run a command and write a file. Success: one `PASS` line per model. A `FAIL` line gives the path of a log to read. This can take a few minutes.

**4. Did it route a real prompt?** Send Claude a normal sentence of a few words (short prompts and slash commands are skipped on purpose). You should see one line like `Jev → ...` naming the model. After a few prompts, run:

```
python3 ~/.claude/skills/jev-interceptor/scripts/jev-routing-report.py
```

Success: it prints `Prompts judged:` followed by a number that goes up as you work.

For a picture of everything, run `python3 ~/.claude/skills/jev-interceptor/scripts/jev-dashboard.py --open`.

### Turn it off

Set these in the `"env"` block of `~/.claude/settings.json` (same place as the key), then restart Claude Code. Remove the line to turn it back on.

| Setting | What it does |
|---|---|
| `"JEV_CLINE": "0"` | Turns off all lane routing. Jobs stay on Claude. |
| `"JEV_PLAN_DELEGATE": "0"` | Turns off the "Claude plans, a lane builds" route. |
| `"JEV_LANES": "shadow"` | Logs which lane it would pick, but always uses the default lane. |
| `"JEV_LANES": "off"` | Skips the lane pick entirely. |
| `"JEV_ROUTER": "0"` | Turns off Claude model-size suggestions. |
| `"JEV_CODEX": "0"` | Takes the Codex lane out. |
| `"JEV_RANK_SKILLS": "0"` | Turns off skill ranking. |

To remove it completely, delete the `~/.claude/skills/jev-interceptor` folder and restart Claude Code. The hook never blocks a prompt: if anything fails it passes your prompt through untouched.

## What it does on every prompt

1. **Triage**: what kind of task this is, whether it needs the repo, whether it is
   vague, whether it is risky (hard to undo, or reaches outside this machine).
2. **Routing**: picks one route (below) and names the model.
3. **Skill ranking**: grades every installed skill, slash command and agent against the
   prompt with `jev rank` and lists the ones that fit.
4. **Catalog**: notices newly installed or changed skills, commands and agents and asks
   Claude to review them (`scripts/jev_catalog.py`).

Triage and ranking run at the same time, so the hook costs one round trip of wall
time. Expect roughly 1 s per prompt with ranking on and 0.5 s with it off.

You see one line per prompt, for example:

```
Jev → Claude opus (Opus 5.5) plans it; builds on DeepSeek V4.1 Flash on ClinePass, then DeepSeek V4 Pro if it falls short
```

## Routes

The first route that fits wins.

| Route | When | What happens |
|---|---|---|
| Image | The job is to make an image | Codex's image tool on the ChatGPT plan (`codex-image.py`); OpenRouter's image tool if Codex refuses |
| Lane | Light or standard, offload ≥ 0.70, risk < 0.50 | Claude writes a self-contained task and runs `cline-worker.sh`; Claude reviews the diff |
| Plan then delegate | Build work ≥ 0.60, risk < 0.50, not a pure explanation | Claude plans and writes work orders; `delegate.py` builds each one on a lane |
| Claude | Everything else, and all design work | A Claude subagent at the tier Jev sizes |

### Claude tiers

Jev sizes the job: light → Haiku 4.5, standard → Sonnet 5.5, deep → Opus 5.5.
Risk ≥ 0.70 raises it to Opus; vagueness ≥ 0.70 raises it to at least Sonnet.

### Lanes

| Task kind | Lane model |
|---|---|
| UI component | Kimi K3 (Kimi Code plan, then ClinePass) |
| Full UI page | Kimi K3 (Kimi Code plan, then ClinePass) |
| Logic and code | GPT-6-Luna (Codex, the ChatGPT plan) |
| Long session | Kimi K3 (Kimi Code plan, then ClinePass) |
| Design (taste, look and feel, critique) | Always Claude |
| Image | Codex image tool |
| Other, or Jev under 60% sure | DeepSeek V4 Pro (ClinePass) |

The lanes were re-pointed on 2026-10-05 after a benchmark and price review. Kimi K3
led the non-Claude models on UI and websites; the Qwen Max models trailed.

On 2026-10-16 the user's own Kimi Code membership (Allegretto) became the first stop for
Kimi K3 jobs: the same K3 model, flat fee, through pi's built-in `kimi-coding` provider
(`kimi-coding/k3`). ClinePass's Kimi K3 is next, so ClinePass's 5-hour cap is kept for
the DeepSeek jobs. When another lane's job fails and Jev's stand-in (Kimi K3) takes a
try, it also runs on the Kimi Code plan when that is up. The plan has its own pause
(`kimi-pause.json`): a limit pauses it until the stated reset, a 401 for a day.

## Small first

Every job starts on the smallest model likely to do it well, and moves up only when
the result falls short. A failed attempt still costs tokens, so "small" means small
within the right family, not the cheapest model overall.

- **ClinePass**: a lane model's smaller ClinePass sibling runs first.
  DeepSeek V4 Pro jobs start on DeepSeek V4.1 Flash. Kimi K3 has no smaller sibling
  there, so Kimi jobs start on K3. (`CLINE_SMALL` in `cline_pause.py`.) This is only
  for ClinePass, where every model costs the same flat plan. Pay-as-you-go picks the
  small model by price instead, so its ladder can differ (next section).
- **Codex**: GPT-6-Luna is already the small model. A redo asks it for more
  reasoning effort.
- **Claude**: starts at Jev's tier and goes one tier up only if the result falls
  short. Risky jobs start at Jev's tier, never smaller.

## When a lane is at its limit

The job runs pay-as-you-go: Cline credits first, then OpenRouter. It stays in the same
model family. Here each job is billed per token, so the first try is the model that
costs least per job, not always the smallest one. That is why DeepSeek uses V4 Pro only
here, while on ClinePass it tries V4.1 Flash first. Some families have one step only.

| Lane model | Pay-as-you-go ladder |
|---|---|
| Kimi K3 | K3 only. Never K2.7 Code: a quarter of the context and weaker scores. |
| DeepSeek V4 Pro | V4 Pro only. On 2026-10-05 Pro cost less per job than V4 Flash on OpenRouter. |
| GPT-6-Luna | Luna, then GPT-6-Sol on a redo |
| Qwen3.8 Max / Qwen3.7 Max | Flash first, then Max (no lane uses Qwen by default) |

The ladders live in `BACKUP_TIERS` in `cline_pause.py`. If nothing can take a job, it
stays with Claude. When Claude itself runs out, `claude-limit.py` asks before any
spend on Claude through OpenRouter.

## Quality gate

A lane result is a draft until it is checked. For work orders:

1. `delegate.py run <order.md>` builds it on the matched lane, small model first.
2. `delegate.py check <id> <result-file>` has Jev score the real result against the
   order's "Done when". Claude also checks the real output.
3. Short of the bar: `run --escalate` redoes it on the full model with sharper feedback.
4. Still short: `run --up` names the next Claude tier up.

Three attempts at most. `delegate.py review <id> pass|redo|fail` records the verdict,
so the dashboard can show how each model holds up.

## Fail-open

Any failure (no key, no network, a timeout, bad JSON, an unexpected shape) exits 0
with empty stdout. Your prompt reaches Claude untouched, so the session can never be
broken or stalled by this hook. If TypeSafe is down, Jev is asked through OpenRouter
instead (`jev_openrouter.py`).

## Commands

All scripts are in `scripts/`.

| Command | What it does |
|---|---|
| `python3 jev-dashboard.py --open` | Every routing decision, attempt, check and review on one local page |
| `python3 jev-routing-report.py` | Scorecard summary of the routing log |
| `sh check-setup.sh` | Plain-English health check |
| `check-tools.sh [model ...]` | Verifies models can read, run and write with tools before they carry work |
| `python3 cline_pause.py status\|clear` | ClinePass pause state |
| `python3 cline_pause.py kimi\|kimi clear` | Kimi Code plan lane state, or lift its pause |
| `python3 cline_pause.py where <model>` | Where a job for that lane model would run right now |
| `python3 set-backup-key.py cline\|openrouter` | Switches on a pay-as-you-go backup |

## Knobs

All are environment variables. All are optional.

| Variable | Default | What it does |
|---|---|---|
| `TYPESAFE_API_KEY` | *(none)* | Jev key. Without it, and without an OpenRouter key, the prompt passes through. |
| `JEV_ROUTER` | `1` | `0` turns Claude tier routing off |
| `JEV_ROUTER_BASELINE` | `opus` | The model you are assumed to be on |
| `JEV_ROUTER_FLOOR` | `haiku` | Never route below this tier |
| `JEV_CLINE` | `1` | `0` turns all lane routing off |
| `JEV_CLINE_OFFLOAD_MIN` | `0.70` | Offload score a prompt needs to go straight to a lane |
| `JEV_PLAN_DELEGATE` | `1` | `0` turns the plan-then-delegate route off |
| `JEV_PLAN_BUILD_MIN` | `0.60` | Build-work score that triggers plan-then-delegate |
| `JEV_LANES` | `live` | `shadow` logs the lane pick but always uses the default; `off` skips it |
| `JEV_LANE_MIN` | `0.60` | Below this task-kind confidence, use the default lane |
| `JEV_CODEX` | `1` | `0` takes the Codex lane out |
| `JEV_CODEX_MODEL` | `gpt-6-luna` | The Codex lane's model |
| `JEV_KIMI` | `1` | `0` takes the Kimi Code plan lane out (Kimi jobs go to ClinePass) |
| `CLINE_DEFAULT_MODEL` | `cline-pass/deepseek-v4-pro` | The worker's model when none is given |
| `CLINE_FALLBACK_MODEL` | `cline-pass/kimi-k3` | The ClinePass stand-in when a lane fails |
| `CLINE_TIMEOUT` | `1200` light / `3600` standard | Seconds a worker may run |
| `JEV_RANK_SKILLS` | `1` | `0` turns skill ranking off |
| `JEV_RANK_MIN` | `0.75` | Lowest score a skill needs to be listed |
| `JEV_RANK_TOP` | `4` | How many skills to list at most |
| `JEV_MODEL` | `jev-latest` | Pin a Jev version |
| `JEV_TIMEOUT` | `8` | Seconds to wait for triage |
| `JEV_ROUTING_LOG` | `~/.config/typesafe/jev-routing.jsonl` | The routing log |
| `JEV_QUIET` | *(unset)* | Any value stops the terminal status line |

A `UserPromptSubmit` hook cannot switch the main model, and the hook payload does not
say which model is active. `JEV_ROUTER_BASELINE` is a setting for that reason. If you
switch models mid-session, the router will not notice.

## Requirements

See [Install on a new Mac](#install-on-a-new-mac): it lists every account, key and
program, which ones are required, and where each key is stored.

## Files

- `scripts/jev-refine.py`: the hook. Triage, routes, lane table (`LANE_MODEL`).
- `scripts/jev_catalog.py`: finds every skill, command and agent (personal, project, installed
  plugins, claude.ai-synced plugins) and tracks which ones were reviewed, in
  `~/.config/typesafe/jev-catalog.json`. `status`, `pending`, `review <name> --line "..."`,
  `review <name> --keep`, `scan --baseline`. Notices are throttled with `JEV_CATALOG_NOTICE_HOURS`
  (default 6); `JEV_CATALOG_NOTICE=0` turns them off.
- `skill-routing.json`: hand-written routing lines for competing design skills (stage, project fit,
  tools, model fit, tie-breakers). Jev ranks against these instead of the skills' own descriptions,
  so `npx skills update` can't undo them.
- `scripts/cline_pause.py`: lane state, small-first siblings, pay-as-you-go ladders.
- `scripts/lane-worker.py` (via `cline-worker.sh`): runs one task on a lane.
- `scripts/delegate.py`: work orders, Jev's quality check, reviews.
- `scripts/claude-limit.py`: the `StopFailure` hook for when Claude runs out.
- `hooks/hooks.json`: wires the two hooks.
- `setup.sh` and `setup/`: the one-command installer (`setup/jev_setup.py`) and the list of
  every piece it installs (`setup/manifest.json`).
- `companions/`: copies of the related skills `jev-browser` and `jev-skill-suggestion`.
  They are snapshots, so copy them again after changing the originals.
  `jev-skill-suggestion` comes from
  [davila7/claude-code-templates](https://github.com/davila7/claude-code-templates) (MIT).
- `claude-md-snippet.md`: the `Jev routing (all projects)` rules for `~/.claude/CLAUDE.md`
  (Install, Step 8).
- `jev-pack/plugins/fast-jev-compaction`: a copy of
  [tamaratran/fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction) (MIT).
  It replaces the compaction summary: Jev scores old tool calls and results, stale ones are
  dropped or truncated, and user and assistant text stays word for word.

## Licence

MIT, see `LICENSE`. The two third-party copies keep their own MIT licences:
`companions/jev-skill-suggestion/LICENSE` and `jev-pack/plugins/fast-jev-compaction/LICENSE`.
