@echo off
rem Start SSH Tree from source without a console window. Extra arguments are
rem passed on, e.g. "SshTree.cmd --hidden" for an autostart entry.
rem Uses the Python launcher (any Python 3.9+ with tkinter), so the PATH order
rem does not matter. Users of the packaged build run SshTree.exe instead.
start "" pyw -3 "%~dp0sshtree.py" %*
