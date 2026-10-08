#!/usr/bin/env python3
"""
jev_setup.py -- put this Mac's whole Jev setup on another machine, and keep the repo current.

    ./setup.sh install [--dry-run] [--no-lanes]   set up a new Mac from this repo
    ./setup.sh check                              what is installed, what is missing
    ./setup.sh sync                               copy this Mac's live pieces into the repo
    ./setup.sh publish [--dry-run]                build the public repo and push it

Everything it installs is listed in setup/manifest.json. Keys are never read from or
written to the repo: `install` asks for each one (input hidden) and saves it only in
~/.claude/settings.json. Pieces marked "private" have no licence that allows sharing;
they are skipped when missing (the public repo), and `publish` leaves them out.

Standard library only. Every step is safe to re-run: what is already there is left alone.
"""

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(REPO, "setup", "manifest.json")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
NOREPLY = "9551898+rayopavri@users.noreply.github.com"


def private_emails() -> set[str]:
    """The owner's real addresses, read at run time (git settings and this repo's history)
    so they are never written in this file. The noreply address is fine to publish."""
    found = set()
    for cmd in (["git", "config", "--global", "user.email"], ["git", "log", "--all", "--format=%ae%n%ce"]):
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
        found.update(line.strip() for line in p.stdout.splitlines() if "@" in line)
    return {e for e in found if not e.endswith("users.noreply.github.com") and not e.endswith("anthropic.com")}
# Shapes of real keys. `publish` refuses to push if any tracked file matches.
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{20,}|sk-or-[A-Za-z0-9-]{20,}|apikey_[0-9a-f]{20,}|ghp_[A-Za-z0-9]{20,}"
                    r"|AKIA[0-9A-Z]{16}|xox[bp]-[0-9A-Za-z-]{10,}|vck_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY")

DRY = False
results = []  # (status, what) for the closing summary


def home(path: str) -> str:
    return os.path.expanduser(path)


def note(status: str, what: str) -> None:
    results.append((status, what))
    mark = {"ok": "✓", "done": "+", "skip": "-", "fail": "✗", "todo": "→"}.get(status, " ")
    print(f"  {mark} {what}", flush=True)


def run(cmd: list[str], cwd: str | None = None, quiet: bool = True) -> tuple[int, str]:
    """Run a command; in --dry-run only print it. Returns (exit code, output)."""
    shown = " ".join(cmd)
    if DRY:
        print(f"    would run: {shown}")
        return 0, ""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=quiet, text=True)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def probe(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    """A read-only command: runs even in --dry-run."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def load() -> dict:
    with open(MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)


def read_settings() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def write_settings(data: dict) -> None:
    if DRY:
        print("    would update ~/.claude/settings.json")
        return
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, SETTINGS + ".bak-jev-setup")
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, SETTINGS)
    os.chmod(SETTINGS, 0o600)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def node_major() -> int:
    code, out = probe(["node", "-v"])
    m = re.match(r"v(\d+)", out) if code == 0 else None
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- install


def prerequisites() -> dict:
    print("\nPrerequisites")
    ok = {}
    ok["python"] = sys.version_info >= (3, 10)
    note("ok" if ok["python"] else "fail", f"Python {sys.version.split()[0]} (3.10+ needed)")
    for tool, why in (("git", "git"), ("claude", "Claude Code's `claude` command")):
        ok[tool] = have(tool)
        note("ok" if ok[tool] else "fail", why + ("" if ok[tool] else " -- missing"))
    major = node_major()
    ok["node"] = major >= 20
    note("ok" if ok["node"] else "fail",
         f"Node {major or 'missing'} (20+ needed; install with nvm: https://github.com/nvm-sh/nvm, then `nvm install --lts`)")
    if REPO != home("~/.claude/skills/jev-interceptor"):
        note("fail", f"this repo must live at ~/.claude/skills/jev-interceptor (it is at {REPO})")
        ok["place"] = False
    return ok


def install_copies(m: dict) -> None:
    print("\nSkills, commands and hooks (copied from this repo)")
    for item in m["copies"]:
        src = os.path.join(REPO, item["repo"])
        dest = home(item["live"])
        if not os.path.exists(src):
            note("skip", f"{item['name']}: not in this repo" + (" (private piece)" if item.get("private") else ""))
            continue
        if os.path.exists(dest):
            note("ok", f"{item['name']}: already at {item['live']}")
        else:
            if not DRY:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.isdir(src):
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
                if item.get("mode"):
                    os.chmod(dest, int(item["mode"], 8))
            note("done", f"{item['name']}: copied to {item['live']}")
        link = item.get("link")
        if link and not os.path.lexists(home(link["from"])):
            if not DRY:
                os.symlink(home(link["to"]), home(link["from"]))
            note("done", f"{item['name']}: linked {link['from']}")


def install_settings(m: dict) -> None:
    print("\nClaude Code settings (~/.claude/settings.json)")
    data = read_settings()
    changed = False
    env = data.setdefault("env", {})
    for key, value in m["settings_env"].items():
        if env.get(key) != value:
            env[key] = value
            changed = True
            note("done", f"env {key}={value}")
        else:
            note("ok", f"env {key}")
    for secret in m["secret_env"]:
        key = secret["key"]
        if env.get(key):
            note("ok", f"{key} is set")
            continue
        # Claude Code reads keys from settings, so a key that is only in this shell is
        # copied in; otherwise ask for it (hidden).
        value = os.environ.get(key, "").strip() or ("" if DRY or not sys.stdin.isatty() else getpass.getpass(
            f"    Paste {key} ({secret['about']}), or press Return to skip: ").strip())
        if value:
            env[key] = value
            changed = True
            note("done", f"{key} saved to settings (not shown)")
        else:
            note("todo", f"{key} not set: add it to the env block in ~/.claude/settings.json ({secret['about']})")
    hooks = data.setdefault("hooks", {})
    for hook in m["hooks"]:
        if not os.path.exists(os.path.join(REPO, hook["needs"])):
            note("skip", f"{hook['about']} hook: its files are not in this repo")
            continue
        groups = hooks.setdefault(hook["event"], [])
        if any(h.get("command") == hook["command"] for g in groups for h in g.get("hooks", [])):
            note("ok", f"{hook['about']} hook ({hook['event']})")
            continue
        groups.append({"matcher": hook["matcher"],
                       "hooks": [{"type": "command", "command": hook["command"], "timeout": hook["timeout"]}]})
        changed = True
        note("done", f"{hook['about']} hook added ({hook['event']})")
    configs = data.setdefault("pluginConfigs", {})
    for plugin in m["plugins"]:
        wanted = plugin.get("options")
        if not wanted:
            continue
        options = configs.setdefault(plugin["id"], {}).setdefault("options", {})
        missing = {k: v for k, v in wanted.items() if k not in options}
        if missing:
            options.update(missing)
            changed = True
            note("done", f"{plugin['id']} options: {', '.join(missing)}")
    if changed:
        write_settings(data)


def install_plugins(m: dict) -> None:
    print("\nPlugins")
    code, out = probe(["claude", "plugin", "list", "--json"])
    installed = out if code == 0 else ""
    for plugin in m["plugins"]:
        if f'"{plugin["id"]}"' in installed or plugin["id"] in installed:
            note("ok", f"{plugin['id']}")
            continue
        source = plugin["marketplace"]
        if source.startswith("~"):
            source = home(source)
            if not os.path.isdir(source):
                note("skip", f"{plugin['id']}: {plugin['marketplace']} is missing")
                continue
        code, out = run(["claude", "plugin", "marketplace", "add", source])
        if code != 0 and "already" not in out.lower():
            note("fail", f"{plugin['id']}: adding its marketplace failed: {out[-200:]}")
            continue
        code, out = run(["claude", "plugin", "install", plugin["id"]])
        note("done" if code == 0 else "fail", f"{plugin['id']}" + ("" if code == 0 else f": {out[-200:]}"))


def install_mcp(m: dict, node_ok: bool) -> None:
    print("\nMCP servers")
    for server in m["mcp_servers"]:
        clone = server.get("clone")
        if not node_ok:
            note("skip", f"{server['name']}: needs Node 20+")
            continue
        if clone:
            dest = home(clone["dest"])
            if os.path.isdir(os.path.join(dest, ".git")):
                note("ok", f"{server['name']}: code at {clone['dest']}")
            else:
                code, out = run(["git", "clone", "-q", clone["url"], dest])
                if code != 0:
                    note("fail", f"{server['name']}: clone failed: {out[-200:]}")
                    continue
                for step in clone["after"]:
                    code, out = run(step, cwd=dest)
                    if code != 0:
                        note("fail", f"{server['name']}: `{' '.join(step)}` failed: {out[-200:]}")
                        break
                else:
                    note("done", f"{server['name']}: installed at {clone['dest']}")
        code, _ = probe(["claude", "mcp", "get", server["name"]])
        if code == 0:
            note("ok", f"{server['name']}: registered")
            continue
        command = [home(part) if part.startswith("~") else part for part in server["command"]]
        code, out = run(["claude", "mcp", "add", "-s", "user", server["name"], "--"] + command)
        note("done" if code == 0 else "fail", f"{server['name']}: registered" + ("" if code == 0 else f": {out[-200:]}"))


def install_lanes(m: dict, node_ok: bool) -> None:
    print("\nClinePass lane tools")
    if not node_ok:
        note("skip", "pi: needs Node 20+")
        return
    for pkg in m["npm_global"]:
        code, out = probe(["npm", "ls", "-g", "--depth=0", pkg["package"]])
        if code == 0 and pkg["package"] in out:
            note("ok", pkg["package"])
            continue
        code, out = run(["npm", "install", "-g", pkg["package"]])
        note("done" if code == 0 else "fail", pkg["package"] + ("" if code == 0 else f": {out[-200:]}"))
    if not have("pi") and not DRY:
        note("skip", "pi add-ons: `pi` is not on PATH yet (open a new terminal, then re-run)")
        return
    for pkg in m["pi_packages"]:
        code, out = run(["pi", "install", pkg["package"]])
        note("done" if code == 0 else "fail", pkg["package"] + ("" if code == 0 else f": {out[-200:]}"))


def install_claude_md(m: dict) -> None:
    print("\nRouting rules (~/.claude/CLAUDE.md)")
    spec = m["claude_md"]
    text = open(CLAUDE_MD, encoding="utf-8").read() if os.path.exists(CLAUDE_MD) else ""
    if spec["marker"] in text:
        note("ok", "Jev routing section present")
        return
    snippet = open(os.path.join(REPO, spec["source"]), encoding="utf-8").read()
    if not DRY:
        os.makedirs(os.path.dirname(CLAUDE_MD), exist_ok=True)
        with open(CLAUDE_MD, "a", encoding="utf-8") as handle:
            handle.write(("\n\n" if text else "") + snippet)
    note("done", "Jev routing section added")


def install(args: list[str]) -> int:
    m = load()
    ok = prerequisites()
    if not (ok.get("python") and ok.get("git") and ok.get("claude")) or ok.get("place") is False:
        print("\nFix the items marked ✗ above, then run this again.")
        return 1
    install_copies(m)
    install_settings(m)
    install_plugins(m)
    install_mcp(m, ok["node"])
    if "--no-lanes" not in args:
        install_lanes(m, ok["node"])
    install_claude_md(m)
    print("\nStill to do by hand")
    for step in m["manual_steps"]:
        note("todo", step)
    failed = [w for s, w in results if s == "fail"]
    print(f"\n{len(failed)} problem(s)." if failed else "\nDone. Re-run any time; finished steps are skipped.")
    return 1 if failed else 0


# ---------------------------------------------------------------- check / sync


def check(_: list[str]) -> int:
    m = load()
    print("Copies")
    for item in m["copies"]:
        live = os.path.exists(home(item["live"]))
        in_repo = os.path.exists(os.path.join(REPO, item["repo"]))
        note("ok" if live else "fail", f"{item['name']}: {'installed' if live else 'missing'}"
             f"{'' if in_repo else ' (not in this repo)'}")
    print("Plugins")
    _, out = probe(["claude", "plugin", "list", "--json"])
    for plugin in m["plugins"]:
        note("ok" if plugin["id"] in out else "fail", plugin["id"])
    print("MCP servers")
    for server in m["mcp_servers"]:
        code, _ = probe(["claude", "mcp", "get", server["name"]])
        note("ok" if code == 0 else "fail", server["name"])
    env = read_settings().get("env", {})
    print("Keys")
    for secret in m["secret_env"]:
        note("ok" if env.get(secret["key"]) or os.environ.get(secret["key"]) else "fail", secret["key"])
    return 0


def sync(_: list[str]) -> int:
    """Copy each live piece on this Mac into the repo, so the backup matches."""
    m = load()
    print("Copying this Mac's pieces into the repo")
    for item in m["copies"]:
        live, dest = home(item["live"]), os.path.join(REPO, item["repo"])
        if not os.path.exists(live):
            note("skip", f"{item['name']}: not on this Mac")
            continue
        if DRY:
            note("done", f"{item['name']}: would copy")
            continue
        if os.path.isdir(live):
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(os.path.realpath(live), dest,
                            ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc", "*.bak*", ".git"))
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.realpath(live), dest)
        note("done", f"{item['name']}")
    code, out = probe(["git", "status", "--short"], cwd=REPO)
    print("\nChanged in the repo:\n" + (out or "  nothing") + "\nReview, then commit and push as usual.")
    return 0


# ---------------------------------------------------------------- publish


def publish(args: list[str]) -> int:
    """Build the public repo from this one: tracked files minus the private pieces, with
    the private-only README sections cut, then push it as an ordinary new commit."""
    m = load()
    public = m["public_repo"]
    code, out = probe(["git", "status", "--porcelain"], cwd=REPO)
    if out.strip() and not DRY:
        print("Commit or discard your changes first, so the public copy matches a saved version.")
        return 1
    work = tempfile.mkdtemp(prefix="jev-public-")
    try:
        code, out = probe(["gh", "repo", "view", public, "--json", "name"])
        exists = code == 0
        target = os.path.join(work, "public")
        if exists:
            code, out = probe(["git", "clone", "-q", f"https://github.com/{public}.git", target])
            if code != 0:
                print(f"Could not clone {public}: {out}")
                return 1
        else:
            os.makedirs(target)
            probe(["git", "init", "-q", "-b", "main"], cwd=target)
        # Replace everything but .git with the private repo's tracked files.
        for name in os.listdir(target):
            if name != ".git":
                path = os.path.join(target, name)
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        _, files = probe(["git", "ls-files"], cwd=REPO)
        private = m["private_paths"]  # whole files or folders: "companions/jev" must not catch "companions/jev-browser"
        kept = [f for f in files.splitlines()
                if f and not any(f == p or f.startswith(p.rstrip("/") + "/") for p in private)]
        for rel in kept:
            os.makedirs(os.path.dirname(os.path.join(target, rel)) or target, exist_ok=True)
            shutil.copy2(os.path.join(REPO, rel), os.path.join(target, rel))
        readme = os.path.join(target, "README.md")
        text = open(readme, encoding="utf-8").read()
        text = re.sub(r"<!-- private:start -->.*?<!-- private:end -->\n?", "", text, flags=re.S)
        text = re.sub(r"<!-- public:\n(.*?)\n-->", r"\1", text, flags=re.S)  # public-only text shows
        text = text.replace(f"github.com/{m['private_repo']}", f"github.com/{public}")
        open(readme, "w", encoding="utf-8").write(text)
        # Never publish a key.
        leaks, emails = [], private_emails()
        for rel in kept:
            try:
                body = open(os.path.join(target, rel), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if SECRET.search(body) or any(e in body for e in emails):
                leaks.append(rel)
        if leaks:
            print("Stopped: these files look like they hold a key or your email:\n  " + "\n  ".join(leaks))
            return 1
        print(f"Public copy: {len(kept)} files, {len(files.splitlines()) - len(kept)} private files left out, no keys found.")
        if DRY:
            print(f"Dry run: built in {target}; nothing pushed.")
            return 0
        run(["git", "add", "-A"], cwd=target)
        code, out = run(["git", "-c", "user.name=Rayomand Pavri", "-c", f"user.email={NOREPLY}",
                         "commit", "-q", "-m", "Update from the private setup repo"], cwd=target)
        if code != 0 and "nothing to commit" in out:
            print("The public repo is already up to date.")
            return 0
        if not exists:
            code, out = run(["gh", "repo", "create", public, "--public", "--source", target, "--push",
                             "--description", "A Mac's whole Jev setup for Claude Code: model routing, lanes, and the community Jev plugins, installed with one command."])
        else:
            code, out = run(["git", "push", "-q", "origin", "main"], cwd=target)
        print(("Published: https://github.com/" + public) if code == 0 else f"Push failed: {out}")
        return 0 if code == 0 else 1
    finally:
        if not DRY:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    global DRY
    DRY = "--dry-run" in argv
    commands = {"install": install, "check": check, "sync": sync, "publish": publish}
    if not argv or argv[0] not in commands:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
