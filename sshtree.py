#!/usr/bin/env python3
"""SshTree - a PuTTY-Session-Manager-style launcher for plain OpenSSH.

* Sessions live in a JSON file (default: ~/.config/sshtree/sessions.json).
* Launching a session opens the system terminal (Windows Terminal,
  Terminal.app, gnome-terminal, ...) with the right `ssh` command line.
* No passwords are ever stored - only key paths and ssh options.
* On Windows a session can also be opened in WinSCP.

Standard library only (tkinter), so it runs unchanged on Windows, macOS and
Linux.  Run with `pythonw sshtree.py` on Windows to avoid a console window.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

APP_NAME = "SSH Tree"
__version__ = "0.2.3"
FROZEN = bool(getattr(sys, "frozen", False))  # running from a PyInstaller build
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# --------------------------------------------------------------------------
# Configuration file handling
# --------------------------------------------------------------------------

EXAMPLE_CONFIG = {
    "defaults": {
        "user": "root",
        "port": 22,
        "identity_file": "~/.ssh/id_ed25519",
        "options": {"ServerAliveInterval": 30},
        "hold_on_exit": "error",
        "terminal": "auto",
        "terminal_path": "auto",
        "ssh_path": "auto",
        "winscp_path": "auto",
        "winscp_protocol": "sftp",
        "ssh_app": "openssh",
        "scp_app": "winscp",
        "close_action": "exit",
    },
    "tree": [
        {
            "name": "example-host",
            "host": "192.0.2.10",
            "user": "admin",
        },
        {
            "name": "Home lab",
            "defaults": {"user": "alice", "identity_file": "~/.ssh/homelab"},
            "children": [
                {"name": "nas", "host": "nas.lan"},
                {"name": "proxmox", "host": "pve.lan", "user": "root", "port": 2222},
                {
                    "name": "db tunnel",
                    "host": "bastion.lan",
                    "no_shell": True,
                    "tunnels": [
                        {
                            "type": "local",
                            "listen": "5432",
                            "target": "db.internal:5432",
                        },
                        "L 127.0.0.1:8080 web.internal:80",
                        "D 1080",
                    ],
                },
                {
                    "name": "Customers",
                    "children": [
                        {
                            "name": "acme-prod",
                            "host": "10.0.0.5",
                            "jump_host": "alice@gw.acme.example",
                            "options": {"StrictHostKeyChecking": "accept-new"},
                            "agent_forwarding": True,
                            "command": "sudo -i",
                        }
                    ],
                },
            ],
        },
    ],
}

MERGE_DICT_KEYS = {"options"}
MERGE_LIST_KEYS = {"extra_args", "tunnels", "winscp_args"}
NON_SETTING_KEYS = {"name", "children", "defaults"}


def default_config_path() -> Path:
    env = os.environ.get("SSHTREE_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "sshtree" / "sessions.json"


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Config:
    """The sessions.json file: defaults plus a tree of folders and sessions."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self.mtime: float | None = None
        self.created = False

    def load(self) -> None:
        if not self.path.exists():
            atomic_write_json(self.path, EXAMPLE_CONFIG)
            self.created = True
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root must be a JSON object")
        data.setdefault("defaults", {})
        data.setdefault("tree", [])
        self.data = data
        self.mtime = self.path.stat().st_mtime

    def save(self) -> None:
        atomic_write_json(self.path, self.data)
        self.mtime = self.path.stat().st_mtime

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self.mtime
        except OSError:
            return False

    @property
    def defaults(self) -> dict:
        return self.data["defaults"]

    @property
    def tree(self) -> list:
        return self.data["tree"]

    # -- tree helpers -----------------------------------------------------

    def find(self, spec: str):
        """Find a session by 'Folder/Sub/name' path or by bare name.

        Returns (session, ancestor_folders) or None.
        """
        parts = [p for p in spec.split("/") if p]
        if not parts:
            return None
        # exact path first
        hit = self._find_path(self.tree, parts, [])
        if hit:
            return hit
        # otherwise first session anywhere with that name
        for session, folders in walk_sessions(self.tree, []):
            if session.get("name") == spec:
                return session, folders
        return None

    def _find_path(self, nodes, parts, folders):
        for node in nodes:
            if node.get("name") != parts[0]:
                continue
            if len(parts) == 1:
                if not is_folder(node):
                    return node, folders
            elif is_folder(node):
                hit = self._find_path(node["children"], parts[1:], folders + [node])
                if hit:
                    return hit
        return None


def is_folder(node: dict) -> bool:
    return isinstance(node, dict) and isinstance(node.get("children"), list)


def walk_sessions(nodes, folders):
    """Yield (session, ancestor_folders) for every session below `nodes`."""
    for node in nodes:
        if is_folder(node):
            yield from walk_sessions(node["children"], folders + [node])
        elif isinstance(node, dict):
            yield node, folders


def walk_folders(nodes, path=()):
    """Yield (folder_node, path_tuple) for every folder, depth first."""
    for node in nodes:
        if is_folder(node):
            p = path + (node.get("name", ""),)
            yield node, p
            yield from walk_folders(node["children"], p)


def merge_settings(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if key in NON_SETTING_KEYS:
            continue
        if key in MERGE_DICT_KEYS:
            merged = dict(base.get(key) or {})
            merged.update(value or {})
            out[key] = merged
        elif key in MERGE_LIST_KEYS:
            if isinstance(value, str):
                value = [value]
            out[key] = list(base.get(key) or []) + list(value or [])
        else:
            out[key] = value
    return out


def effective_settings(cfg: Config, session: dict, folders: list) -> dict:
    """Global defaults -> folder defaults (outer to inner) -> session."""
    settings = merge_settings({}, cfg.defaults)
    for folder in folders:
        settings = merge_settings(settings, folder.get("defaults") or {})
    settings = merge_settings(settings, session)
    settings["name"] = session.get("name", "")
    return settings


# --------------------------------------------------------------------------
# Building command lines
# --------------------------------------------------------------------------


def expand_path(p: str) -> str:
    return os.path.normpath(os.path.expandvars(os.path.expanduser(str(p))))


def truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def option_value(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


TUNNEL_TYPES = {
    "L": "L",
    "LOCAL": "L",
    "R": "R",
    "REMOTE": "R",
    "D": "D",
    "DYNAMIC": "D",
    "SOCKS": "D",
}


def parse_tunnel(spec) -> tuple[str, str]:
    """Return (flag, argument), e.g. ('-L', '8080:localhost:80').

    Accepts dicts {"type","listen","target"} and strings such as
    "L 8080 localhost:80", "R 2222 localhost:22", "D 1080",
    "L 8080:localhost:80" or "-L 127.0.0.1:8080:web:80".
    """
    if isinstance(spec, dict):
        kind = str(spec.get("type", "L"))
        listen = str(spec.get("listen") or spec.get("bind") or "")
        target = str(spec.get("target") or "")
        if spec.get("host") and spec.get("port") and not target:
            target = f"{spec['host']}:{spec['port']}"
    else:
        tokens = str(spec).split()
        if not tokens:
            raise ValueError("empty tunnel spec")
        kind = tokens[0].lstrip("-")
        listen = target = ""
        if len(tokens) >= 3:
            listen, target = tokens[1], tokens[2]
        elif len(tokens) == 2:
            parts = tokens[1].split(":")
            if kind.upper()[0] == "D" or len(parts) <= 2:
                listen = tokens[1]
            elif len(parts) == 3:
                listen, target = parts[0], f"{parts[1]}:{parts[2]}"
            else:
                listen, target = f"{parts[0]}:{parts[1]}", f"{parts[2]}:{parts[3]}"
    flag = TUNNEL_TYPES.get(kind.upper())
    if not flag:
        raise ValueError(f"unknown tunnel type {kind!r} in {spec!r}")
    if not listen:
        raise ValueError(f"tunnel without listen port: {spec!r}")
    if flag == "D":
        return "-D", listen
    if not target:
        raise ValueError(f"tunnel without target: {spec!r}")
    return f"-{flag}", f"{listen}:{target}"


def identity_files(s: dict) -> list[str]:
    """`identity_file` may be one path or a list; ssh offers them in order,
    which stands in for an agent holding several keys."""
    value = s.get("identity_file")
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [expand_path(v) for v in value if str(v).strip()]


WINDOWS_OPENSSH = r"%SystemRoot%\System32\OpenSSH\ssh.exe"


def resolve_ssh_path(configured) -> str:
    """`ssh_path`: explicit path, or "ssh"/"auto".  On Windows "auto" prefers
    the built-in OpenSSH over Git's MSYS ssh, which is usually first on the
    PATH but cannot talk to a Windows named-pipe agent such as Pageant."""
    value = str(configured or "auto").strip()
    if value.lower() == "ssh":
        return "ssh"  # explicitly: whatever ssh is first on the PATH
    if value.lower() != "auto":
        return expand_path(value)
    if IS_WINDOWS:
        native = os.path.expandvars(WINDOWS_OPENSSH)
        if os.path.isfile(native):
            return native
    return "ssh"


def build_ssh_command(s: dict) -> list[str]:
    """The bare ssh command line (no terminal, no hold wrapper)."""
    host = str(s.get("host") or "").strip()
    if not host:
        raise ValueError(f"session {s.get('name', '?')!r} has no host")
    args = [resolve_ssh_path(s.get("ssh_path"))]
    port = s.get("port")
    if port not in (None, "", 22, "22"):
        args += ["-p", str(port)]
    for identity in identity_files(s):
        args += ["-i", identity]
    jump = s.get("jump_host")
    if jump:
        args += ["-J", str(jump)]
    if truthy(s.get("agent_forwarding")):
        args.append("-A")
    if truthy(s.get("x11")):
        args.append("-X")
    if truthy(s.get("no_shell")):
        args.append("-N")
    if truthy(s.get("verbose")):
        args.append("-v")
    for key, value in (s.get("options") or {}).items():
        if value is None or value == "":
            continue
        args += ["-o", f"{key}={option_value(value)}"]
    for spec in s.get("tunnels") or []:
        args += list(parse_tunnel(spec))
    extra = s.get("extra_args") or []
    if isinstance(extra, str):
        extra = shlex.split(extra)
    for item in extra:
        if isinstance(item, str) and " " in item and not item.startswith("-"):
            args += shlex.split(item)
        else:
            args.append(str(item))
    user = s.get("user")
    args.append(f"{user}@{host}" if user else host)
    command = s.get("command")
    if command:
        args.append(str(command))
    return args


def normalize_hold_mode(mode) -> str:
    mode = str(mode or "error").lower()
    if mode in ("never", "no", "false", "0"):
        return "never"
    if mode in ("always", "yes", "true", "1"):
        return "always"
    return "error"


def console_python() -> str:
    """The console flavour of the running interpreter (python.exe, not pythonw)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        twin = exe.with_name("python.exe")
        if twin.exists():
            return str(twin)
    return str(exe)


def wrap_hold_on_exit(cmd: list[str], mode: str) -> list[str]:
    """Run `cmd` through this script's `--exec` runner inside the terminal.

    The runner decides whether the window stays open after ssh ends ("error":
    only on ssh's own failure code 255, "always", "never").  It gets the same
    Ctrl+C as ssh, so an interrupted session closes silently instead of
    counting as a failure, and it always exits 0 so Windows Terminal closes
    the tab instead of showing "[process exited with code N]".
    """
    runner = console_runner()
    return [*runner, "--exec", normalize_hold_mode(mode), "--", *cmd]


def console_runner() -> list[str]:
    """Command prefix that runs this program with a console: the console
    build (sshtree-cli.exe) next to a frozen GUI build, else the script."""
    if FROZEN:
        exe = Path(sys.executable)
        cli = exe.with_name("sshtree-cli.exe" if IS_WINDOWS else "sshtree-cli")
        return [str(cli if cli.exists() else exe)]
    return [console_python(), str(Path(__file__).resolve())]


def wait_key(prompt: str) -> None:
    print(prompt, end="", flush=True)
    try:
        if IS_WINDOWS:
            import msvcrt

            msvcrt.getch()
        else:
            input()
    except (KeyboardInterrupt, EOFError):
        pass
    print()


def run_in_terminal(mode: str, cmd: list[str]) -> int:
    """`--exec MODE -- cmd...`: run cmd, then hold the window as configured."""
    mode = normalize_hold_mode(mode)
    try:
        proc = subprocess.Popen(cmd)
    except OSError as exc:
        wait_key(f"[cannot start {cmd[0]!r}: {exc}] press any key to close ")
        return 0
    interrupted = False
    while True:
        try:
            rc = proc.wait()
            break
        except KeyboardInterrupt:
            interrupted = True  # ssh got the same Ctrl+C; let it finish
    if mode == "always" or (mode == "error" and rc == 255 and not interrupted):
        wait_key(f"\n[ssh exited with status {rc}] press any key to close ")
    return 0


def find_winscp(configured) -> str | None:
    if configured and str(configured).lower() != "auto":
        p = expand_path(configured)
        return p if os.path.exists(p) else None
    candidates = [
        shutil.which("WinSCP"),
        os.path.expandvars(r"%ProgramFiles(x86)%\WinSCP\WinSCP.exe"),
        os.path.expandvars(r"%ProgramFiles%\WinSCP\WinSCP.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\WinSCP\WinSCP.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def winscp_key_for(s: dict) -> str | None:
    """Prefer an explicit .ppk; else a sibling <key>.ppk; else the OpenSSH key."""
    explicit = s.get("winscp_key")
    if explicit:
        return expand_path(explicit)
    keys = identity_files(s)
    if not keys:
        return None
    identity = keys[0]
    ppk = identity + ".ppk"
    if os.path.exists(ppk):
        return ppk
    root, _ = os.path.splitext(identity)
    if os.path.exists(root + ".ppk"):
        return root + ".ppk"
    return identity


def build_winscp_command(s: dict) -> list[str]:
    exe = find_winscp(s.get("winscp_path"))
    if not exe:
        raise FileNotFoundError("WinSCP.exe not found - set defaults.winscp_path")
    host = str(s.get("host") or "").strip()
    if not host:
        raise ValueError(f"session {s.get('name', '?')!r} has no host")
    protocol = str(s.get("winscp_protocol") or "sftp").lower()
    user = s.get("user")
    port = s.get("port") or 22
    url = f"{protocol}://{user + '@' if user else ''}{host}:{port}/"
    args = [exe, url]
    key = winscp_key_for(s)
    if key:
        args.append(f"/privatekey={key}")
    jump = s.get("jump_host")
    if jump:
        juser, _, jhost = str(jump).rpartition("@")
        jhost, _, jport = jhost.partition(":")
        raw = ["Tunnel=1", f"TunnelHostName={jhost}", f"TunnelPortNumber={jport or 22}"]
        if juser:
            raw.append(f"TunnelUserName={juser}")
        if key:
            raw.append(f"TunnelPublicKeyFile={key}")
        args += ["/rawsettings", *raw]
    extra = s.get("winscp_args") or []
    if isinstance(extra, str):
        extra = shlex.split(extra)
    args += [str(a) for a in extra]
    return args


# --------------------------------------------------------------------------
# Which application opens a session (OpenSSH in a terminal, or PuTTY) and
# which one does file transfer (WinSCP, FileZilla, a custom command)
# --------------------------------------------------------------------------

SSH_APPS = ("openssh", "putty")
TRANSFER_APPS = ("winscp", "filezilla", "custom", "none")
TRANSFER_LABELS = {
    "winscp": "Open in WinSCP",
    "filezilla": "Open in FileZilla",
    "custom": "Open File Transfer",
}


def find_program(configured, names: list[str], candidates: list[str]) -> str | None:
    """An explicit path (if it exists), else the first of `names` on the PATH,
    else the first existing path in `candidates` (with %VARS% expanded)."""
    if configured and str(configured).lower() != "auto":
        p = expand_path(configured)
        return p if os.path.exists(p) else None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for cand in candidates:
        path = os.path.expandvars(cand)
        if "%" not in path and os.path.isfile(path):
            return path
    return None


def find_putty(configured) -> str | None:
    return find_program(
        configured,
        ["putty"],
        [
            r"%ProgramFiles%\PuTTY\putty.exe",
            r"%ProgramFiles(x86)%\PuTTY\putty.exe",
            r"%LOCALAPPDATA%\Programs\PuTTY\putty.exe",
            r"%ProgramData%\chocolatey\bin\putty.exe",
        ],
    )


def find_filezilla(configured) -> str | None:
    return find_program(
        configured,
        ["filezilla"],
        [
            r"%ProgramFiles%\FileZilla FTP Client\filezilla.exe",
            r"%ProgramFiles(x86)%\FileZilla FTP Client\filezilla.exe",
            "/Applications/FileZilla.app/Contents/MacOS/filezilla",
        ],
    )


def ssh_app_of(s: dict) -> str:
    app = str(s.get("ssh_app") or "openssh").strip().lower()
    return app if app in SSH_APPS else "openssh"


def transfer_app_of(s: dict) -> str:
    app = (
        str(s.get("scp_app") or ("winscp" if IS_WINDOWS else "filezilla"))
        .strip()
        .lower()
    )
    return app if app in TRANSFER_APPS else "winscp"


def build_putty_command(s: dict) -> list[str]:
    """PuTTY instead of OpenSSH: same session settings, PuTTY's switches.
    `options` and `command` have no PuTTY equivalent and are ignored."""
    exe = find_putty(s.get("putty_path"))
    if not exe:
        raise FileNotFoundError("putty.exe not found - set putty_path in Settings")
    host = str(s.get("host") or "").strip()
    if not host:
        raise ValueError(f"session {s.get('name', '?')!r} has no host")
    args = [exe, "-ssh"]
    if s.get("user"):
        args += ["-l", str(s["user"])]
    port = s.get("port")
    if port not in (None, "", 22, "22"):
        args += ["-P", str(port)]
    key = winscp_key_for(s)  # prefers a .ppk next to the OpenSSH key
    if key:
        args += ["-i", key]
    if truthy(s.get("agent_forwarding")):
        args.append("-A")
    if truthy(s.get("x11")):
        args.append("-X")
    if truthy(s.get("no_shell")):
        args.append("-N")
    for spec in s.get("tunnels") or []:
        args += list(parse_tunnel(spec))  # -L/-R/-D take the same syntax
    jump = s.get("jump_host")
    if jump:
        args += ["-proxycmd", f"plink -batch -nc %host:%port {jump}"]
    extra = s.get("extra_args") or []
    if isinstance(extra, str):
        extra = shlex.split(extra)
    args += [str(a) for a in extra]
    args.append(host)
    return args


def build_session_command(s: dict) -> list[str]:
    """The command that opens the session, for the chosen SSH application."""
    if ssh_app_of(s) == "putty":
        return build_putty_command(s)
    return build_ssh_command(s)


def build_filezilla_command(s: dict) -> list[str]:
    exe = find_filezilla(s.get("filezilla_path"))
    if not exe:
        raise FileNotFoundError("filezilla not found - set filezilla_path in Settings")
    host = str(s.get("host") or "").strip()
    if not host:
        raise ValueError(f"session {s.get('name', '?')!r} has no host")
    user = s.get("user")
    port = s.get("port") or 22
    return [exe, f"sftp://{user + '@' if user else ''}{host}:{port}"]


def build_custom_transfer_command(s: dict) -> list[str]:
    """`scp_command`: a list (or string) with {host} {user} {port} {key} {url}
    {userhost} placeholders, e.g. ["C:/Tools/MyScp.exe", "{url}"]."""
    template = s.get("scp_command") or []
    if isinstance(template, str):
        template = shlex.split(template)
    if not template:
        raise ValueError("scp_app is 'custom' but scp_command is not set")
    host = str(s.get("host") or "").strip()
    user = str(s.get("user") or "")
    port = str(s.get("port") or 22)
    keys = identity_files(s)
    values = {
        "host": host,
        "user": user,
        "port": port,
        "key": keys[0] if keys else "",
        "userhost": f"{user}@{host}" if user else host,
        "url": f"sftp://{user + '@' if user else ''}{host}:{port}/",
    }
    out = []
    for element in template:
        text = str(element)
        for name, value in values.items():
            text = text.replace("{" + name + "}", value)
        out.append(text)
    return out


def build_file_transfer_command(s: dict) -> list[str]:
    app = transfer_app_of(s)
    if app == "winscp":
        return build_winscp_command(s)
    if app == "filezilla":
        return build_filezilla_command(s)
    if app == "custom":
        return build_custom_transfer_command(s)
    raise ValueError("no file transfer application configured (scp_app)")


# --------------------------------------------------------------------------
# Terminal launching
# --------------------------------------------------------------------------

# Template tokens: "{cmd}" as a whole element is replaced by the command
# list; "{title}" and "{cmdline}" are substituted inside elements.
TERMINAL_PRESETS: dict[str, list[str]] = {
    "wt": ["wt", "-w", "last", "new-tab", "--title", "{title}", "{cmd}"],
    "wt-window": ["wt", "-w", "new", "new-tab", "--title", "{title}", "{cmd}"],
    "console": ["cmd", "/c", "title", "{title}", "&", "{cmd}"],
    "macos-terminal": [
        "osascript",
        "-e",
        'tell application "Terminal" to do script "{cmdline_applescript}"',
        "-e",
        'tell application "Terminal" to activate',
    ],
    "iterm": [
        "osascript",
        "-e",
        'tell application "iTerm" to create window with default profile command "{cmdline_applescript}"',
    ],
    "gnome-terminal": ["gnome-terminal", "--title", "{title}", "--", "{cmd}"],
    "konsole": ["konsole", "-p", "tabtitle={title}", "-e", "{cmd}"],
    "xfce4-terminal": ["xfce4-terminal", "--title", "{title}", "-x", "{cmd}"],
    "kitty": ["kitty", "--title", "{title}", "{cmd}"],
    "alacritty": ["alacritty", "--title", "{title}", "-e", "{cmd}"],
    "wezterm": ["wezterm", "start", "--", "{cmd}"],
    "foot": ["foot", "--title", "{title}", "{cmd}"],
    "x-terminal-emulator": ["x-terminal-emulator", "-T", "{title}", "-e", "{cmd}"],
    "xterm": ["xterm", "-T", "{title}", "-e", "{cmd}"],
}

LINUX_AUTO_ORDER = [
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "kitty",
    "alacritty",
    "wezterm",
    "foot",
    "x-terminal-emulator",
    "xterm",
]


# Where a Windows Terminal that is not on the PATH may live (store app alias,
# portable/zip installs).  WindowsTerminal.exe takes the same arguments as wt.exe.
WINDOWS_TERMINAL_CANDIDATES = [
    r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe",
    r"%USERPROFILE%\Programs\WindowsTerminal\WindowsTerminal.exe",
    r"%USERPROFILE%\Programs\WindowsTerminal\wt.exe",
    r"%LOCALAPPDATA%\Programs\WindowsTerminal\WindowsTerminal.exe",
    r"%LOCALAPPDATA%\Programs\WindowsTerminal\wt.exe",
    r"%ProgramFiles%\WindowsTerminal\WindowsTerminal.exe",
    r"%ProgramFiles%\WindowsTerminal\wt.exe",
]


def find_windows_terminal() -> str | None:
    found = shutil.which("wt")
    if found:
        return found
    for candidate in WINDOWS_TERMINAL_CANDIDATES:
        path = os.path.expandvars(candidate)
        if "%" not in path and os.path.isfile(path):
            return path
    return None


def detect_terminal() -> tuple[str, str | None]:
    """Return (preset name, executable path or None for 'use the preset's own')."""
    if IS_WINDOWS:
        wt = find_windows_terminal()
        return ("wt", wt) if wt else ("console", None)
    if IS_MAC:
        return "macos-terminal", None
    for name in LINUX_AUTO_ORDER:
        if shutil.which(name):
            return name, None
    return "xterm", None


def resolve_terminal(setting, path_setting=None) -> tuple[str, list[str]]:
    """Return (preset_name_or_'custom', template).

    `path_setting` (config key `terminal_path`) replaces the executable of the
    chosen preset, e.g. a portable WindowsTerminal.exe.  With `auto` the
    Windows Terminal is searched on the PATH and in known install folders.
    """
    if isinstance(setting, list):
        return "custom", [str(x) for x in setting]
    name = str(setting or "auto").strip().lower()
    exe = None
    if path_setting and str(path_setting).strip().lower() != "auto":
        exe = expand_path(path_setting)
        if not os.path.isfile(exe) and not shutil.which(exe):
            raise FileNotFoundError(f"terminal_path not found: {exe}")
    if name == "auto":
        name, detected = detect_terminal()
        exe = exe or detected
    elif name.startswith("wt") and exe is None and IS_WINDOWS:
        exe = find_windows_terminal()
    if name not in TERMINAL_PRESETS:
        known = ", ".join(sorted(TERMINAL_PRESETS))
        raise ValueError(f"unknown terminal {name!r}; known presets: auto, {known}")
    template = list(TERMINAL_PRESETS[name])
    if exe:
        template[0] = exe
    return name, template


def cmdline_string(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if IS_WINDOWS else shlex.join(cmd)


def expand_template(template: list[str], title: str, cmd: list[str]) -> list[str]:
    line = cmdline_string(cmd)
    applescript = line.replace("\\", "\\\\").replace('"', '\\"')
    out: list[str] = []
    for element in template:
        if element == "{cmd}":
            out.extend(cmd)
        else:
            out.append(
                element.replace("{title}", title)
                .replace("{cmdline_applescript}", applescript)
                .replace("{cmdline}", line)
            )
    return out


def terminal_command(s: dict, cmd: list[str]) -> tuple[list[str], str]:
    """Full argv that opens a terminal running `cmd`, plus the preset name."""
    name, template = resolve_terminal(s.get("terminal"), s.get("terminal_path"))
    title = str(s.get("title") or s.get("name") or "ssh")
    cmd = wrap_hold_on_exit(cmd, str(s.get("hold_on_exit") or "error"))
    if name.startswith("wt"):
        # ';' separates wt sub-commands and has to be escaped
        cmd = [a.replace(";", "\\;") for a in cmd]
    return expand_template(template, title, cmd), name


def spawn(argv: list[str], new_console: bool = False) -> None:
    kwargs: dict = {"close_fds": True}
    if IS_WINDOWS:
        flags = 0
        if new_console:
            flags |= subprocess.CREATE_NEW_CONSOLE
        else:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
        kwargs["stdin"] = kwargs["stdout"] = kwargs["stderr"] = subprocess.DEVNULL
    subprocess.Popen(argv, **kwargs)


def launch_session(s: dict) -> list[str]:
    if ssh_app_of(s) == "putty":
        argv = build_putty_command(s)  # PuTTY brings its own window
        spawn(argv)
        return argv
    cmd = build_ssh_command(s)
    argv, preset = terminal_command(s, cmd)
    spawn(argv, new_console=(preset == "console"))
    return argv


def launch_file_transfer(s: dict) -> list[str]:
    argv = build_file_transfer_command(s)
    spawn(argv)
    return argv


def launch_winscp(s: dict) -> list[str]:
    """Kept for callers of the 0.1 API; the app is chosen by `scp_app` now."""
    return launch_file_transfer(s)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Single-instance activation and foreground handling
#
# The app registers no hotkey of its own.  Bind the key in your hotkey tool
# (AutoHotkey, a launcher, a desktop shortcut) to this script: a second launch
# just activates the running window through a localhost socket.
# --------------------------------------------------------------------------


class InstanceServer:
    """Listens on 127.0.0.1 so that a second launch (for example from a desktop
    shortcut bound to a key) can activate this window instead of starting a
    second copy.  Port and a random token are written to `info_path`."""

    def __init__(self, info_path: Path, on_message):
        self.info_path = info_path
        self.on_message = on_message
        self.token = secrets.token_hex(16)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.info = {"port": self.port, "token": self.token, "pid": os.getpid()}
        atomic_write_json(info_path, self.info)
        threading.Thread(target=self._serve, name="instance", daemon=True).start()

    def update(self, **fields) -> None:
        """Add fields (e.g. the window handle) to the instance file."""
        self.info.update(fields)
        try:
            atomic_write_json(self.info_path, self.info)
        except OSError:
            pass

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    data = conn.recv(4096).decode("utf-8", "replace").strip()
                    token, _, command = data.partition(" ")
                    if token == self.token and command:
                        self.on_message(command)
                        conn.sendall(b"ok\n")
                    else:
                        conn.sendall(b"denied\n")
                except OSError:
                    pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            with open(self.info_path, encoding="utf-8") as fh:
                mine = json.load(fh).get("pid") == os.getpid()
            if mine:  # unlink only after the handle is closed (Windows)
                self.info_path.unlink()
        except (OSError, ValueError):
            pass


def describe_foreground() -> str:
    """'hwnd pid title' of the current foreground window (for the log)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(80)
    user32.GetWindowTextW(hwnd, buf, 80)
    return f"{hwnd or 0:#x} pid={pid.value} {buf.value[:40]!r}"


def force_foreground_windows(hwnd: int, log=None) -> bool:
    """Bring `hwnd` to the foreground even when Windows would deny it.

    A process that did not receive the last input event (ours, when a hotkey
    tool or a second launch asks us to show) gets SetForegroundWindow silently
    refused.  The usual layered escalation (as in launchers such as AutoHotkey): plain call
    first; if denied, release held modifiers, tap ALT (which releases the
    foreground lock), attach our input queue to the foreground thread and try
    again a few times.  Returns True when we really are in the foreground.
    """
    import ctypes
    import time
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    KEYUP = 0x0002
    VK_MENU, VK_CONTROL, VK_SHIFT, VK_LWIN, VK_RWIN = 0x12, 0x11, 0x10, 0x5B, 0x5C

    def activate() -> bool:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.SetFocus(hwnd)
        return user32.GetForegroundWindow() == hwnd

    if user32.GetForegroundWindow() == hwnd or activate():
        return True
    if log:
        log(f"plain activation denied; foreground is {describe_foreground()}")

    # Denied: escalate.  Held modifiers (the hotkey is probably still pressed)
    # would turn the ALT tap into a chord, so release them first.
    for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.keybd_event(vk, 0, KEYUP, None)
    our_thread = kernel32.GetCurrentThreadId()
    for attempt in range(3):
        if attempt:
            time.sleep(0.03)
        user32.keybd_event(VK_MENU, 0, 0, None)
        user32.keybd_event(VK_MENU, 0, KEYUP, None)
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        attached = bool(
            fg_thread
            and fg_thread != our_thread
            and user32.AttachThreadInput(our_thread, fg_thread, True)
        )
        ok = activate()
        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)
        if log:
            log(
                f"attempt {attempt + 1}: attached={attached} ok={ok}; foreground is {describe_foreground()}"
            )
        if ok:
            return True
    return False


def activate_running_instance(info_path: Path, command: str = "show") -> bool:
    """Send `command` to a running instance; True if one answered."""
    try:
        with open(info_path, encoding="utf-8") as fh:
            info = json.load(fh)
        if IS_WINDOWS and command == "show":
            # We were just started by the user's hotkey tool / shell, so we may
            # hold the right to set the foreground window; hand it to the
            # running instance before asking it to show itself.
            try:
                import ctypes

                ctypes.WinDLL("user32").AllowSetForegroundWindow(
                    int(info.get("pid", -1))
                )
            except (OSError, ValueError, AttributeError):
                pass
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=1) as s:
            s.sendall(f"{info['token']} {command}\n".encode("utf-8"))
            s.settimeout(3)
            ok = s.recv(16).strip() == b"ok"
        if ok and IS_WINDOWS and command == "show" and info.get("hwnd"):
            # This process was started by the user's action (hotkey tool,
            # shortcut), so it is the one Windows lets set the foreground
            # window.  Do it from here as well, after the app had a moment
            # to show itself; the app's own attempt may be refused.
            foreground_from_launcher(int(info["hwnd"]))
        return ok
    except (OSError, ValueError, KeyError, TypeError):
        return False


def foreground_from_launcher(hwnd: int, seconds: float = 1.5) -> bool:
    import ctypes
    import time
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------


def apply_settings(defaults: dict, values: dict) -> list[str]:
    """Write Settings dialog values into the defaults dict; empty means
    'remove the key'.  Returns the keys that changed."""
    changed = []
    for key, value in values.items():
        old = defaults.get(key)
        if value == "":
            if key in defaults:
                del defaults[key]
                changed.append(key)
        elif str(old) != value:
            defaults[key] = value
            changed.append(key)
    return changed


def run_gui(cfg: Config, on_ready=None, start_hidden: bool = False) -> None:
    """Start the Tk application.  `on_ready(app)` is called once the window
    is built (used by the smoke test to drive the GUI).  With `start_hidden`
    the window waits in the background until a later launch activates it."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    FOLDER_PX = [
        "................",
        "................",
        ".ooooo..........",
        "oyyyyyo.........",
        "oyyyyyyooooooooo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oooooooooooooooo",
        "................",
        "................",
    ]
    FOLDER_OPEN_PX = [
        "................",
        "................",
        ".ooooo..........",
        "oyyyyyo.........",
        "oyyyyyyooooooooo",
        "oyyyyyyyyyyyyyyo",
        "oyyyyyyyyyyyyyyo",
        "oooooooooooooooo",
        "ollllllllllllllo",
        "ollllllllllllllo",
        "ollllllllllllllo",
        "ollllllllllllllo",
        "ollllllllllllllo",
        "oooooooooooooooo",
        "................",
        "................",
    ]
    SESSION_PX = [
        "................",
        ".bbbbbbbbbbbbbb.",
        ".bwwwwwwwwwwwwb.",
        ".bwkkkkkkkkkkwb.",
        ".bwkgkkkkkkkkwb.",
        ".bwkkgkkkkkkkwb.",
        ".bwkgkkkkkkkkwb.",
        ".bwkkkkkkkkkkwb.",
        ".bwkkkkkkkkkkwb.",
        ".bwwwwwwwwwwwwb.",
        ".bbbbbbbbbbbbbb.",
        "......bbbb......",
        "......bbbb......",
        "....bbbbbbbb....",
        "................",
        "................",
    ]
    PALETTE = {
        "o": "#a07820",
        "y": "#f2c94c",
        "l": "#f7dc82",
        "b": "#3b5f8a",
        "w": "#c8d8ea",
        "k": "#101820",
        "g": "#40e060",
        "t": "#f0a030",
    }

    def pixmap(rows, palette, pad=5):
        """Build a PhotoImage; `pad` transparent columns on the right keep the
        tree text from touching the icon (ttk adds no spacing itself)."""
        rows = [row + "." * pad for row in rows]
        h, w = len(rows), len(rows[0])
        img = tk.PhotoImage(width=w, height=h)
        data = " ".join(
            "{" + " ".join(palette.get(c, "#000000") for c in row) + "}" for row in rows
        )
        img.put(data, to=(0, 0))
        for y, row in enumerate(rows):
            for x, c in enumerate(row):
                if c == ".":
                    img.transparency_set(x, y, True)
        return img

    class SessionDialog(tk.Toplevel):
        """Create / edit one session.  Empty fields mean 'inherit'."""

        # (key, label, kind, tab[, combo values]); empty fields mean "inherit"
        TABS = ["Session", "Tunnels", "SSH", "Terminal", "File transfer"]
        TAB_HELP = {
            "Tunnels": (
                "Port forwardings through this SSH connection, one per line. "
                "Each line is: type, the port to listen on, and (except for D) "
                "the target as host:port.\n\n"
                "L 8080 db.internal:5432   Local forward: connections to port 8080 "
                "on this PC are sent through the SSH host to db.internal port 5432. "
                "Use it to reach services that are only visible from the SSH host.\n\n"
                "R 2222 localhost:22   Remote forward: the opposite direction. "
                "Port 2222 on the SSH host is sent back to port 22 on this PC.\n\n"
                "D 1080   Dynamic forward: a SOCKS proxy on port 1080 of this PC; "
                "any program set to use it (browser, etc.) goes out via the SSH host.\n\n"
                "Prefix the port with an address to bind elsewhere than localhost, "
                "e.g. L 0.0.0.0:8080 web:80. Pick 'No shell' to keep the tunnel open "
                "without a login shell; the window then only shows the connection."
            ),
            "SSH": (
                "Extra ssh behaviour. Jump host connects through a bastion first "
                "(ProxyJump). Remote command runs instead of a login shell. "
                "Options are passed as -o Key=Value, e.g. ServerAliveInterval=30 "
                "or StrictHostKeyChecking=accept-new."
            ),
            "Terminal": (
                "How the terminal window behaves. Hold on exit: 'error' keeps the "
                "window open only when the connection itself failed, 'always' and "
                "'never' do what they say. Preset and executable choose the terminal "
                "program; leave them empty to use the global default."
            ),
            "File transfer": (
                "Used by 'Open in WinSCP / FileZilla'. The application itself is "
                "chosen in Settings (or per session here). WinSCP wants a PuTTY .ppk "
                "key; when empty, a .ppk next to the identity file is used, otherwise "
                "WinSCP falls back to Pageant."
            ),
        }
        FIELDS = [
            ("name", "Name", "entry", "Session"),
            ("host", "Host", "entry", "Session"),
            ("user", "User", "entry", "Session"),
            ("port", "Port", "entry", "Session"),
            ("identity_file", "Identity file", "file", "Session"),
            ("tunnels", "Tunnels, one per line", "text", "Tunnels"),
            ("no_shell", "No shell, tunnels only (-N)", "bool", "Tunnels"),
            ("ssh_app", "SSH application", "combo", "SSH", ["", *SSH_APPS]),
            ("jump_host", "Jump host (-J user@host)", "entry", "SSH"),
            ("command", "Remote command", "entry", "SSH"),
            ("extra_args", "Extra ssh arguments", "entry", "SSH"),
            ("options", "ssh -o options, one per line\n(Key=Value)", "text", "SSH"),
            ("agent_forwarding", "Agent forwarding (-A)", "bool", "SSH"),
            ("x11", "X11 forwarding (-X)", "bool", "SSH"),
            (
                "hold_on_exit",
                "Hold terminal on exit",
                "combo",
                "Terminal",
                ["", "never", "error", "always"],
            ),
            (
                "terminal",
                "Terminal preset",
                "combo",
                "Terminal",
                ["", "auto"] + sorted(TERMINAL_PRESETS),
            ),
            ("terminal_path", "Terminal executable", "file", "Terminal"),
            (
                "winscp_protocol",
                "WinSCP protocol",
                "combo",
                "File transfer",
                ["", "sftp", "scp"],
            ),
            ("winscp_key", "WinSCP key (.ppk)", "file", "File transfer"),
            (
                "scp_app",
                "File transfer application",
                "combo",
                "File transfer",
                ["", *TRANSFER_APPS],
            ),
        ]

        def __init__(self, parent, title, node, folder_paths, current_folder):
            super().__init__(parent)
            self.title(title)
            self.resizable(True, True)
            self.transient(parent)
            self.result = None
            self.vars: dict = {}
            self.texts: dict = {}
            node = node or {}
            # a custom terminal template (a list) cannot be edited in the combo; keep it as is
            self.preserved = {
                k: v for k, v in node.items() if k == "terminal" and isinstance(v, list)
            }

            outer = ttk.Frame(self, padding=10)
            outer.pack(fill="both", expand=True)
            self.notebook = ttk.Notebook(outer)
            self.notebook.pack(fill="both", expand=True)
            pages: dict[str, ttk.Frame] = {}
            rows: dict[str, int] = {}
            for tab in self.TABS:
                page = ttk.Frame(self.notebook, padding=10)
                page.columnconfigure(1, weight=1)
                self.notebook.add(page, text=tab)
                pages[tab] = page
                rows[tab] = 0
                if tab in self.TAB_HELP:
                    ttk.Label(
                        page, text=self.TAB_HELP[tab], justify="left", wraplength=560
                    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
                    rows[tab] = 1

            body = pages["Session"]
            ttk.Label(body, text="Folder").grid(row=0, column=0, sticky="nw", pady=2)
            self.folder_var = tk.StringVar(value=current_folder)
            ttk.Combobox(
                body,
                textvariable=self.folder_var,
                values=folder_paths,
                state="readonly",
            ).grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)
            rows["Session"] = 1

            for spec in self.FIELDS:
                key, label, kind, tab = spec[:4]
                body = pages[tab]
                row = rows[tab]
                rows[tab] = row + 1
                ttk.Label(body, text=label, justify="left").grid(
                    row=row, column=0, sticky="nw", pady=2, padx=(0, 8)
                )
                value = node.get(key)
                if kind == "text":
                    height = 8 if key == "tunnels" else 5
                    txt = tk.Text(body, height=height, width=46, wrap="none")
                    txt.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=2)
                    body.rowconfigure(row, weight=1)
                    if key == "options" and isinstance(value, dict):
                        txt.insert(
                            "1.0",
                            "\n".join(
                                f"{k}={option_value(v)}" for k, v in value.items()
                            ),
                        )
                    elif isinstance(value, list):
                        txt.insert(
                            "1.0", "\n".join(self._tunnel_to_line(t) for t in value)
                        )
                    self.texts[key] = txt
                elif kind == "bool":
                    var = tk.StringVar(
                        value=(
                            "" if value is None else ("yes" if truthy(value) else "no")
                        )
                    )
                    ttk.Combobox(
                        body,
                        textvariable=var,
                        values=["", "yes", "no"],
                        state="readonly",
                        width=10,
                    ).grid(row=row, column=1, sticky="w", pady=2)
                    self.vars[key] = var
                elif kind == "combo":
                    var = tk.StringVar(
                        value=(
                            "" if value is None or key in self.preserved else str(value)
                        )
                    )
                    ttk.Combobox(
                        body,
                        textvariable=var,
                        values=spec[4],
                        state="readonly",
                        width=14,
                    ).grid(row=row, column=1, sticky="w", pady=2)
                    self.vars[key] = var
                else:
                    if isinstance(value, list):
                        value = " ".join(str(v) for v in value)
                    var = tk.StringVar(value="" if value is None else str(value))
                    ent = ttk.Entry(body, textvariable=var, width=48)
                    ent.grid(row=row, column=1, sticky="ew", pady=2)
                    if kind == "file":
                        ttk.Button(
                            body,
                            text="...",
                            width=3,
                            command=lambda v=var: self._browse(v),
                        ).grid(row=row, column=2, padx=(4, 0))
                    self.vars[key] = var
                    if key == "name":
                        ent.focus_set()

            buttons = ttk.Frame(outer)
            buttons.pack(fill="x", pady=(10, 0))
            ttk.Label(
                buttons, text="Empty fields inherit the folder / global defaults."
            ).pack(side="left")
            ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
            ttk.Button(buttons, text="OK", command=self._ok).pack(side="right", padx=4)
            self.bind("<Escape>", lambda e: self.destroy())
            self.bind("<Control-Return>", lambda e: self._ok())
            self.minsize(560, 360)

            self.update_idletasks()
            x = parent.winfo_rootx() + 40
            y = parent.winfo_rooty() + 40
            self.geometry(f"+{x}+{y}")
            self.grab_set()
            self.wait_window(self)

        @staticmethod
        def _tunnel_to_line(t) -> str:
            if isinstance(t, dict):
                flag, arg = parse_tunnel(t)
                kind = flag.lstrip("-")
                if kind == "D":
                    return f"D {arg}"
                # arg is [bind:]port:host:port -> "L listen host:port"
                parts = arg.split(":")
                if len(parts) == 3:
                    return f"{kind} {parts[0]} {parts[1]}:{parts[2]}"
                return f"{kind} {parts[0]}:{parts[1]} {parts[2]}:{parts[3]}"
            return str(t)

        def _browse(self, var):
            initial = (
                os.path.dirname(expand_path(var.get()))
                if var.get()
                else str(Path.home() / ".ssh")
            )
            path = filedialog.askopenfilename(
                parent=self, initialdir=initial, title="Choose key file"
            )
            if path:
                home = str(Path.home())
                if path.startswith(home):
                    path = "~" + path[len(home) :]
                var.set(path.replace("\\", "/"))

        def _ok(self):
            result: dict = {}
            for spec in self.FIELDS:
                key, _label, kind = spec[:3]
                if kind == "text":
                    lines = [
                        ln.strip()
                        for ln in self.texts[key].get("1.0", "end").splitlines()
                        if ln.strip()
                    ]
                    if not lines:
                        continue
                    if key == "options":
                        opts = {}
                        for ln in lines:
                            k, sep, v = ln.partition("=")
                            if not sep:
                                messagebox.showerror(
                                    APP_NAME,
                                    f"Option needs Key=Value: {ln}",
                                    parent=self,
                                )
                                return
                            opts[k.strip()] = v.strip()
                        result[key] = opts
                    else:
                        for ln in lines:
                            try:
                                parse_tunnel(ln)
                            except ValueError as exc:
                                messagebox.showerror(
                                    APP_NAME, f"Bad tunnel: {exc}", parent=self
                                )
                                return
                        result[key] = lines
                    continue
                value = self.vars[key].get().strip()
                if value == "":
                    if key in self.preserved:
                        result[key] = self.preserved[key]
                    continue
                if kind == "bool":
                    result[key] = value == "yes"
                elif key == "port":
                    if not value.isdigit():
                        messagebox.showerror(
                            APP_NAME, "Port must be a number", parent=self
                        )
                        return
                    result[key] = int(value)
                else:
                    result[key] = value
            if not result.get("name"):
                messagebox.showerror(APP_NAME, "A session needs a name", parent=self)
                return
            self.result = (self.folder_var.get(), result)
            self.destroy()

    class SettingsDialog(tk.Toplevel):
        """Global defaults that are about *this machine*: which programs open
        sessions and transfers, the terminal, and a few conveniences.  Empty
        means 'not set' (the built-in default applies)."""

        TABS = ["SSH", "Terminal", "File transfer", "General"]
        TAB_HELP = {
            "SSH": (
                "OpenSSH runs inside the terminal chosen on the Terminal tab; "
                "PuTTY opens its own window. 'auto' finds the programs itself: "
                "the Windows OpenSSH in System32, putty.exe in Program Files or "
                "on the PATH."
            ),
            "File transfer": (
                "The program behind 'Open in ...' on a session. 'custom' runs the "
                "command below with {host} {user} {port} {key} {url} {userhost} "
                "filled in, e.g.  C:/Tools/MyScp.exe {url}"
            ),
        }
        FIELDS = [
            ("ssh_app", "SSH application", "combo", "SSH", ["", *SSH_APPS]),
            ("ssh_path", "ssh executable (auto)", "file", "SSH"),
            ("putty_path", "putty executable (auto)", "file", "SSH"),
            ("user", "Default login user", "entry", "SSH"),
            ("identity_file", "Default identity file", "file", "SSH"),
            (
                "terminal",
                "Terminal preset",
                "combo",
                "Terminal",
                ["", "auto"] + sorted(TERMINAL_PRESETS),
            ),
            ("terminal_path", "Terminal executable (auto)", "file", "Terminal"),
            (
                "hold_on_exit",
                "Hold terminal on exit",
                "combo",
                "Terminal",
                ["", "never", "error", "always"],
            ),
            (
                "scp_app",
                "File transfer application",
                "combo",
                "File transfer",
                ["", *TRANSFER_APPS],
            ),
            ("winscp_path", "WinSCP executable (auto)", "file", "File transfer"),
            ("filezilla_path", "FileZilla executable (auto)", "file", "File transfer"),
            ("scp_command", "Custom command", "entry", "File transfer"),
            (
                "winscp_protocol",
                "WinSCP protocol",
                "combo",
                "File transfer",
                ["", "sftp", "scp"],
            ),
            (
                "close_action",
                "Close button",
                "combo",
                "General",
                ["", "exit", "hide"],
            ),
            ("font", "Tree font (e.g. Segoe UI 9)", "entry", "General"),
        ]

        def __init__(self, parent, defaults: dict):
            super().__init__(parent)
            self.title("Settings")
            self.transient(parent)
            self.result = None
            self.vars: dict = {}
            outer = ttk.Frame(self, padding=10)
            outer.pack(fill="both", expand=True)
            nb = ttk.Notebook(outer)
            nb.pack(fill="both", expand=True)
            pages: dict = {}
            rows: dict = {}
            for tab in self.TABS:
                page = ttk.Frame(nb, padding=10)
                page.columnconfigure(1, weight=1)
                nb.add(page, text=tab)
                pages[tab], rows[tab] = page, 0
                if tab in self.TAB_HELP:
                    ttk.Label(
                        page, text=self.TAB_HELP[tab], justify="left", wraplength=520
                    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
                    rows[tab] = 1
            for spec in self.FIELDS:
                key, label, kind, tab = spec[:4]
                body, row = pages[tab], rows[tab]
                rows[tab] = row + 1
                ttk.Label(body, text=label).grid(
                    row=row, column=0, sticky="w", pady=2, padx=(0, 8)
                )
                value = defaults.get(key)
                if isinstance(value, list):
                    value = " ".join(str(v) for v in value)
                var = tk.StringVar(value="" if value is None else str(value))
                self.vars[key] = var
                if kind == "combo":
                    ttk.Combobox(
                        body,
                        textvariable=var,
                        values=spec[4],
                        state="readonly",
                        width=14,
                    ).grid(row=row, column=1, sticky="w", pady=2)
                else:
                    ttk.Entry(body, textvariable=var, width=46).grid(
                        row=row, column=1, sticky="ew", pady=2
                    )
                    if kind == "file":
                        ttk.Button(
                            body,
                            text="...",
                            width=3,
                            command=lambda v=var: self._browse(v),
                        ).grid(row=row, column=2, padx=(4, 0))
            buttons = ttk.Frame(outer)
            buttons.pack(fill="x", pady=(10, 0))
            ttk.Label(buttons, text="Empty fields use the built-in default.").pack(
                side="left"
            )
            ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
            ttk.Button(buttons, text="OK", command=self._ok).pack(side="right", padx=4)
            self.bind("<Escape>", lambda e: self.destroy())
            self.bind("<Control-Return>", lambda e: self._ok())
            self.minsize(560, 320)
            self.update_idletasks()
            self.geometry(f"+{parent.winfo_rootx() + 40}+{parent.winfo_rooty() + 40}")
            self.grab_set()
            self.wait_window(self)

        def _browse(self, var):
            path = filedialog.askopenfilename(
                parent=self, title="Choose program or file"
            )
            if path:
                var.set(path.replace("\\", "/"))

        def _ok(self):
            self.result = {k: v.get().strip() for k, v in self.vars.items()}
            self.destroy()

    def ask_choice(parent, title: str, text: str, choices: list[tuple[str, str]]):
        """A small dialog with one button per (key, label); returns the key."""
        dlg = tk.Toplevel(parent)
        dlg.title(title)
        dlg.transient(parent)
        picked = []
        ttk.Label(dlg, text=text, justify="left", wraplength=440, padding=12).pack()
        row = ttk.Frame(dlg, padding=(12, 0, 12, 12))
        row.pack(fill="x")
        for key, label in choices:
            ttk.Button(
                row, text=label, command=lambda k=key: (picked.append(k), dlg.destroy())
            ).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 80}")
        dlg.grab_set()
        parent.wait_window(dlg)
        return picked[0] if picked else None

    class AboutDialog(tk.Toplevel):
        """Name, version, links, and a neat table of the machine facts."""

        URL = "https://github.com/larsborn/sshtree"

        def __init__(self, parent, rows: list[tuple[str, str]], icon):
            super().__init__(parent)
            self.title(f"About {APP_NAME}")
            self.resizable(False, False)
            self.transient(parent)
            outer = ttk.Frame(self, padding=(16, 14, 16, 12))
            outer.pack(fill="both", expand=True)

            head = ttk.Frame(outer)
            head.pack(fill="x")
            ttk.Label(head, image=icon).pack(side="left", padx=(0, 12))
            titles = ttk.Frame(head)
            titles.pack(side="left", fill="x", expand=True)
            from tkinter import font as tkfont

            big = tkfont.nametofont("TkDefaultFont").copy()
            big.configure(size=big.cget("size") + 5, weight="bold")
            ttk.Label(titles, text=APP_NAME, font=big).pack(anchor="w")
            ttk.Label(titles, text=f"Version {__version__}  ·  MIT License").pack(
                anchor="w"
            )
            link = ttk.Label(
                titles, text=self.URL, foreground="#1a5fb4", cursor="hand2"
            )
            link.pack(anchor="w", pady=(2, 0))
            link.bind("<Button-1>", lambda e: open_with_default_app(self.URL))
            ttk.Label(
                outer,
                text="A folder tree of SSH sessions. Any terminal. No stored passwords.",
            ).pack(anchor="w", pady=(10, 8))

            ttk.Separator(outer).pack(fill="x", pady=(0, 8))
            grid = ttk.Frame(outer)
            grid.pack(fill="x")
            grid.columnconfigure(1, weight=1)
            self.details = rows
            for r, (label, value) in enumerate(rows):
                ttk.Label(grid, text=label, foreground="#555555").grid(
                    row=r, column=0, sticky="nw", padx=(0, 14), pady=1
                )
                # read-only entries: long paths stay on one line and can be copied
                ent = ttk.Entry(grid, width=64)
                ent.insert(0, value)
                ent.configure(state="readonly")
                ent.grid(row=r, column=1, sticky="ew", pady=1)

            buttons = ttk.Frame(outer)
            buttons.pack(fill="x", pady=(12, 0))
            ttk.Button(buttons, text="Copy details", command=self._copy).pack(
                side="left"
            )
            ttk.Button(
                buttons,
                text="Open config folder",
                command=lambda: open_with_default_app(Path(rows[0][1]).parent),
            ).pack(side="left", padx=(6, 0))
            ok = ttk.Button(buttons, text="OK", command=self.destroy)
            ok.pack(side="right")
            ok.focus_set()
            self.bind("<Escape>", lambda e: self.destroy())
            self.bind("<Return>", lambda e: self.destroy())
            self.update_idletasks()
            self.geometry(f"+{parent.winfo_rootx() + 30}+{parent.winfo_rooty() + 60}")
            self.grab_set()

        def _copy(self):
            text = f"{APP_NAME} {__version__}\n" + "\n".join(
                f"{k}: {v}" for k, v in self.details
            )
            self.clipboard_clear()
            self.clipboard_append(text)

    class App(tk.Tk):
        ROOT_LABEL = "(top level)"

        def __init__(self, cfg: Config):
            super().__init__()
            self.cfg = cfg
            self.state_path = cfg.path.with_name("state.json")
            self.ui = self._load_state()
            self.title(APP_NAME)
            self.geometry(self.ui.get("geometry", "360x720"))
            self.minsize(240, 200)

            self.icons = {
                "folder": pixmap(FOLDER_PX, PALETTE),
                "folder_open": pixmap(FOLDER_OPEN_PX, PALETTE),
                "session": pixmap(SESSION_PX, PALETTE),
                "tunnel": pixmap(SESSION_PX, {**PALETTE, "g": PALETTE["t"]}),
                "session_big": pixmap(SESSION_PX, PALETTE, pad=0).zoom(3, 3),
            }
            self.iconphoto(True, self.icons["session"])

            self.sort_var = tk.StringVar(value=self.ui.get("sort", "name"))
            self.locked_var = tk.BooleanVar(value=bool(self.ui.get("locked", False)))
            self.filter_var = tk.StringVar()
            self.filter_var.trace_add("write", lambda *_: self.rebuild())

            # compact tree like PuTTY Session Manager: small font, tight rows.
            # defaults.font overrides, e.g. "Segoe UI 9" or "Tahoma 8".
            from tkinter import font as tkfont

            spec = str(cfg.defaults.get("font") or "").strip()
            if spec:
                family, _, size = spec.rpartition(" ")
                tree_font = tkfont.Font(
                    family=family or spec,
                    size=int(size) if size.lstrip("-").isdigit() else 9,
                )
            else:
                tree_font = tkfont.nametofont("TkDefaultFont").copy()
                tree_font.configure(size=8)
            self.tree_font = tree_font
            row_height = tree_font.metrics("linespace") + 4
            style = ttk.Style(self)
            style.configure(
                "Treeview", font=tree_font, rowheight=max(row_height, 18), indent=18
            )

            # the filter bar stays hidden until Ctrl+F or typing in the tree
            self.filter_bar = ttk.Frame(self, padding=(4, 4, 4, 0))
            ttk.Label(self.filter_bar, text="Filter:").pack(side="left")
            self.filter_entry = ttk.Entry(self.filter_bar, textvariable=self.filter_var)
            self.filter_entry.pack(side="left", fill="x", expand=True, padx=4)

            frame = ttk.Frame(self, padding=(4, 4, 4, 2))
            frame.pack(fill="both", expand=True)
            self.tree_frame = frame
            self.tree = ttk.Treeview(frame, show="tree", selectmode="browse")
            vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
            self.vsb = vsb

            def on_yscroll(first, last):
                # auto-hide: the scrollbar only appears when the tree overflows
                vsb.set(first, last)
                needed = not (float(first) <= 0.0 and float(last) >= 1.0)
                if needed and not vsb.winfo_manager():
                    vsb.pack(side="right", fill="y")
                elif not needed and vsb.winfo_manager():
                    vsb.pack_forget()

            self.tree.configure(yscrollcommand=on_yscroll)
            self.tree.pack(side="left", fill="both", expand=True)

            self.status_var = tk.StringVar()
            # flat status bar: a thin separator line instead of a bevel
            ttk.Separator(self, orient="horizontal").pack(fill="x", side="bottom")
            status = ttk.Label(
                self,
                textvariable=self.status_var,
                anchor="w",
                relief="flat",
                padding=(6, 0),
            )
            status.pack(fill="x", side="bottom")

            self.nodes: dict[str, dict] = {}  # iid -> node
            self.parents: dict[str, list] = {}  # iid -> list containing node
            self.folder_iids: dict[str, tuple] = {}  # iid -> folder path tuple
            self._drag_src = None
            self._drag_start = None
            self._dragging = False
            self._last_filter = ""

            self._build_menu()
            self._bind_keys()
            self.rebuild()
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.after(500, self._poll_config)

            self.events: queue.Queue = queue.Queue()
            self.instance: InstanceServer | None = None
            self.close_action = str(cfg.defaults.get("close_action") or "exit").lower()
            notes = self._start_background_services()
            self.after(100, self._poll_events)
            self._foreground_retries = 0
            if IS_WINDOWS and self.instance:
                self.update_idletasks()
                self.instance.update(hwnd=self._root_hwnd())

            if notes:
                self.set_status("; ".join(notes))
            elif cfg.created:
                self.set_status(f"Created example config at {cfg.path}")
            else:
                self.set_status(
                    f"{len(list(walk_sessions(cfg.tree, [])))} sessions from {cfg.path}"
                )

        # -- single instance, window visibility -----------------------------

        def _start_background_services(self) -> list[str]:
            """Start the activation socket; return problems."""
            notes: list[str] = []
            try:
                self.instance = InstanceServer(
                    self.cfg.path.with_name("instance.json"), self.events.put
                )
            except OSError as exc:
                notes.append(f"single-instance socket unavailable: {exc}")
            return notes

        def _poll_events(self):
            try:
                while True:
                    command = self.events.get_nowait()
                    if command == "show":
                        self.show_window()
                    elif command == "hide":
                        self.hide_window()
                    elif command == "quit":
                        self.quit_app()
            except queue.Empty:
                pass
            self.after(100, self._poll_events)

        def _root_hwnd(self) -> int:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            return user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()  # GA_ROOT

        def show_window(self):
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.update_idletasks()
            if IS_WINDOWS:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32")
                hwnd = self._root_hwnd()
                user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                self._fg_log("show_window")
                if not force_foreground_windows(hwnd, self._fg_log):
                    # the launcher may still be busy hiding its own window;
                    # keep trying for a while from the event loop
                    self._foreground_retries = 15
                    self.after(100, self._retry_foreground)
            self.after(50, lambda: self.attributes("-topmost", False))
            self.focus_force()
            self.focus_main()

        def focus_main(self):
            """Restore the widget that last had the keyboard focus inside the
            window (the highlighted tree entry, or the filter if the user was
            typing there); on the first show, start in the tree."""
            last = getattr(self, "_last_focus", None)
            if last is self.filter_entry and self.filter_bar.winfo_manager():
                self.filter_entry.focus_set()
                self.filter_entry.icursor("end")
                return
            self.tree.focus_set()
            if not self.tree.focus() and self.tree.get_children(""):
                first = self.tree.get_children("")[0]
                self.tree.focus(first)
                self.tree.selection_set(first)

        def _fg_log(self, message: str):
            """Append to foreground.log next to the config (diagnosing launchers)."""
            try:
                import datetime

                with open(
                    self.cfg.path.with_name("foreground.log"), "a", encoding="utf-8"
                ) as fh:
                    fh.write(f"{datetime.datetime.now():%H:%M:%S.%f} {message}\n")
            except OSError:
                pass

        def _retry_foreground(self):
            if self._foreground_retries <= 0 or self.state() != "normal":
                return
            self._foreground_retries -= 1
            self._fg_log(f"retry {self._foreground_retries} left")
            if force_foreground_windows(self._root_hwnd(), self._fg_log):
                self._fg_log("retry succeeded")
                self.focus_force()
                self.focus_main()
                return
            if self._foreground_retries == 0:
                self._fg_log("giving up")
                self.set_status("Windows refused to bring the window to the front")
            else:
                self.after(100, self._retry_foreground)

        def hide_window(self):
            self._save_state()
            if self.close_action == "hide":
                self.withdraw()
            else:
                self.iconify()

        def quit_app(self):
            self._save_state()
            if self.instance:
                self.instance.close()
            self.destroy()

        # -- persistence of UI state ---------------------------------------

        def _load_state(self) -> dict:
            try:
                with open(self.state_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

        def _save_state(self) -> None:
            self.ui["geometry"] = self.geometry()
            self.ui["sort"] = self.sort_var.get()
            self.ui["locked"] = bool(self.locked_var.get())
            self._capture_expanded()
            try:
                atomic_write_json(self.state_path, self.ui)
            except OSError:
                pass

        def on_close(self):
            """Window close button: hide when close_action is 'hide', else exit."""
            if self.close_action == "hide":
                self.hide_window()
            else:
                self.quit_app()

        def _poll_config(self):
            if self.cfg.changed_on_disk():
                self.refresh()
            self.after(1500, self._poll_config)

        # -- menu & keys ---------------------------------------------------

        def _build_menu(self):
            # Context menus are built per click (session / folder / empty
            # space), so every entry shown is one that applies; see
            # build_context_menu().
            self.bind("<Control-q>", lambda e: self.quit_app())
            button = "<Button-2>" if IS_MAC else "<Button-3>"
            self.tree.bind(button, self.popup_menu)
            if IS_MAC:
                self.tree.bind("<Control-Button-1>", self.popup_menu)

        def build_context_menu(self, kind: str) -> tk.Menu:
            """`kind` is 'session', 'folder' or 'background'."""
            m = tk.Menu(self, tearoff=0)
            locked = bool(self.locked_var.get())
            if kind == "session":
                m.add_command(
                    label="Launch Session",
                    command=self.launch_selected,
                    accelerator="Enter",
                )
                label = self.transfer_label()
                if label:
                    m.add_command(
                        label=label,
                        command=self.launch_transfer_selected,
                        accelerator="Ctrl+W",
                    )
                m.add_command(
                    label="Copy SSH Command",
                    command=self.copy_command,
                    accelerator="Ctrl+C",
                )
                if not locked:
                    m.add_separator()
                    m.add_command(
                        label="Edit Session...",
                        command=self.edit_session,
                        accelerator="Ctrl+E",
                    )
                    m.add_command(
                        label="Duplicate Session...", command=self.duplicate_session
                    )
                    m.add_command(
                        label="Move To Folder...", command=self.move_to_folder
                    )
                    m.add_command(
                        label="Delete Session",
                        command=self.delete_node,
                        accelerator="Del",
                    )
                    m.add_separator()
                    m.add_command(
                        label="New Session Here...",
                        command=self.new_session,
                        accelerator="Ctrl+N",
                    )
            elif kind == "folder":
                m.add_command(
                    label="Launch All Sessions",
                    command=lambda: self.launch_folder(False),
                )
                m.add_command(
                    label="Launch All Including Subfolders",
                    command=lambda: self.launch_folder(True),
                )
                if not locked:
                    m.add_separator()
                    m.add_command(
                        label="New Session Here...",
                        command=self.new_session,
                        accelerator="Ctrl+N",
                    )
                    m.add_command(label="New Subfolder...", command=self.new_folder)
                    m.add_separator()
                    m.add_command(
                        label="Rename Folder...",
                        command=self.rename_folder,
                        accelerator="F2",
                    )
                    m.add_command(
                        label="Move Folder To...", command=self.move_to_folder
                    )
                    m.add_command(
                        label="Delete Folder",
                        command=self.delete_node,
                        accelerator="Del",
                    )
            else:
                if not locked:
                    m.add_command(
                        label="New Session...",
                        command=self.new_session,
                        accelerator="Ctrl+N",
                    )
                    m.add_command(label="New Folder...", command=self.new_folder)
                    m.add_separator()
                self._add_app_items(m)
                return m
            m.add_separator()
            app_menu = tk.Menu(m, tearoff=0)
            self._add_app_items(app_menu)
            m.add_cascade(label=APP_NAME, menu=app_menu)
            return m

        def _add_app_items(self, m: tk.Menu):
            """Tree-wide and application items: shown flat on empty space and
            as a submenu on sessions and folders."""
            m.add_command(
                label="Refresh Sessions", command=self.refresh, accelerator="F5"
            )
            m.add_command(
                label="Expand Tree",
                command=lambda: self.set_all_open(True),
                accelerator="Ctrl+Right",
            )
            m.add_command(
                label="Collapse Tree",
                command=lambda: self.set_all_open(False),
                accelerator="Ctrl+Left",
            )
            sort = tk.Menu(m, tearoff=0)
            for label, value in (
                ("Name", "name"),
                ("Host", "host"),
                ("Config order", "none"),
            ):
                sort.add_radiobutton(
                    label=label,
                    value=value,
                    variable=self.sort_var,
                    command=self.on_sort_change,
                )
            m.add_cascade(label="Sort By", menu=sort)
            m.add_checkbutton(
                label="Lock Session Tree",
                variable=self.locked_var,
                command=self.on_lock_change,
            )
            m.add_separator()
            m.add_command(label="Settings...", command=self.open_settings)
            if IS_WINDOWS and not self.locked_var.get():
                m.add_command(
                    label="Import PuTTY Sessions...", command=self.import_putty
                )
            m.add_command(
                label="Edit Config File",
                command=lambda: open_with_default_app(self.cfg.path),
            )
            m.add_command(
                label="Open Config Folder",
                command=lambda: open_with_default_app(self.cfg.path.parent),
            )
            m.add_separator()
            m.add_command(label="About", command=self.about)
            m.add_command(label="Exit", command=self.quit_app, accelerator="Ctrl+Q")

        def _bind_keys(self):
            t = self.tree
            t.bind("<Double-1>", self.on_double_click)
            t.bind("<Return>", lambda e: self.launch_selected())
            t.bind("<<TreeviewSelect>>", self.on_select)
            t.bind("<ButtonPress-1>", self._dnd_press)
            t.bind("<B1-Motion>", self._dnd_motion)
            t.bind("<ButtonRelease-1>", self._dnd_release)
            t.bind("<<TreeviewOpen>>", lambda e: self._update_folder_icon(True))
            t.bind("<<TreeviewClose>>", lambda e: self._update_folder_icon(False))
            self.bind("<F5>", lambda e: self.refresh())
            self.bind("<F2>", lambda e: self.rename_folder())
            self.bind("<Control-Right>", lambda e: self.set_all_open(True))
            self.bind("<Control-Left>", lambda e: self.set_all_open(False))
            self.bind("<Control-n>", lambda e: self.new_session())
            self.bind("<Control-e>", lambda e: self.edit_session())
            self.bind("<Control-w>", lambda e: self.launch_transfer_selected())
            self.bind("<Control-f>", lambda e: self.show_filter())
            self.filter_entry.bind("<Return>", lambda e: self.focus_first_session())
            self.filter_entry.bind("<Down>", lambda e: self.focus_first_session())
            self.bind("<Escape>", lambda e: self.on_escape())
            t.bind("<KeyPress>", self.on_tree_keypress)
            # Tk forgets the last focused widget across withdraw/deiconify,
            # so remember it ourselves for focus_main()
            for widget in (t, self.filter_entry):
                widget.bind(
                    "<FocusIn>", lambda e, w=widget: setattr(self, "_last_focus", w)
                )
            t.bind("<Delete>", lambda e: self.delete_node())
            t.bind("<Control-c>", lambda e: self.copy_command())
            t.bind("<Home>", lambda e: self.select_edge(first=True))
            t.bind("<End>", lambda e: self.select_edge(first=False))

        def show_filter(self, initial: str = ""):
            """Open the filter bar (Ctrl+F or typing in the tree) and focus it."""
            if not self.filter_bar.winfo_manager():
                self.filter_bar.pack(fill="x", before=self.tree_frame)
            if initial:
                self.filter_var.set(initial)
                self.filter_entry.selection_clear()
                self.filter_entry.icursor("end")
            else:
                # Ctrl+F on an existing filter: select it, so typing replaces it
                self.filter_entry.selection_range(0, "end")
                self.filter_entry.icursor("end")
            self.filter_entry.focus_set()
            return "break"

        def hide_filter(self):
            self.filter_var.set("")
            if self.filter_bar.winfo_manager():
                self.filter_bar.pack_forget()
            self.tree.focus_set()

        def on_tree_keypress(self, event):
            """Typing a printable character in the tree starts a filter."""
            ch = event.char
            if not ch or not ch.isprintable() or ch.isspace():
                return None
            if event.state & 0x4 or event.state & 0x20000:  # Control / Alt held
                return None
            self.show_filter(ch)
            return "break"

        def on_escape(self):
            """Escape: clear the filter text, then hide the filter bar, then close
            the window (hidden or quit, like the close button)."""
            if self.filter_var.get():
                self.filter_var.set("")
                self.filter_entry.focus_set()
            elif self.filter_bar.winfo_manager():
                self.hide_filter()
            else:
                self.on_close()

        def first_session_iid(self, parent=""):
            """First session (not folder) in display order, descending into folders."""
            for iid in self.tree.get_children(parent):
                if iid not in self.folder_iids:
                    return iid
                hit = self.first_session_iid(iid)
                if hit:
                    return hit
            return None

        def focus_first_session(self):
            """Enter in the filter box: select the first match and move to the tree,
            so that a second Enter launches it."""
            iid = self.first_session_iid()
            if iid:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
                self.on_select()
                self.tree.focus_set()
            else:
                self.bell()
            return "break"

        def select_edge(self, first: bool):
            """Home/End: jump to the first or the last visible row."""
            items = self.tree.get_children("")
            if not items:
                return "break"
            iid = items[0] if first else items[-1]
            if not first:  # descend into open folders to the last visible row
                while self._is_open(iid) and self.tree.get_children(iid):
                    iid = self.tree.get_children(iid)[-1]
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            return "break"

        # -- tree building -------------------------------------------------

        def _is_open(self, iid) -> bool:
            return str(self.tree.item(iid, "open")) in ("1", "true", "True")

        def _sorted(self, nodes: list) -> list:
            mode = self.sort_var.get()
            folders = [n for n in nodes if is_folder(n)]
            sessions = [n for n in nodes if not is_folder(n) and isinstance(n, dict)]
            if mode == "name":
                key = lambda n: str(n.get("name", "")).lower()
                folders.sort(key=key)
                sessions.sort(key=key)
            elif mode == "host":
                folders.sort(key=lambda n: str(n.get("name", "")).lower())
                sessions.sort(
                    key=lambda n: (
                        str(n.get("host", "")).lower(),
                        str(n.get("name", "")).lower(),
                    )
                )
            return folders + sessions

        def _matches(self, node: dict, needle: str) -> bool:
            hay = " ".join(
                str(node.get(k, "")) for k in ("name", "host", "user")
            ).lower()
            return needle in hay

        def _subtree_matches(self, node: dict, needle: str) -> bool:
            if is_folder(node):
                return any(self._subtree_matches(c, needle) for c in node["children"])
            return self._matches(node, needle)

        def _capture_expanded(self):
            """Remember which folders are open in the widget (unfiltered view only)."""
            if self.folder_iids and not self._last_filter:
                self.ui["expanded"] = [
                    list(p) for iid, p in self.folder_iids.items() if self._is_open(iid)
                ]

        def rebuild(self, open_paths=(), capture=True):
            """Rebuild the widget from the config.

            `open_paths` are folder paths to show expanded (with their parents).
            """
            if capture:
                self._capture_expanded()
            expanded = {tuple(p) for p in self.ui.get("expanded", [])}
            for path in open_paths:
                path = tuple(path)
                expanded.update(path[:i] for i in range(1, len(path) + 1))
            self.ui["expanded"] = [list(p) for p in expanded]
            selected_path = self.selected_path()
            self.tree.delete(*self.tree.get_children(""))
            self.nodes.clear()
            self.parents.clear()
            self.folder_iids.clear()
            needle = self.filter_var.get().strip().lower()
            self._last_filter = needle
            self._insert(self.cfg.tree, "", (), needle, expanded)
            if selected_path:
                self.select_path(selected_path)

        def _insert(self, nodes, parent_iid, path, needle, expanded):
            for node in self._sorted(nodes):
                name = str(node.get("name", "?"))
                if is_folder(node):
                    if needle and not self._subtree_matches(node, needle):
                        continue
                    fpath = path + (name,)
                    is_open = bool(needle) or fpath in expanded
                    iid = self.tree.insert(
                        parent_iid,
                        "end",
                        text=name,
                        open=is_open,
                        image=self.icons["folder_open" if is_open else "folder"],
                    )
                    self.nodes[iid] = node
                    self.parents[iid] = nodes
                    self.folder_iids[iid] = fpath
                    self._insert(node["children"], iid, fpath, needle, expanded)
                else:
                    if needle and not self._matches(node, needle):
                        continue
                    icon = self.icons[
                        "tunnel" if truthy(node.get("no_shell")) else "session"
                    ]
                    iid = self.tree.insert(parent_iid, "end", text=name, image=icon)
                    self.nodes[iid] = node
                    self.parents[iid] = nodes

        def _update_folder_icon(self, is_open: bool):
            iid = self.tree.focus()
            if iid in self.folder_iids:
                self.tree.item(
                    iid, image=self.icons["folder_open" if is_open else "folder"]
                )

        def set_all_open(self, value: bool):
            for iid in self.folder_iids:
                self.tree.item(
                    iid,
                    open=value,
                    image=self.icons["folder_open" if value else "folder"],
                )

        # -- selection helpers ---------------------------------------------

        def selected_iid(self):
            sel = self.tree.selection()
            return sel[0] if sel else None

        def selected_node(self):
            iid = self.selected_iid()
            return self.nodes.get(iid) if iid else None

        def node_path(self, iid) -> tuple:
            parts = []
            while iid:
                parts.append(self.tree.item(iid, "text"))
                iid = self.tree.parent(iid)
            return tuple(reversed(parts))

        def selected_path(self):
            iid = self.selected_iid()
            return self.node_path(iid) if iid else None

        def select_path(self, path: tuple):
            iid = ""
            for name in path:
                nxt = None
                for child in self.tree.get_children(iid):
                    if self.tree.item(child, "text") == name:
                        nxt = child
                        break
                if nxt is None:
                    return
                iid = nxt
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

        def folder_of(self, iid) -> tuple:
            """Path of the folder that contains iid (or iid itself if a folder)."""
            if iid in self.folder_iids:
                return self.folder_iids[iid]
            parent = self.tree.parent(iid)
            return self.folder_iids.get(parent, ()) if parent else ()

        def ancestors_of(self, iid) -> list:
            out = []
            parent = self.tree.parent(iid)
            while parent:
                out.append(self.nodes[parent])
                parent = self.tree.parent(parent)
            return list(reversed(out))

        def folder_node_by_path(self, path: tuple):
            """Return (children_list, folder_node_or_None) for a folder path."""
            nodes = self.cfg.tree
            folder = None
            for name in path:
                nxt = next(
                    (n for n in nodes if is_folder(n) and n.get("name") == name), None
                )
                if nxt is None:
                    raise KeyError("/".join(path))
                folder, nodes = nxt, nxt["children"]
            return nodes, folder

        def folder_choices(self) -> list[str]:
            return [self.ROOT_LABEL] + [
                "/".join(p) for _f, p in walk_folders(self.cfg.tree)
            ]

        def settings_for(self, iid) -> dict:
            node = self.nodes[iid]
            return effective_settings(self.cfg, node, self.ancestors_of(iid))

        def set_status(self, text: str):
            self.status_var.set(text)

        def locked(self) -> bool:
            if self.locked_var.get():
                self.set_status(
                    "Session tree is locked - unlock it from the context menu"
                )
                self.bell()
                return True
            return False

        def save(self):
            self.cfg.save()
            self.rebuild()

        # -- events --------------------------------------------------------

        def popup_menu(self, event):
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                kind = "folder" if iid in self.folder_iids else "session"
            else:
                self.tree.selection_remove(*self.tree.selection())
                kind = "background"
            menu = self.build_context_menu(kind)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        def on_select(self, _event=None):
            iid = self.selected_iid()
            if not iid or iid in self.folder_iids:
                return
            try:
                cmd = build_session_command(self.settings_for(iid))
                self.set_status(cmdline_string(cmd))
            except Exception as exc:  # noqa: BLE001
                self.set_status(f"error: {exc}")

        def on_double_click(self, event):
            iid = self.tree.identify_row(event.y)
            if iid and iid not in self.folder_iids:
                self.launch_iid(iid)
                return "break"
            return None

        def on_sort_change(self):
            self.ui["sort"] = self.sort_var.get()
            self.rebuild()

        def on_lock_change(self):
            self.ui["locked"] = bool(self.locked_var.get())
            self.title(APP_NAME + (" [locked]" if self.locked_var.get() else ""))

        # -- drag & drop ---------------------------------------------------

        def _dnd_press(self, event):
            self._drag_src = self.tree.identify_row(event.y)
            self._drag_start = (event.x, event.y)
            self._dragging = False

        def _dnd_motion(self, event):
            if not self._drag_src or self.locked_var.get() or self.filter_var.get():
                return
            dx = abs(event.x - self._drag_start[0])
            dy = abs(event.y - self._drag_start[1])
            if not self._dragging and (dx > 6 or dy > 6):
                self._dragging = True
                self.tree.configure(cursor="hand2")
            if self._dragging:
                target = self.tree.identify_row(event.y)
                self.set_status(
                    f"Move to: {'/'.join(self.folder_of(target)) or self.ROOT_LABEL}"
                    if target
                    else f"Move to: {self.ROOT_LABEL}"
                )

        def _dnd_release(self, event):
            src, dragging = self._drag_src, self._dragging
            self._drag_src = self._drag_start = None
            self._dragging = False
            self.tree.configure(cursor="")
            if not dragging or not src:
                return
            target = self.tree.identify_row(event.y)
            dest = self.folder_of(target) if target else ()
            self._move(src, dest)

        def _move(self, iid, dest_path: tuple):
            node = self.nodes[iid]
            src_list = self.parents[iid]
            if iid in self.folder_iids:
                own = self.folder_iids[iid]
                if dest_path[: len(own)] == own:
                    self.set_status("Cannot move a folder into itself")
                    return
            dest_list, _ = self.folder_node_by_path(dest_path)
            if dest_list is src_list:
                return
            if any(
                n is not node
                and n.get("name") == node.get("name")
                and is_folder(n) == is_folder(node)
                for n in dest_list
            ):
                messagebox.showerror(
                    APP_NAME,
                    f"'{node.get('name')}' already exists in that folder",
                    parent=self,
                )
                return
            src_list.remove(node)
            dest_list.append(node)
            self.cfg.save()
            self.rebuild(open_paths=[dest_path])
            self.select_path(dest_path + (node.get("name"),))
            self.set_status(
                f"Moved '{node.get('name')}' to {'/'.join(dest_path) or self.ROOT_LABEL}"
            )

        # -- actions -------------------------------------------------------

        def refresh(self):
            try:
                self.cfg.load()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    APP_NAME, f"Could not reload {self.cfg.path}:\n{exc}", parent=self
                )
                return
            self.rebuild()
            self.set_status(f"Reloaded {self.cfg.path}")

        def launch_iid(self, iid):
            try:
                argv = launch_session(self.settings_for(iid))
                self.set_status("Launched: " + cmdline_string(argv))
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    APP_NAME, f"Could not launch session:\n{exc}", parent=self
                )

        def launch_selected(self):
            iid = self.selected_iid()
            if iid and iid not in self.folder_iids:
                self.launch_iid(iid)

        def launch_folder(self, recursive: bool):
            iid = self.selected_iid()
            if not iid or iid not in self.folder_iids:
                return
            count = 0
            for child in self._descendants(iid, recursive):
                if child not in self.folder_iids:
                    self.launch_iid(child)
                    count += 1
            self.set_status(f"Launched {count} session(s)")

        def _descendants(self, iid, recursive):
            for child in self.tree.get_children(iid):
                yield child
                if recursive and child in self.folder_iids:
                    yield from self._descendants(child, True)

        def launch_transfer_selected(self):
            iid = self.selected_iid()
            if not iid or iid in self.folder_iids:
                return
            try:
                argv = launch_file_transfer(self.settings_for(iid))
                self.set_status("Launched: " + cmdline_string(argv))
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    APP_NAME,
                    f"Could not open the file transfer app:\n{exc}",
                    parent=self,
                )

        def transfer_label(self) -> str | None:
            """Menu label for the file transfer item, None when disabled."""
            return TRANSFER_LABELS.get(transfer_app_of(self.cfg.defaults))

        def copy_command(self):
            iid = self.selected_iid()
            if not iid or iid in self.folder_iids:
                return
            try:
                line = cmdline_string(build_session_command(self.settings_for(iid)))
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP_NAME, str(exc), parent=self)
                return
            self.clipboard_clear()
            self.clipboard_append(line)
            self.set_status("Copied: " + line)

        def _current_folder_label(self) -> str:
            iid = self.selected_iid()
            return (
                "/".join(self.folder_of(iid)) or self.ROOT_LABEL
                if iid
                else self.ROOT_LABEL
            )

        def _apply_session_dialog(self, result, node=None, src_list=None):
            folder_label, values = result
            path = (
                ()
                if folder_label == self.ROOT_LABEL
                else tuple(folder_label.split("/"))
            )
            dest_list, _ = self.folder_node_by_path(path)
            clash = any(
                n is not node and not is_folder(n) and n.get("name") == values["name"]
                for n in dest_list
            )
            if clash:
                messagebox.showerror(
                    APP_NAME,
                    f"A session named '{values['name']}' already exists there",
                    parent=self,
                )
                return False
            if node is None:
                dest_list.append(values)
            else:
                for key in [k for k in node if k not in ("children",)]:
                    if key not in values and key in {
                        f[0] for f in SessionDialog.FIELDS
                    }:
                        del node[key]
                node.update(values)
                if src_list is not dest_list:
                    src_list.remove(node)
                    dest_list.append(node)
            self.cfg.save()
            self.rebuild(open_paths=[path])
            self.select_path(path + (values["name"],))
            return True

        def new_session(self):
            if self.locked():
                return
            dlg = SessionDialog(
                self,
                "New Session",
                None,
                self.folder_choices(),
                self._current_folder_label(),
            )
            if dlg.result:
                self._apply_session_dialog(dlg.result)

        def edit_session(self):
            iid = self.selected_iid()
            if not iid or iid in self.folder_iids or self.locked():
                return
            node = self.nodes[iid]
            folder = "/".join(self.folder_of(iid)) or self.ROOT_LABEL
            dlg = SessionDialog(
                self,
                f"Edit Session - {node.get('name')}",
                node,
                self.folder_choices(),
                folder,
            )
            if dlg.result:
                self._apply_session_dialog(dlg.result, node, self.parents[iid])

        def duplicate_session(self):
            iid = self.selected_iid()
            if not iid or iid in self.folder_iids or self.locked():
                return
            node = json.loads(json.dumps(self.nodes[iid]))
            node["name"] = f"{node.get('name', '')} (copy)"
            folder = "/".join(self.folder_of(iid)) or self.ROOT_LABEL
            dlg = SessionDialog(
                self, "Duplicate Session", node, self.folder_choices(), folder
            )
            if dlg.result:
                self._apply_session_dialog(dlg.result)

        def move_to_folder(self):
            iid = self.selected_iid()
            if not iid or self.locked():
                return
            choices = self.folder_choices()
            dlg = tk.Toplevel(self)
            dlg.title("Move To Folder")
            dlg.transient(self)
            lb = tk.Listbox(dlg, height=min(20, max(5, len(choices))), width=40)
            for c in choices:
                lb.insert("end", c)
            lb.pack(fill="both", expand=True, padx=8, pady=8)
            lb.selection_set(0)
            chosen = []

            def ok(_e=None):
                sel = lb.curselection()
                if sel:
                    chosen.append(choices[sel[0]])
                dlg.destroy()

            ttk.Button(dlg, text="Move", command=ok).pack(pady=(0, 8))
            lb.bind("<Double-1>", ok)
            lb.bind("<Return>", ok)
            dlg.bind("<Escape>", lambda e: dlg.destroy())
            dlg.grab_set()
            lb.focus_set()
            self.wait_window(dlg)
            if chosen:
                label = chosen[0]
                self._move(
                    iid, () if label == self.ROOT_LABEL else tuple(label.split("/"))
                )

        def delete_node(self):
            iid = self.selected_iid()
            if not iid or self.locked():
                return
            node = self.nodes[iid]
            kind = "folder (including its contents)" if is_folder(node) else "session"
            if not messagebox.askyesno(
                APP_NAME, f"Delete {kind} '{node.get('name')}'?", parent=self
            ):
                return
            self.parents[iid].remove(node)
            self.save()
            self.set_status(f"Deleted '{node.get('name')}'")

        def new_folder(self):
            if self.locked():
                return
            name = simpledialog.askstring(APP_NAME, "Folder name:", parent=self)
            if not name:
                return
            name = name.strip()
            iid = self.selected_iid()
            path = self.folder_of(iid) if iid else ()
            dest, _ = self.folder_node_by_path(path)
            if any(is_folder(n) and n.get("name") == name for n in dest):
                messagebox.showerror(
                    APP_NAME, f"Folder '{name}' already exists there", parent=self
                )
                return
            dest.append({"name": name, "children": []})
            self.cfg.save()
            self.rebuild(open_paths=[path])
            self.select_path(path + (name,))

        def rename_folder(self):
            iid = self.selected_iid()
            if not iid or iid not in self.folder_iids or self.locked():
                return
            node = self.nodes[iid]
            name = simpledialog.askstring(
                APP_NAME,
                "New folder name:",
                initialvalue=node.get("name", ""),
                parent=self,
            )
            if not name or name.strip() == node.get("name"):
                return
            name = name.strip()
            if any(
                n is not node and is_folder(n) and n.get("name") == name
                for n in self.parents[iid]
            ):
                messagebox.showerror(
                    APP_NAME, f"Folder '{name}' already exists there", parent=self
                )
                return
            old_path = self.folder_iids[iid]
            self._capture_expanded()
            node["name"] = name
            new_path = old_path[:-1] + (name,)
            self.ui["expanded"] = [
                (
                    list(new_path + tuple(p[len(old_path) :]))
                    if tuple(p[: len(old_path)]) == old_path
                    else p
                )
                for p in self.ui.get("expanded", [])
            ]
            self.cfg.save()
            self.rebuild(capture=False)
            self.select_path(new_path)

        def open_settings(self):
            dlg = SettingsDialog(self, self.cfg.defaults)
            if dlg.result is None:
                return
            changed = apply_settings(self.cfg.defaults, dlg.result)
            if not changed:
                self.set_status("Settings unchanged")
                return
            self.cfg.save()
            self.close_action = str(
                self.cfg.defaults.get("close_action") or "exit"
            ).lower()
            self.rebuild()
            note = " (font changes apply after a restart)" if "font" in changed else ""
            self.set_status("Settings saved: " + ", ".join(changed) + note)

        def import_putty(self):
            if self.locked():
                return
            try:
                import import_putty_registry as imp

                sessions = imp.read_registry_sessions()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    APP_NAME, f"Could not read PuTTY sessions:\n{exc}", parent=self
                )
                return
            tree, derived, notes = imp.build_tree(sessions)
            count = sum(1 for _ in walk_sessions(tree, []))
            if not count:
                messagebox.showinfo(
                    APP_NAME, "No PuTTY sessions found in the registry.", parent=self
                )
                return
            mode = ask_choice(
                self,
                "Import PuTTY Sessions",
                f"{count} PuTTY sessions found (folders from PuTTY Session Manager "
                "are kept).\n\nAdd only the sessions that are missing here, or "
                "replace the whole tree? A backup of the config file is kept when "
                "replacing.",
                [("merge", "Add missing"), ("replace", "Replace all")],
            )
            if not mode:
                return
            added = apply_putty_import(self.cfg, tree, derived, mode)
            self.cfg.save()
            self.rebuild()
            self.set_status(f"Imported {added} PuTTY session(s) ({mode})")
            if notes:
                shown = "\n".join(notes[:12]) + ("\n..." if len(notes) > 12 else "")
                messagebox.showinfo(
                    APP_NAME, "Some sessions need attention:\n\n" + shown, parent=self
                )

        def about(self):
            try:
                preset, template = resolve_terminal(
                    self.cfg.defaults.get("terminal"),
                    self.cfg.defaults.get("terminal_path"),
                )
                terminal = f"{preset} ({template[0]})"
            except Exception as exc:  # noqa: BLE001
                terminal = f"error: {exc}"
            d = self.cfg.defaults
            ssh_app = ssh_app_of(d)
            if ssh_app == "putty":
                ssh_info = f"PuTTY ({find_putty(d.get('putty_path')) or 'not found'})"
            else:
                ssh_info = f"OpenSSH ({resolve_ssh_path(d.get('ssh_path'))})"
            transfer = transfer_app_of(d)
            finders = {
                "winscp": lambda: find_winscp(d.get("winscp_path")),
                "filezilla": lambda: find_filezilla(d.get("filezilla_path")),
            }
            found = finders[transfer]() if transfer in finders else transfer
            if transfer == "none":
                transfer_info = "none"
            else:
                transfer_info = f"{transfer} ({found or 'not found'})"
            rows = [
                ("Config", str(self.cfg.path)),
                ("State", str(self.state_path)),
                ("SSH", ssh_info),
                ("Terminal", terminal),
                ("File transfer", transfer_info),
                ("Python", f"{platform.python_version()}  {sys.executable}"),
                ("Platform", platform.platform()),
            ]
            AboutDialog(self, rows, self.icons["session_big"])

    app = App(cfg)
    if start_hidden:
        app.withdraw()
    elif IS_WINDOWS:
        # A fresh start from a launcher (hotkey tool, shortcut in a detached
        # cmd) often appears behind the current window; insist a little.
        app._foreground_retries = 15
        app.after(150, app._retry_foreground)
    if on_ready:
        app.after(100, lambda: on_ready(app))
    app.mainloop()


def apply_putty_import(cfg: Config, tree: list, derived: dict, mode: str) -> int:
    """Put an imported PuTTY tree into the config.  mode 'replace' swaps the
    whole tree (keeping a .bak of the file), 'merge' adds what is missing.
    Returns the number of sessions added."""
    import import_putty_registry as imp

    for key, value in derived.items():
        cfg.defaults.setdefault(key, value)
    if mode == "replace":
        if cfg.path.exists():
            import datetime

            stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy2(cfg.path, cfg.path.with_name(f"{cfg.path.name}.bak-{stamp}"))
        cfg.data["tree"] = tree
        return sum(1 for _ in walk_sessions(tree, []))
    return imp.merge_trees(cfg.tree, tree)


def open_with_default_app(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def fix_windows_argv(argv):
    """Repair arguments fused by PowerShell tab completion (trailing backslash)."""
    out = []
    for a in argv:
        if '"' in a:
            out.extend(p.strip() for p in a.split('"') if p.strip())
        else:
            out.append(a)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        "-c",
        help="sessions.json to use (default: $SSHTREE_CONFIG or ~/.config/sshtree/sessions.json)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list sessions and exit")
    group.add_argument(
        "--print",
        metavar="SESSION",
        help="print the ssh command for SESSION (path or name)",
    )
    group.add_argument(
        "--launch", metavar="SESSION", help="open SESSION in a terminal and exit"
    )
    group.add_argument(
        "--winscp",
        "--transfer",
        metavar="SESSION",
        help="open SESSION in the file transfer app (WinSCP, FileZilla, ...) and exit",
    )
    group.add_argument(
        "--write-example", metavar="FILE", help="write the example config to FILE"
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="start the GUI in the background (for autostart; a later launch shows it)",
    )
    parser.add_argument(
        "--new-instance",
        action="store_true",
        help="start a second window instead of activating the running one",
    )
    parser.add_argument(
        "--version", action="version", version=f"{APP_NAME} {__version__}"
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "--import-putty":
        # the PuTTY registry importer, reachable from the frozen build too
        import import_putty_registry

        return import_putty_registry.main(raw[1:])
    if raw and raw[0] == "--exec":
        # terminal runner: `--exec MODE -- ssh ...` (no config needed)
        rest = raw[2:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        if len(raw) < 2 or not rest:
            print("usage: --exec MODE -- command...", file=sys.stderr)
            return 2
        return run_in_terminal(raw[1], rest)
    args = parser.parse_args(fix_windows_argv(raw))

    if args.write_example:
        atomic_write_json(Path(args.write_example), EXAMPLE_CONFIG)
        return 0

    cfg = Config(
        Path(args.config).expanduser() if args.config else default_config_path()
    )
    try:
        cfg.load()
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot load {cfg.path}: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for session, folders in walk_sessions(cfg.tree, []):
            path = "/".join(
                [f.get("name", "") for f in folders] + [session.get("name", "")]
            )
            s = effective_settings(cfg, session, folders)
            print(f"{path}\t{s.get('user', '')}@{s.get('host', '')}")
        return 0

    spec = args.print or args.launch or args.winscp
    if spec:
        hit = cfg.find(spec)
        if not hit:
            print(f"error: no session {spec!r}", file=sys.stderr)
            return 1
        session, folders = hit
        s = effective_settings(cfg, session, folders)
        try:
            if args.print:
                print(cmdline_string(build_session_command(s)))
            elif args.launch:
                print(cmdline_string(launch_session(s)))
            else:
                print(cmdline_string(launch_file_transfer(s)))
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.new_instance and activate_running_instance(
        cfg.path.with_name("instance.json"), "noop" if args.hidden else "show"
    ):
        return 0  # the running window was activated instead
    run_gui(cfg, start_hidden=args.hidden)
    return 0


if __name__ == "__main__":
    sys.exit(main())
