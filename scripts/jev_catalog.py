#!/usr/bin/env python3
"""Catalog Claude Code skills, commands and agents, and track their review state."""

import datetime
import glob
import hashlib
import json
import os
import re
import sys

HOME = os.environ.get("JEV_HOME", os.path.expanduser("~"))
CATALOG = os.environ.get("JEV_CATALOG", os.path.expanduser("~/.config/typesafe/jev-catalog.json"))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTING_FILE = os.environ.get("JEV_ROUTING_FILE", os.path.join(REPO_ROOT, "skill-routing.json"))
VERSION = 1
FRONTMATTER_CHARS = 6000


def _frontmatter(path: str) -> dict[str, str]:
    """Read name and description from the first YAML-style frontmatter block."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read(FRONTMATTER_CHARS).splitlines()
    except (OSError, ValueError):
        return {}
    starts = [index for index, line in enumerate(lines) if line.strip() == "---"]
    if len(starts) < 2:
        return {}
    fields: dict[str, str] = {}
    block = lines[starts[0] + 1:starts[1]]
    index = 0
    while index < len(block):
        match = re.match(r"^([A-Za-z_-]+)\s*:\s*(.*)$", block[index])
        if not match:
            index += 1
            continue
        key, value = match.group(1), match.group(2).strip()
        if key not in ("name", "description"):
            index += 1
            continue
        if value in ("|", "|-", ">", ">-"):
            parts = []
            index += 1
            while index < len(block):
                line = block[index]
                if line.strip() and not line[:1].isspace():
                    break
                if line.strip():
                    parts.append(line.strip())
                index += 1
            fields[key] = " ".join(" ".join(parts).split())
            continue
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        fields[key] = " ".join(value.split())
        index += 1
    return fields


def _add(items: list[dict], seen: set[str], name: str, kind: str, plugin: str | None,
         source: str, path: str, description: str) -> None:
    """Add a discovered file unless an earlier source already supplied its name."""
    if name and name not in seen:
        seen.add(name)
        items.append({"name": name, "kind": kind, "plugin": plugin, "source": source,
                      "path": path, "description": description})


def _scan_root(items: list[dict], seen: set[str], root: str, source: str,
               plugin: str | None = None) -> None:
    """Add skills, commands and agents from one .claude directory."""
    try:
        for path in sorted(glob.glob(os.path.join(root, "skills", "*", "SKILL.md"))):
            directory = os.path.basename(os.path.dirname(path))
            fields = _frontmatter(path)
            _add(items, seen, f"{plugin}:{directory}" if plugin else directory, "skill",
                 plugin, source, path, fields.get("description", ""))
        for path in sorted(glob.glob(os.path.join(root, "commands", "**", "*.md"), recursive=True)):
            relative = os.path.relpath(path, os.path.join(root, "commands"))[:-3]
            name = relative.replace(os.sep, ":")
            fields = _frontmatter(path)
            _add(items, seen, f"{plugin}:{name}" if plugin else name, "command", plugin,
                 source, path, fields.get("description", ""))
        for path in sorted(glob.glob(os.path.join(root, "agents", "*.md"))):
            fields = _frontmatter(path)
            item_name = fields.get("name") or os.path.splitext(os.path.basename(path))[0]
            _add(items, seen, f"{plugin}:{item_name}" if plugin else item_name, "agent",
                 plugin, source, path, fields.get("description", ""))
    except (OSError, ValueError):
        return


def _inside(path: str, parent: str) -> bool:
    """Whether path is parent or a descendant of parent."""
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(parent))) == os.path.abspath(parent)
    except (OSError, ValueError):
        return False


def _plugin_entries(home: str) -> tuple[dict, dict]:
    """Load installed plugin records and enabled-plugin settings, or empty mappings."""
    base = os.path.join(home, ".claude", "plugins")
    try:
        with open(os.path.join(base, "installed_plugins.json"), encoding="utf-8") as handle:
            plugins = json.load(handle).get("plugins") or {}
        if not isinstance(plugins, dict):
            plugins = {}
    except (OSError, ValueError, AttributeError):
        plugins = {}
    try:
        with open(os.path.join(home, ".claude", "settings.json"), encoding="utf-8") as handle:
            enabled = json.load(handle).get("enabledPlugins") or {}
        if not isinstance(enabled, dict):
            enabled = {}
    except (OSError, ValueError, AttributeError):
        enabled = {}
    return plugins, enabled


def discover(cwd: str | None = None) -> list[dict]:
    """Discover available skills, commands and agents in precedence order."""
    items: list[dict] = []
    seen: set[str] = set()
    home = os.path.abspath(HOME)
    _scan_root(items, seen, os.path.join(home, ".claude"), "personal")

    if cwd:
        try:
            current = os.path.abspath(cwd)
            while current != os.path.dirname(current) and current != home:
                _scan_root(items, seen, os.path.join(current, ".claude"), "project")
                current = os.path.dirname(current)
        except (OSError, ValueError):
            pass

    plugins, enabled = _plugin_entries(home)
    plugin_root = os.path.join(home, ".claude", "plugins")
    for key, entries in plugins.items():
        if enabled.get(key) is False:
            continue
        plugin = key.split("@", 1)[0]
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                continue
            project = entry.get("projectPath")
            if entry.get("scope") in ("project", "local") and (
                    not cwd or not project or not _inside(cwd, str(project))):
                continue
            install = entry.get("installPath")
            if install:
                _scan_root(items, seen, str(install), "plugin", plugin)

    try:
        synced = sorted(glob.glob(os.path.join(plugin_root, "synced", "*", "*")))
        for base in synced:
            if os.path.isdir(base):
                _scan_root(items, seen, base, "synced", os.path.basename(base))
    except (OSError, ValueError):
        pass
    return items


def _load_state() -> dict:
    """Load the catalog state; missing or malformed files start empty."""
    try:
        with open(CATALOG, encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
            return {"version": VERSION, "items": {}}
        return {"version": VERSION, "items": state["items"]}
    except (OSError, ValueError):
        return {"version": VERSION, "items": {}}


def _save_state(state: dict) -> None:
    """Write catalog state through a sibling temporary file."""
    parent = os.path.dirname(CATALOG) or "."
    os.makedirs(parent, exist_ok=True)
    with open(CATALOG + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(CATALOG + ".tmp", CATALOG)


def _now() -> str:
    """Return an ISO UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def scan(cwd: str | None = None, baseline: bool = False) -> dict:
    """Refresh catalog state from discovery and report additions and removals."""
    state = _load_state()
    records = state["items"]
    found = discover(cwd)
    names = {item["name"] for item in found}
    new: list[str] = []
    changed: list[str] = []
    # Project- and local-scoped items vanish from discovery whenever the session runs in
    # another folder; only a file that is really gone counts as removed.
    removed = sorted(name for name in set(records) - names
                     if not os.path.exists(str(records[name].get("path") or "")))
    for name in removed:
        del records[name]
    for item in found:
        name = item["name"]
        fingerprint = hashlib.sha1((item["kind"] + "\n" + item["description"]).encode()).hexdigest()[:12]
        old = records.get(name)
        if not isinstance(old, dict):
            new.append(name)
            records[name] = {"kind": item["kind"], "plugin": item["plugin"], "source": item["source"],
                             "path": item["path"], "fingerprint": fingerprint,
                             "status": "baseline" if baseline else "pending", "first_seen": _now(),
                             "reviewed_at": None, "note": "new"}
        elif old.get("fingerprint") != fingerprint:
            changed.append(name)
            status = old.get("status")
            note = old.get("note", "")
            if status != "pending":
                status, note = "pending", "description changed"
            old.update({"kind": item["kind"], "plugin": item["plugin"], "source": item["source"],
                        "path": item["path"], "fingerprint": fingerprint, "status": status, "note": note})
        else:
            old.update({"kind": item["kind"], "plugin": item["plugin"], "source": item["source"],
                        "path": item["path"]})
    records = {name: records[name] for name in sorted(records)}
    state["items"] = records
    _save_state(state)
    return {"total": len(found), "new": sorted(new), "changed": sorted(changed),
            "removed": removed,
            "pending": sorted(name for name, record in records.items()
                               if record.get("status") == "pending")}


def _routing() -> dict:
    """Load the routing JSON, returning an empty object if it is unavailable."""
    try:
        with open(ROUTING_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def pending_items(cwd: str | None = None) -> list[dict]:
    """Return pending discovered items with review note and current routing line."""
    scan(cwd)
    state = _load_state()["items"]
    routing = _routing().get("skills") or {}
    return [{**item, "note": state[item["name"]].get("note", ""),
             "routing": routing.get(item["name"], "") if isinstance(routing, dict) else ""}
            for item in discover(cwd) if state.get(item["name"], {}).get("status") == "pending"]


def _usage() -> None:
    """Print the supported command forms."""
    print("Usage:\n"
          "  jev_catalog.py scan [--baseline] [--cwd DIR]\n"
          "  jev_catalog.py status [--cwd DIR]\n"
          "  jev_catalog.py pending [--json] [--cwd DIR]\n"
          "  jev_catalog.py review <name> --line <text>\n"
          "  jev_catalog.py review <name> --keep\n"
          "  jev_catalog.py review --plugin <plugin> --keep")


def _cwd_arg(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract an optional --cwd directory from arguments."""
    rest: list[str] = []
    cwd = None
    index = 0
    while index < len(args):
        if args[index] == "--cwd" and index + 1 < len(args):
            cwd = args[index + 1]
            index += 2
        else:
            rest.append(args[index])
            index += 1
    return cwd, rest


def main(argv: list[str] | None = None) -> int:
    """Run the catalog command line interface."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _usage()
        return 2
    command, args = args[0], args[1:]
    if command in ("scan", "status", "pending"):
        cwd, args = _cwd_arg(args)
        if command == "scan":
            baseline = "--baseline" in args
            if any(arg != "--baseline" for arg in args):
                _usage()
                return 2
            result = scan(cwd, baseline)
            print(f"{result['total']} items; {len(result['new'])} new, {len(result['changed'])} changed, "
                  f"{len(result['removed'])} removed, {len(result['pending'])} pending")
            return 0
        if command == "status":
            if args:
                _usage()
                return 2
            scan(cwd)
            state = _load_state()["items"]
            print("status: " + ", ".join(f"{key} {sum(r.get('status') == key for r in state.values())}"
                                          for key in ("pending", "reviewed", "baseline")))
            print("kind: " + ", ".join(f"{key} {sum(r.get('kind') == key for r in state.values())}"
                                        for key in ("skill", "command", "agent")))
            groups: dict[str, list[str]] = {}
            for name, record in state.items():
                if record.get("status") == "pending":
                    groups.setdefault(record.get("plugin") or "(personal)", []).append(name)
            for plugin, names in sorted(groups.items()):
                print(f"{plugin}: {', '.join(sorted(names))}")
            return 0
        as_json = "--json" in args
        if any(arg != "--json" for arg in args):
            _usage()
            return 2
        pending = pending_items(cwd)
        if as_json:
            print(json.dumps(pending, ensure_ascii=False))
        else:
            for item in pending:
                print(f"## {item['name']} ({item['kind']}, {item['source']}, {item['note']})")
                print(f"path: {item['path']}")
                print(f"description: {item['description']}")
                print(f"routing: {item['routing'] or '(none)'}")
        return 0
    if command == "review":
        if len(args) == 3 and args[0] == "--plugin" and args[2] == "--keep":
            state = _load_state()
            count = 0
            for record in state["items"].values():
                if record.get("plugin") == args[1] and record.get("status") == "pending":
                    record.update({"status": "reviewed", "reviewed_at": _now(), "note": ""})
                    count += 1
            _save_state(state)
            print(f"reviewed {count} items for {args[1]}")
            return 0
        if not ((len(args) == 3 and args[1] == "--line") or
                (len(args) == 2 and args[1] == "--keep")):
            _usage()
            return 2
        name, option = args[:2]
        text = args[2] if option == "--line" else ""
        state = _load_state()
        record = state["items"].get(name)
        if not isinstance(record, dict):
            print(f"error: {name} is not in the catalog", file=sys.stderr)
            return 1
        if option == "--line":
            if not 1 <= len(text) <= 500:
                print("error: routing text must be 1-500 characters", file=sys.stderr)
                return 1
            routing = _routing()
            skills = routing.get("skills")
            if not isinstance(skills, dict):
                skills = {}
            routing["skills"] = skills
            skills[name] = text
            with open(ROUTING_FILE, "w", encoding="utf-8") as handle:
                json.dump(routing, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        record.update({"status": "reviewed", "reviewed_at": _now(), "note": ""})
        _save_state(state)
        print(f"reviewed {name}")
        return 0
    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
