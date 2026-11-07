#!/usr/bin/env python3
"""Populate the SSH Tree config from PuTTY sessions in the Windows registry.

PuTTY keeps every session under HKCU\\Software\\SimonTatham\\PuTTY\\Sessions;
PuTTY Session Manager adds a "PsmPath" value with the folder (for example
"Sessions\\Work\\Servers").  This script turns those into the JSON tree:

    python import_putty_registry.py                # write ~/.config/sshtree/sessions.json
    python import_putty_registry.py --dry-run      # print the result, write nothing
    python import_putty_registry.py --output F     # write somewhere else
    python import_putty_registry.py --merge        # keep existing sessions, add missing

Without --merge the existing tree is replaced (a .bak copy is kept) while the
`defaults` block is preserved apart from what the import derives.  Passwords
are never read: PuTTY does not store them, and this tool would not either.
Key files: PuTTY's .ppk keys are mapped to the OpenSSH key of the same base
name in the same folder when one exists; otherwise the .ppk is kept for WinSCP
and a note is added to the session.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.parse
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sshtree  # noqa: E402

SKIP_SESSIONS = {"Default Settings", "WinSCP temporary session"}


def read_registry_sessions() -> dict[str, dict]:
    """{session name: {value name: value}} for every PuTTY session."""
    import winreg

    base = r"Software\SimonTatham\PuTTY\Sessions"
    out: dict[str, dict] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
        count = winreg.QueryInfoKey(root)[0]
        for i in range(count):
            raw = winreg.EnumKey(root, i)
            values: dict = {}
            with winreg.OpenKey(root, raw) as key:
                j = 0
                while True:
                    try:
                        name, value, _type = winreg.EnumValue(key, j)
                    except OSError:
                        break
                    values[name] = value
                    j += 1
            out[urllib.parse.unquote(raw)] = values
    return out


def parse_port_forwardings(spec: str) -> list[str]:
    """'L3306=db:3306,D9001=,4R2222=localhost:22' -> SSH Tree tunnel strings."""
    tunnels = []
    for item in str(spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        left, _, target = item.partition("=")
        left = left.lstrip("46")  # PuTTY's IPv4/IPv6 prefix
        if not left:
            continue
        kind, listen = left[0].upper(), left[1:]
        if kind == "D":
            tunnels.append(f"D {listen}")
        elif kind in ("L", "R") and target:
            tunnels.append(f"{kind} {listen} {target}")
    return tunnels


def map_key_file(ppk: str) -> tuple[str | None, str | None]:
    """(identity_file, winscp_key) for a PuTTY PublicKeyFile value."""
    ppk = str(ppk or "").strip()
    if not ppk:
        return None, None
    path = Path(os.path.expandvars(ppk))
    home = str(Path.home())
    candidates = [path.with_suffix(""), path.with_name(path.name + ".openssh")]
    for cand in candidates:
        if cand.exists() and cand.suffix != ".ppk":
            s = str(cand)
            if s.startswith(home):
                s = "~" + s[len(home) :]
            return s.replace("\\", "/"), None
    return None, ppk.replace("\\", "/")


def convert_session(name: str, v: dict) -> dict:
    """One registry session -> one SSH Tree session node (no defaults applied)."""
    host = str(v.get("HostName") or "").strip()
    user = str(v.get("UserName") or "").strip()
    if "@" in host and not user:
        user, _, host = host.rpartition("@")
    node: dict = {"name": name, "host": host}
    if user:
        node["user"] = user
    port = int(v.get("PortNumber") or 22)
    if port != 22:
        node["port"] = port
    identity, winscp_key = map_key_file(v.get("PublicKeyFile"))
    if identity:
        node["identity_file"] = identity
    if winscp_key:
        node["winscp_key"] = winscp_key
        node["_note"] = (
            "no OpenSSH key next to the .ppk; convert it with puttygen or set identity_file"
        )
    tunnels = parse_port_forwardings(v.get("PortForwardings"))
    if tunnels:
        node["tunnels"] = tunnels
    if v.get("RemoteCommand"):
        node["command"] = str(v["RemoteCommand"])
    node["agent_forwarding"] = bool(v.get("AgentFwd", 0))
    if v.get("X11Forward"):
        node["x11"] = True
    if v.get("SshNoShell"):
        node["no_shell"] = True
    options: dict = {}
    if v.get("Compression"):
        options["Compression"] = "yes"
    if v.get("TCPKeepalives"):
        options["TCPKeepAlive"] = "yes"
    if v.get("PingIntervalSecs"):
        options["ServerAliveInterval"] = int(v["PingIntervalSecs"])
    proxy = int(v.get("ProxyMethod") or 0)
    if proxy in (1, 2, 3, 4) and v.get("ProxyHost"):  # SOCKS4/5, HTTP, telnet
        kind = {1: "socks4", 2: "socks5", 3: "http", 4: "telnet"}[proxy]
        node["_note"] = (
            f"PuTTY used a {kind} proxy via {v['ProxyHost']}:{v.get('ProxyPort')}; set options.ProxyCommand"
        )
    elif proxy == 5 and v.get("ProxyTelnetCommand"):  # local command, often plink -nc
        node["_note"] = (
            f"PuTTY proxy command: {v['ProxyTelnetCommand']!r}; use jump_host or options.ProxyCommand"
        )
    if options:
        node["options"] = options
    if not host:
        node["_note"] = "no host name in PuTTY"
    return node


def folder_parts(psm_path) -> tuple[str, ...]:
    parts = [p for p in str(psm_path or "").replace("/", "\\").split("\\") if p]
    if parts and parts[0].lower() == "sessions":
        parts = parts[1:]
    return tuple(parts)


def build_tree(sessions: dict[str, dict]) -> tuple[list, dict, list[str]]:
    """Return (tree, derived defaults, notes)."""
    nodes: list[tuple[tuple[str, ...], dict]] = []
    for name, values in sessions.items():
        if name in SKIP_SESSIONS:
            continue
        nodes.append(
            (folder_parts(values.get("PsmPath")), convert_session(name, values))
        )

    # derive global defaults from the majority so the sessions stay short
    defaults: dict = {}
    agent = Counter(n["agent_forwarding"] for _p, n in nodes)
    if agent:
        defaults["agent_forwarding"] = agent.most_common(1)[0][0]
    users = Counter(n.get("user") for _p, n in nodes if n.get("user"))
    if users and users.most_common(1)[0][1] >= max(3, len(nodes) // 3):
        defaults["user"] = users.most_common(1)[0][0]
    keys = Counter(n.get("identity_file") for _p, n in nodes if n.get("identity_file"))
    if keys and keys.most_common(1)[0][1] >= max(3, len(nodes) // 3):
        defaults["identity_file"] = keys.most_common(1)[0][0]
    for _p, n in nodes:
        for key in ("agent_forwarding", "user", "identity_file"):
            if key in defaults and n.get(key) == defaults[key]:
                del n[key]
        if (
            "agent_forwarding" in n
            and "agent_forwarding" not in defaults
            and not n["agent_forwarding"]
        ):
            del n["agent_forwarding"]

    tree: list = []
    folders: dict[tuple[str, ...], list] = {(): tree}

    def children_of(path: tuple[str, ...]) -> list:
        if path not in folders:
            parent = children_of(path[:-1])
            folder = {"name": path[-1], "children": []}
            parent.append(folder)
            folders[path] = folder["children"]
        return folders[path]

    notes: list[str] = []
    for path in sorted({p for p, _n in nodes}):
        children_of(path)  # folders first, then sessions, at every level
    for path, node in sorted(nodes, key=lambda pn: (pn[0], pn[1]["name"].lower())):
        children_of(path).append(node)
        if node.get("_note"):
            notes.append(f"{'/'.join(path + (node['name'],))}: {node['_note']}")
    return tree, defaults, notes


def merge_trees(existing: list, imported: list) -> int:
    """Add imported sessions/folders missing from `existing`; return count added."""
    added = 0
    for node in imported:
        match = next(
            (
                n
                for n in existing
                if n.get("name") == node.get("name")
                and sshtree.is_folder(n) == sshtree.is_folder(node)
            ),
            None,
        )
        if match is None:
            existing.append(node)
            added += (
                sum(1 for _s, _f in sshtree.walk_sessions([node], []))
                if sshtree.is_folder(node)
                else 1
            )
        elif sshtree.is_folder(node):
            added += merge_trees(match["children"], node["children"])
    return added


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", "-o", help="config file to write (default: the SSH Tree config)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the resulting JSON, write nothing"
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="keep existing sessions and only add missing ones",
    )
    args = parser.parse_args(
        sshtree.fix_windows_argv(sys.argv[1:] if argv is None else argv)
    )

    if not sshtree.IS_WINDOWS:
        print(
            "error: PuTTY sessions live in the Windows registry; run this on Windows",
            file=sys.stderr,
        )
        return 2
    sessions = read_registry_sessions()
    tree, derived, notes = build_tree(sessions)
    count = sum(1 for _ in sshtree.walk_sessions(tree, []))

    out_path = (
        Path(args.output).expanduser() if args.output else sshtree.default_config_path()
    )
    data: dict = {"defaults": dict(sshtree.EXAMPLE_CONFIG["defaults"]), "tree": []}
    if out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("defaults", {})
        data.setdefault("tree", [])
    if data["defaults"] == sshtree.EXAMPLE_CONFIG["defaults"]:
        # untouched example: its placeholder login must not apply to real hosts
        data["defaults"].pop("user", None)
    data["defaults"].update(derived)
    if args.merge:
        added = merge_trees(data["tree"], tree)
        summary = f"{added} new of {count} PuTTY sessions added to {out_path}"
    else:
        data["tree"] = tree
        summary = f"{count} PuTTY sessions written to {out_path}"

    if args.dry_run:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        if out_path.exists():
            backup = out_path.with_name(
                f"{out_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            )
            shutil.copy2(out_path, backup)
            print(f"backup: {backup}")
        sshtree.atomic_write_json(out_path, data)
    print(summary, file=sys.stderr)
    if derived:
        print("derived defaults:", json.dumps(derived), file=sys.stderr)
    for note in notes:
        print("note:", note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
