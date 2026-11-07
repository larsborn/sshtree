# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/larsborn/sshtree/releases/tag/v0.1.0
