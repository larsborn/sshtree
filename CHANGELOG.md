# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.2] - 2026-11-07

### Added

- Packaged builds for Linux (x86_64 tarball) and macOS (Apple Silicon
  `SshTree.app`), next to the Windows zip, plus `SHA256SUMS.txt` on every
  release. The macOS app is unsigned; see the README for the Gatekeeper step.

## [0.2.1] - 2026-11-07

### Changed

- About is a proper dialog: icon, version, licence and repository link, and
  the machine facts (config, state, SSH, terminal, file transfer, Python,
  platform) in a table of copyable fields instead of a wrapped message box.
  Buttons to copy the details and to open the config folder.

## [0.2.0] - 2026-11-07

### Changed

- Context menus depend on what was clicked: a session, a folder or empty
  space each get their own menu with only the entries that apply, so nothing
  is greyed out any more. The "Session Management" submenu is gone; folder
  operations live on folders and empty space, application items (refresh,
  sort, lock, settings, config file, about, exit) are flat on empty space and
  in an "SSH Tree" submenu elsewhere.
- "Launch WinSCP" became "Open in WinSCP / FileZilla / Open File Transfer",
  depending on the configured application.

### Added

- Settings dialog for the machine-specific defaults: SSH application, ssh and
  putty executables, terminal, hold mode, file transfer application and
  executables, close button behaviour, tree font.
- Choice of SSH application: OpenSSH in a terminal (default) or PuTTY
  (`ssh_app`, per session or globally). PuTTY gets the same user, port, key
  (`.ppk` preferred), tunnels, agent and X11 forwarding, `-N` and jump host
  (via plink).
- Choice of file transfer application: WinSCP, FileZilla, a custom command
  template with placeholders, or none (`scp_app`, `scp_command`,
  `filezilla_path`).
- Import PuTTY sessions from the GUI (Windows): add missing sessions or
  replace the tree, with a backup of the config file.

## [0.1.0] - 2026-11-07

First release.

### Added

- Folder tree of SSH sessions, configured in one JSON file with global and
  per-folder defaults.
- Launches sessions in Windows Terminal, the classic console, Terminal.app,
  iTerm, gnome-terminal, konsole and other terminals, or a custom template.
- Port forwardings (local, remote, dynamic), jump hosts, remote commands,
  arbitrary `-o` options, agent and X11 forwarding, "tunnels only" sessions.
- Launch sessions in WinSCP on Windows.
- Single-instance activation: bind any hotkey tool to the script and the
  running window comes to the front.
- Importer for PuTTY sessions and PuTTY Session Manager folders from the
  Windows registry.
- Standalone Windows build (`SshTree.exe` plus `sshtree-cli.exe`).

[0.2.2]: https://github.com/larsborn/sshtree/releases/tag/v0.2.2
[0.2.1]: https://github.com/larsborn/sshtree/releases/tag/v0.2.1
[0.2.0]: https://github.com/larsborn/sshtree/releases/tag/v0.2.0
[0.1.0]: https://github.com/larsborn/sshtree/releases/tag/v0.1.0
