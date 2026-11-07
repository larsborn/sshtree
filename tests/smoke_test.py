"""Smoke test for sshtree: command building plus a scripted run of the GUI.

Run:  python tests/smoke_test.py
It opens the real window for about a second, drives the dialogs, and closes.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sshtree  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def test_builders(tmp: Path):
    pt = sshtree.parse_tunnel
    check(pt("L 8080 localhost:80") == ("-L", "8080:localhost:80"), "tunnel string L")
    check(pt("-R 2222 localhost:22") == ("-R", "2222:localhost:22"), "tunnel string -R")
    check(pt("D 1080") == ("-D", "1080"), "tunnel string D")
    check(pt("L 8080:localhost:80") == ("-L", "8080:localhost:80"), "tunnel compact")
    check(
        pt("local 127.0.0.1:8080:web:80") == ("-L", "127.0.0.1:8080:web:80"),
        "tunnel compact with bind",
    )
    check(
        pt({"type": "remote", "listen": "2222", "target": "localhost:22"})
        == ("-R", "2222:localhost:22"),
        "tunnel dict",
    )
    check(
        pt({"type": "L", "listen": 5432, "host": "db", "port": 5432})
        == ("-L", "5432:db:5432"),
        "tunnel dict host/port",
    )
    for bad in ("X 1 2", "L", "L 8080"):
        try:
            pt(bad)
        except ValueError:
            pass
        else:
            raise SystemExit(f"FAIL: {bad!r} accepted")
    print("ok   bad tunnels rejected")

    s = {
        "name": "t",
        "host": "h",
        "user": "u",
        "port": 2200,
        "identity_file": "~/.ssh/k",
        "jump_host": "j@gw",
        "agent_forwarding": True,
        "x11": "yes",
        "no_shell": False,
        "options": {"StrictHostKeyChecking": "no", "Compression": True, "Empty": ""},
        "tunnels": ["D 1080"],
        "extra_args": "-vv -o LogLevel=DEBUG",
        "command": "uptime",
    }
    cmd = sshtree.build_ssh_command(s)
    key = sshtree.expand_path("~/.ssh/k")
    check(
        cmd
        == [
            sshtree.resolve_ssh_path(None),
            "-p",
            "2200",
            "-i",
            key,
            "-J",
            "j@gw",
            "-A",
            "-X",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "Compression=yes",
            "-D",
            "1080",
            "-vv",
            "-o",
            "LogLevel=DEBUG",
            "u@h",
            "uptime",
        ],
        f"ssh command: {cmd}",
    )
    ssh_exe = sshtree.resolve_ssh_path(None)
    check(
        sshtree.build_ssh_command({"host": "h"}) == [ssh_exe, "h"],
        "minimal ssh command",
    )
    check(
        sshtree.build_ssh_command({"host": "h", "ssh_path": "/opt/bin/ssh"})[0]
        == sshtree.expand_path("/opt/bin/ssh"),
        "explicit ssh_path",
    )
    if sshtree.IS_WINDOWS:
        check(
            ssh_exe.lower().endswith(r"system32\openssh\ssh.exe"),
            f"auto ssh_path prefers Windows OpenSSH: {ssh_exe}",
        )
    cmd = sshtree.build_ssh_command(
        {"host": "h", "identity_file": ["~/.ssh/a", "", "~/.ssh/b"]}
    )
    check(
        cmd[1:]
        == [
            "-i",
            sshtree.expand_path("~/.ssh/a"),
            "-i",
            sshtree.expand_path("~/.ssh/b"),
            "h",
        ],
        f"identity_file list -> several -i: {cmd}",
    )
    try:
        sshtree.build_ssh_command({"name": "x"})
    except ValueError:
        print("ok   missing host rejected")
    else:
        raise SystemExit("FAIL: missing host accepted")

    # hold-on-exit: ssh runs through the script's own --exec runner
    orig = sshtree.IS_WINDOWS
    w = sshtree.wrap_hold_on_exit(["ssh", "-o", "A=b c", "h"], "error")
    check(
        w[0] == sshtree.console_python()
        and w[1].endswith("sshtree.py")
        and w[2:] == ["--exec", "error", "--", "ssh", "-o", "A=b c", "h"],
        f"hold wrapper: {w}",
    )
    check(
        sshtree.wrap_hold_on_exit(["ssh", "h"], "no")[3] == "never",
        "hold mode 'no' -> never",
    )
    check(
        sshtree.wrap_hold_on_exit(["ssh", "h"], None)[3] == "error",
        "hold mode default -> error",
    )
    check(
        not sshtree.console_python().lower().endswith("pythonw.exe"),
        "runner uses the console python",
    )

    # runner behaviour: patch wait_key and run real child processes
    waited = []
    real_wait = sshtree.wait_key
    sshtree.wait_key = lambda prompt: waited.append(prompt)
    try:
        py = sshtree.console_python()
        child = lambda code: [py, "-c", f"import sys; sys.exit({code})"]
        check(
            sshtree.run_in_terminal("error", child(0)) == 0 and not waited,
            "runner: exit 0 closes",
        )
        check(
            sshtree.run_in_terminal("error", child(1)) == 0 and not waited,
            "runner: remote exit 1 closes",
        )
        sshtree.run_in_terminal("error", child(255))
        check(
            len(waited) == 1 and "255" in waited[0], f"runner: exit 255 holds: {waited}"
        )
        waited.clear()
        sshtree.run_in_terminal("never", child(255))
        check(not waited, "runner: never mode does not hold")
        sshtree.run_in_terminal("always", child(0))
        check(len(waited) == 1, "runner: always mode holds")
        waited.clear()
        sshtree.run_in_terminal("error", ["definitely-not-a-program-xyz"])
        check(
            len(waited) == 1 and "cannot start" in waited[0],
            "runner: missing program reported",
        )
        waited.clear()
        # Ctrl+C: a KeyboardInterrupt during the wait must not hold, even at 255
        import _thread
        import threading

        threading.Timer(0.3, _thread.interrupt_main).start()
        sshtree.run_in_terminal(
            "error", [py, "-c", "import time, sys; time.sleep(1.2); sys.exit(255)"]
        )
        check(not waited, "runner: interrupted session closes silently")
    finally:
        sshtree.wait_key = real_wait
        sshtree.IS_WINDOWS = orig

    # terminal templates (the hold wrapper is tested above; bypass it here)
    real_wrap = sshtree.wrap_hold_on_exit
    sshtree.wrap_hold_on_exit = lambda cmd, mode: cmd
    argv, preset = sshtree.terminal_command(
        {"name": "My Box", "terminal": "wt", "hold_on_exit": "never"},
        ["ssh", "h", "a; b"],
    )
    check(
        preset == "wt"
        and argv[1:]
        == ["-w", "last", "new-tab", "--title", "My Box", "ssh", "h", "a\\; b"]
        and argv[0].lower().endswith(("wt", "wt.exe", "windowsterminal.exe")),
        f"wt template: {argv}",
    )
    argv, preset = sshtree.terminal_command(
        {"name": "x", "terminal": "console", "hold_on_exit": "never"}, ["ssh", "h"]
    )
    check(
        argv == ["cmd", "/c", "title", "x", "&", "ssh", "h"],
        f"console template: {argv}",
    )
    argv, preset = sshtree.terminal_command(
        {"name": "x", "terminal": "gnome-terminal", "hold_on_exit": "never"},
        ["ssh", "h"],
    )
    check(
        argv == ["gnome-terminal", "--title", "x", "--", "ssh", "h"],
        f"gnome template: {argv}",
    )
    try:
        sshtree.IS_WINDOWS = False  # the macOS preset always sees POSIX quoting
        argv, preset = sshtree.terminal_command(
            {"name": "x", "terminal": "macos-terminal", "hold_on_exit": "never"},
            ["ssh", "-o", 'A="q"', "h"],
        )
    finally:
        sshtree.IS_WINDOWS = orig
    check(
        argv[2] == 'tell application "Terminal" to do script "ssh -o \'A=\\"q\\"\' h"',
        f"applescript escaping: {argv[2]}",
    )
    argv, preset = sshtree.terminal_command(
        {
            "name": "x",
            "terminal": ["myterm", "-t", "{title}", "-e", "{cmdline}"],
            "hold_on_exit": "never",
        },
        ["ssh", "h"],
    )
    check(argv == ["myterm", "-t", "x", "-e", "ssh h"], f"custom template: {argv}")
    try:
        sshtree.resolve_terminal("nonsense")
    except ValueError:
        print("ok   unknown terminal rejected")
    else:
        raise SystemExit("FAIL: unknown terminal accepted")
    name, template = sshtree.resolve_terminal("auto")
    check(
        name in sshtree.TERMINAL_PRESETS,
        f"auto-detected terminal: {name} -> {template[0]}",
    )

    # terminal_path replaces the preset's executable
    fake_wt = tmp / "WindowsTerminal.exe"
    fake_wt.write_bytes(b"")
    argv, preset = sshtree.terminal_command(
        {
            "name": "x",
            "terminal": "wt",
            "terminal_path": str(fake_wt),
            "hold_on_exit": "never",
        },
        ["ssh", "h"],
    )
    check(
        preset == "wt"
        and argv == [str(fake_wt), "-w", "last", "new-tab", "--title", "x", "ssh", "h"],
        f"terminal_path used: {argv}",
    )
    argv, _ = sshtree.terminal_command(
        {
            "name": "x",
            "terminal": "gnome-terminal",
            "terminal_path": str(fake_wt),
            "hold_on_exit": "never",
        },
        ["ssh", "h"],
    )
    check(
        argv[0] == str(fake_wt) and argv[1:] == ["--title", "x", "--", "ssh", "h"],
        "terminal_path on other presets",
    )
    try:
        sshtree.resolve_terminal("wt", str(tmp / "missing.exe"))
    except FileNotFoundError:
        print("ok   missing terminal_path reported")
    else:
        raise SystemExit("FAIL: missing terminal_path accepted")
    name, template = sshtree.resolve_terminal("wt", "auto")
    check(
        (
            name == "wt" and template[0] != "wt"
            if sshtree.find_windows_terminal()
            else template[0] == "wt"
        ),
        f"wt auto path: {template[0]}",
    )
    sshtree.wrap_hold_on_exit = real_wrap

    # WinSCP
    fake = tmp / "WinSCP.exe"
    fake.write_bytes(b"")
    ppk = tmp / "key.ppk"
    ppk.write_bytes(b"")
    s = {
        "name": "w",
        "host": "h",
        "user": "u",
        "port": 2200,
        "identity_file": str(tmp / "key"),
        "winscp_path": str(fake),
        "jump_host": "j@gw:2022",
        "winscp_args": "/log=x.log",
    }
    argv = sshtree.build_winscp_command(s)
    check(
        argv
        == [
            str(fake),
            "sftp://u@h:2200/",
            f"/privatekey={ppk}",
            "/rawsettings",
            "Tunnel=1",
            "TunnelHostName=gw",
            "TunnelPortNumber=2022",
            "TunnelUserName=j",
            f"TunnelPublicKeyFile={ppk}",
            "/log=x.log",
        ],
        f"winscp command: {argv}",
    )
    s2 = {
        "host": "h",
        "winscp_path": str(fake),
        "winscp_protocol": "scp",
        "winscp_key": str(ppk),
    }
    check(
        sshtree.build_winscp_command(s2)
        == [str(fake), "scp://h:22/", f"/privatekey={ppk}"],
        "winscp explicit key",
    )
    try:
        sshtree.build_winscp_command(
            {"host": "h", "winscp_path": str(tmp / "missing.exe")}
        )
    except FileNotFoundError:
        print("ok   missing WinSCP reported")
    else:
        raise SystemExit("FAIL: missing winscp accepted")

    # config find + folder defaults
    cfg = sshtree.Config(tmp / "sessions.json")
    cfg.load()
    check(cfg.created and cfg.path.exists(), "example config written on first run")
    hit = cfg.find("Home lab/Customers/acme-prod")
    check(
        hit is not None and hit[0]["name"] == "acme-prod" and len(hit[1]) == 2,
        "find by path",
    )
    check(cfg.find("nas") is not None and cfg.find("nas/x") is None, "find by name")
    s = sshtree.effective_settings(cfg, *hit)
    check(
        s["identity_file"] == "~/.ssh/homelab"
        and s["user"] == "alice"
        and s["options"]
        == {"ServerAliveInterval": 30, "StrictHostKeyChecking": "accept-new"},
        "defaults merge chain",
    )

    check(
        sshtree.activate_running_instance(tmp / "instance.json") is False,
        "no running instance -> False",
    )


def test_importer(tmp: Path):
    import import_putty_registry as imp

    pf = imp.parse_port_forwardings
    check(
        pf("L3306=db:3306,D9001=,4R2222=localhost:22,,6L80=[::1]:80")
        == ["L 3306 db:3306", "D 9001", "R 2222 localhost:22", "L 80 [::1]:80"],
        "PuTTY port forwardings parsed",
    )
    ppk = tmp / "mykey.ppk"
    ppk.write_bytes(b"")
    check(
        imp.map_key_file(str(ppk)) == (None, str(ppk).replace("\\", "/")),
        "ppk without OpenSSH twin kept for WinSCP",
    )
    (tmp / "mykey").write_bytes(b"")
    ident, wk = imp.map_key_file(str(ppk))
    check(
        ident is not None and ident.endswith("/mykey") and wk is None,
        f"ppk mapped to OpenSSH key: {ident}",
    )

    sessions = {
        "Default Settings": {"HostName": "", "AgentFwd": 1},
        "WinSCP temporary session": {"HostName": "x", "AgentFwd": 0},
        "alpha": {
            "HostName": "root@alpha.lan",
            "PortNumber": 22,
            "AgentFwd": 1,
            "PsmPath": "Sessions\\Lab\\Sub",
        },
        "beta": {
            "HostName": "beta.lan",
            "UserName": "admin",
            "PortNumber": 2222,
            "AgentFwd": 1,
            "PortForwardings": "L8080=localhost:80",
            "PsmPath": "Sessions\\Lab",
        },
        "gamma": {
            "HostName": "gamma",
            "AgentFwd": 0,
            "X11Forward": 1,
            "Compression": 1,
            "RemoteCommand": "uptime",
            "ProxyMethod": 5,
            "ProxyTelnetCommand": "plink -nc %host:%port gw",
        },
    }
    tree, derived, notes = imp.build_tree(sessions)
    check(derived == {"agent_forwarding": True}, f"derived defaults: {derived}")
    names = [n["name"] for n in tree]
    check(names == ["Lab", "gamma"], f"top level: {names}")
    lab = tree[0]["children"]
    check(
        [n["name"] for n in lab] == ["Sub", "beta"],
        f"Lab folder: {[n['name'] for n in lab]}",
    )
    alpha = lab[0]["children"][0]
    check(
        alpha == {"name": "alpha", "host": "alpha.lan", "user": "root"},
        f"alpha: {alpha}",
    )
    beta = lab[1]
    check(
        beta
        == {
            "name": "beta",
            "host": "beta.lan",
            "user": "admin",
            "port": 2222,
            "tunnels": ["L 8080 localhost:80"],
        },
        f"beta: {beta}",
    )
    gamma = tree[1]
    check(
        gamma["agent_forwarding"] is False
        and gamma["x11"] is True
        and gamma["command"] == "uptime"
        and gamma["options"] == {"Compression": "yes"}
        and "plink" in gamma["_note"],
        f"gamma: {gamma}",
    )
    check(len(notes) == 1 and notes[0].startswith("gamma:"), f"notes: {notes}")
    existing = [
        {"name": "Lab", "children": [{"name": "beta", "host": "old"}]},
        {"name": "mine", "host": "m"},
    ]
    added = imp.merge_trees(existing, tree)
    check(
        added == 2
        and existing[0]["children"][0]["host"] == "old"
        and [n["name"] for n in existing] == ["Lab", "mine", "gamma"],
        f"merge keeps existing: {existing}",
    )


def test_gui(tmp: Path):
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    cfg = sshtree.Config(tmp / "sessions.json")
    cfg.load()
    messagebox.askyesno = lambda *a, **k: True
    messagebox.showerror = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError(f"showerror: {a}")
    )
    simpledialog.askstring = lambda *a, **k: "Smoke folder"
    results = {}

    def fill_dialog(app, values, tunnels=None):
        dialogs = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
        assert dialogs, "no dialog open"
        dlg = dialogs[-1]
        for k, v in values.items():
            dlg.vars[k].set(v)
        if tunnels:
            dlg.texts["tunnels"].delete("1.0", "end")
            dlg.texts["tunnels"].insert("1.0", tunnels)
        dlg._ok()

    def drive(app):
        try:
            # a bare X server (xvfb in CI) never hands the window the input
            # focus by itself, and the focus checks below depend on it
            app.focus_force()
            app.update()
            names = [app.tree.item(i, "text") for i in app.tree.get_children("")]
            results["top"] = names
            app.set_all_open(True)
            app.select_path(("Home lab", "Customers", "acme-prod"))
            app.on_select()
            results["status"] = app.status_var.get()
            results["cmd_copy"] = None
            app.copy_command()
            results["cmd_copy"] = app.clipboard_get()
            app.select_edge(first=True)
            results["home"] = app.selected_path()
            app.select_edge(first=False)
            results["end"] = app.selected_path()
            app.filter_var.set("nas")
            app.select_edge(first=False)
            results["end_nested"] = app.selected_path()
            app.tree.selection_set(())
            app.filter_var.set("acme")
            app.focus_first_session()
            results["enter_select"] = app.selected_path()
            results["enter_focus"] = app.focus_get() is app.tree
            app.filter_var.set("zzz-nothing")
            app.tree.selection_set(())
            app.focus_first_session()
            results["enter_none"] = app.selected_path()
            app.filter_var.set("")

            # filter bar: hidden at start, opened by Ctrl+F / typing, Escape in stages
            results["bar_hidden_at_start"] = not app.filter_bar.winfo_manager()
            app.show_filter("abc")
            app.update()
            results["bar_shown"] = bool(app.filter_bar.winfo_manager())
            app.on_escape()
            results["esc_cleared"] = app.filter_var.get()
            results["bar_after_clear"] = bool(app.filter_bar.winfo_manager())
            app.on_escape()
            app.update()
            results["bar_after_second_esc"] = bool(app.filter_bar.winfo_manager())
            saved_action, app.close_action = app.close_action, "hide"
            app.on_escape()
            app.update()
            results["esc_state"] = app.state()
            app.close_action = saved_action
            app.show_window()
            app.update()

            app.filter_var.set("acme")
            results["filtered"] = sorted(app.tree.item(i, "text") for i in app.nodes)
            app.filter_var.set("")

            # New session through the dialog
            app.tree.selection_set(())
            app.after(
                150,
                lambda: fill_dialog(
                    app,
                    {
                        "name": "smoke",
                        "host": "10.9.9.9",
                        "port": "2201",
                        "no_shell": "yes",
                    },
                    "L 8080 localhost:80\nD 1080",
                ),
            )
            app.new_session()
            # Edit it: rename + move into a folder
            app.select_path(("smoke",))
            dlg_folder = "Home lab/Customers"

            def set_folder_then_ok():
                dlg = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)][
                    -1
                ]
                dlg.folder_var.set(dlg_folder)
                fill_dialog(app, {"name": "smoke2"})

            app.after(150, set_folder_then_ok)
            app.edit_session()
            # New folder at top level, move smoke2 into it by the move helper
            app.tree.selection_set(())
            app.new_folder()
            app.select_path(("Home lab", "Customers", "smoke2"))
            app._move(app.selected_iid(), ("Smoke folder",))
            # Delete the example-host session
            app.select_path(("example-host",))
            app.delete_node()
            results["sort_host"] = None
            app.sort_var.set("host")
            app.on_sort_change()
            app.locked_var.set(True)
            app.on_lock_change()
            results["locked_title"] = app.title()
            app.locked_var.set(False)
            app.on_lock_change()
            app.refresh()

            # single-instance activation: a second launch talks to this window
            results["activated"] = sshtree.activate_running_instance(
                tmp / "instance.json", "show"
            )
            results["activation_event"] = app.events.get(timeout=2)
            # use the withdraw path: iconify needs a window manager (none on xvfb)
            saved_action, app.close_action = app.close_action, "hide"
            app.hide_window()
            app.update()
            results["hidden_state"] = app.state()
            app.show_window()
            app.update()
            results["shown_state"] = app.state()
            app.close_action = saved_action

            # scrollbar auto-hides: few rows -> hidden, many rows -> shown
            app.geometry("360x600")
            app.set_all_open(True)
            app.update()
            results["vsb_tall"] = bool(app.vsb.winfo_manager())
            dummies = [app.tree.insert("", "end", text=f"dummy{i}") for i in range(60)]
            app.update()
            results["vsb_small"] = bool(app.vsb.winfo_manager())
            app.tree.delete(*dummies)
            app.update()
            results["vsb_after"] = bool(app.vsb.winfo_manager())

            # re-showing keeps the inner focus: tree entry stays focused,
            # and the filter entry stays focused if the user was typing there
            app.hide_filter()
            app.select_path(("Home lab", "Customers", "acme-prod"))
            app.tree.focus_set()
            app.update()
            app.hide_window()
            app.update()
            app.show_window()
            app.update()
            results["refocus_tree"] = app.focus_get() is app.tree
            results["refocus_sel"] = app.selected_path()
            app.show_filter("acme")
            app.update()
            app.hide_window()
            app.update()
            app.show_window()
            app.update()
            results["refocus_entry"] = app.focus_get() is app.filter_entry
            app.hide_filter()
        except Exception:  # noqa: BLE001
            import traceback

            results["error"] = traceback.format_exc()
        finally:
            app.on_close()

    sshtree.run_gui(cfg, on_ready=drive)

    check(
        "error" not in results, f"GUI script ran without error: {results.get('error')}"
    )
    check(
        results["top"] == ["Home lab", "example-host"],
        f"top level order: {results['top']}",
    )
    check(
        results["status"].split()[0].lower().rstrip('"').endswith(("ssh", "ssh.exe"))
        and "alice@10.0.0.5" in results["status"],
        f"status preview: {results['status']}",
    )
    check(results["cmd_copy"] == results["status"], "copy command matches preview")
    check(
        results["filtered"] == ["Customers", "Home lab", "acme-prod"],
        f"filter: {results['filtered']}",
    )
    check("[locked]" in results["locked_title"], "lock shows in title")
    check(
        results["home"] == ("Home lab",)
        and results["end"] == ("example-host",)
        and results["end_nested"] == ("Home lab", "nas"),
        f"Home/End: {results['home']} {results['end']} {results['end_nested']}",
    )
    check(
        results["enter_select"] == ("Home lab", "Customers", "acme-prod")
        and results["enter_focus"] is True
        and results["enter_none"] is None,
        f"Enter in filter selects first match: {results['enter_select']} "
        f"focus={results['enter_focus']} none={results['enter_none']}",
    )
    check(
        results["bar_hidden_at_start"]
        and results["bar_shown"]
        and results["esc_cleared"] == ""
        and results["bar_after_clear"]
        and not results["bar_after_second_esc"]
        and results["esc_state"] == "withdrawn",
        "filter bar hidden by default; Escape clears, hides the bar, then the window: "
        f"{ {k: v for k, v in results.items() if k.startswith(('bar', 'esc'))} }",
    )
    check(
        results["activated"] is True and results["activation_event"] == "show",
        f"activation via socket: {results['activated']} / {results['activation_event']}",
    )
    check(
        results["hidden_state"] in ("iconic", "withdrawn")
        and results["shown_state"] == "normal",
        f"hide/show window: {results['hidden_state']} -> {results['shown_state']}",
    )
    check(not (tmp / "instance.json").exists(), "instance.json removed on exit")
    check(
        results["vsb_tall"] is False
        and results["vsb_small"] is True
        and results["vsb_after"] is False,
        f"scrollbar auto-hide: few={results['vsb_tall']} many={results['vsb_small']} "
        f"after={results['vsb_after']}",
    )
    check(
        results["refocus_tree"]
        and results["refocus_sel"] == ("Home lab", "Customers", "acme-prod")
        and results["refocus_entry"],
        f"re-show restores inner focus: tree={results['refocus_tree']} "
        f"sel={results['refocus_sel']} entry={results['refocus_entry']}",
    )

    saved = json.loads((tmp / "sessions.json").read_text(encoding="utf-8"))
    top_names = [n["name"] for n in saved["tree"]]
    check(
        "example-host" not in top_names and "Smoke folder" in top_names,
        f"saved top level: {top_names}",
    )
    folder = next(n for n in saved["tree"] if n["name"] == "Smoke folder")
    check(
        [n["name"] for n in folder["children"]] == ["smoke2"],
        f"moved session: {folder}",
    )
    smoke = folder["children"][0]
    check(
        smoke
        == {
            "name": "smoke2",
            "host": "10.9.9.9",
            "port": 2201,
            "no_shell": True,
            "tunnels": ["L 8080 localhost:80", "D 1080"],
        },
        f"dialog values saved: {smoke}",
    )
    state = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
    check(
        state["sort"] == "host"
        and "geometry" in state
        and ["Home lab"] in state["expanded"],
        f"state saved: {state}",
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_builders(tmp)
        test_importer(tmp)
        test_gui(tmp)
    print("ALL OK")
