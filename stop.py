# -*- coding: utf-8 -*-
"""
VideoDownloader — stopper.

Usage:
    python stop.py

Standard library ONLY. Safe to run when nothing is running.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (must match start.py)
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
PIDS_FILE = PROJECT_DIR / ".pids.json"

SUPERVISOR_CMD_MARK = "start.py"
FLASK_CMD_MARK = "app.py"
NODE_CMD_MARK = "server.js"

FORBIDDEN_PORTS = {
    3000, 3001, 4200, 5000, 5173, 5174, 5432,
    5555, 8000, 8080, 8081, 8888, 9000,
}


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------
def _enable_utf8() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _enable_ansi_windows() -> None:
    if os.name != "nt":
        return
    try:
        os.system("")
    except Exception:
        pass


def _is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


class C:
    RESET = "\x1b[0m" if _is_tty() else ""
    BOLD = "\x1b[1m" if _is_tty() else ""
    DIM = "\x1b[2m" if _is_tty() else ""
    RED = "\x1b[31m" if _is_tty() else ""
    GRN = "\x1b[32m" if _is_tty() else ""
    YEL = "\x1b[33m" if _is_tty() else ""
    CYN = "\x1b[36m" if _is_tty() else ""


def _print(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid) in out.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _pid_cmdline(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"],
                capture_output=True, text=True, timeout=6,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in out.stdout.splitlines():
                line = line.strip()
                if line.startswith("CommandLine="):
                    return line[len("CommandLine="):]
        except Exception:
            pass
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return out.stdout.strip()
        except Exception:
            return ""
    else:
        try:
            p = Path(f"/proc/{pid}/cmdline")
            if p.exists():
                return p.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            pass
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip()
        except Exception:
            return ""
    return ""


def _is_our_process(pid: int, marker: str) -> bool:
    cmd = _pid_cmdline(pid)
    if not cmd:
        return False
    cmd_l = cmd.lower().replace("\\", "/")
    proj = str(PROJECT_DIR).lower().replace("\\", "/")
    if marker.lower() not in cmd_l:
        return False
    # Node and Flask are launched with absolute paths to their script;
    # supervisor is `python start.py` in PROJECT_DIR — cwd might not be in cmdline,
    # so also accept when the project dir appears anywhere.
    return (proj in cmd_l) or (marker in (NODE_CMD_MARK, FLASK_CMD_MARK))


def _kill_pid_tree(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return r.returncode == 0 or "not found" not in (r.stdout or "").lower()
        except Exception:
            return False
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            for _ in range(30):
                if not _pid_alive(pid):
                    return True
                time.sleep(0.1)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
                return True
            except Exception:
                return False


# ---------------------------------------------------------------------------
# Ports check
# ---------------------------------------------------------------------------
def _port_in_use(port: int) -> bool:
    """Return True if anything answers on 127.0.0.1:port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.4)
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Auto-start removal
# ---------------------------------------------------------------------------
def _remove_autostart() -> None:
    try:
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            if appdata:
                vbs = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / \
                      "Programs" / "Startup" / "VideoDownloader.vbs"
                if vbs.exists():
                    vbs.unlink()
        elif sys.platform == "darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / \
                    "com.videodownloader.supervisor.plist"
            if plist.exists():
                try:
                    subprocess.run(["launchctl", "unload", str(plist)],
                                   capture_output=True, timeout=6)
                except Exception:
                    pass
                plist.unlink()
        else:
            # systemd --user
            unit = Path.home() / ".config" / "systemd" / "user" / "videodownloader.service"
            if unit.exists():
                try:
                    subprocess.run(["systemctl", "--user", "disable", "videodownloader.service"],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
                unit.unlink()
                try:
                    subprocess.run(["systemctl", "--user", "daemon-reload"],
                                   capture_output=True, timeout=8)
                except Exception:
                    pass
            # crontab @reboot (best effort)
            try:
                cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=6)
                if cur.returncode == 0 and "start.py --daemon" in cur.stdout:
                    kept = [l for l in cur.stdout.splitlines()
                            if "start.py --daemon" not in l]
                    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                                   capture_output=True, text=True, timeout=6)
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Firewall rule removal (best effort)
# ---------------------------------------------------------------------------
def _remove_firewall_rule() -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", "name=VideoDownloader"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _read_pids() -> dict:
    try:
        with open(PIDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _delete_pids() -> None:
    try:
        if PIDS_FILE.exists():
            PIDS_FILE.unlink()
    except Exception:
        pass


def main() -> int:
    _enable_utf8()
    _enable_ansi_windows()

    pids = _read_pids()
    if not pids:
        _print(f"{C.YEL}VideoDownloader is not running (no .pids.json).{C.RESET}")
        _remove_autostart()
        _remove_firewall_rule()
        return 0

    sup_pid = int(pids.get("supervisor_pid") or 0)
    node_pid = int(pids.get("node_pid") or 0)
    flask_pid = int(pids.get("flask_pid") or 0)
    node_port = int(pids.get("node_port") or 0)
    flask_port = int(pids.get("flask_port") or 0)

    killed_any = False

    # 1) Stop supervisor FIRST so it cannot restart anything.
    if sup_pid and _pid_alive(sup_pid):
        if _is_our_process(sup_pid, SUPERVISOR_CMD_MARK):
            _print(f"{C.CYN}• Stopping supervisor (pid {sup_pid})…{C.RESET}")
            if _kill_pid_tree(sup_pid):
                killed_any = True
        else:
            _print(f"{C.DIM}(pid {sup_pid} no longer belongs to this project; skipping){C.RESET}")

    # 2) Stop Node.
    if node_pid and _pid_alive(node_pid):
        if _is_our_process(node_pid, NODE_CMD_MARK):
            _print(f"{C.CYN}• Stopping Node (pid {node_pid})…{C.RESET}")
            if _kill_pid_tree(node_pid):
                killed_any = True
        else:
            _print(f"{C.DIM}(pid {node_pid} no longer belongs to this project; skipping){C.RESET}")

    # 3) Stop Flask.
    if flask_pid and _pid_alive(flask_pid):
        if _is_our_process(flask_pid, FLASK_CMD_MARK):
            _print(f"{C.CYN}• Stopping Flask (pid {flask_pid})…{C.RESET}")
            if _kill_pid_tree(flask_pid):
                killed_any = True
        else:
            _print(f"{C.DIM}(pid {flask_pid} no longer belongs to this project; skipping){C.RESET}")

    # 4) Remove auto-start entries.
    _remove_autostart()

    # 5) Remove firewall rule (best effort, ignore errors).
    _remove_firewall_rule()

    # 6) Delete .pids.json.
    _delete_pids()

    # 7) Verify our ports are now free.
    still_used = []
    for p in (node_port, flask_port):
        if p and p not in FORBIDDEN_PORTS and _port_in_use(p):
            still_used.append(p)
    if still_used:
        _print(f"{C.YEL}Note: these ports still answer on 127.0.0.1: {still_used}. "
               f"They may be used by another program, not by VideoDownloader.{C.RESET}")

    if killed_any:
        _print(f"{C.GRN}VideoDownloader stopped.{C.RESET}")
    else:
        _print(f"{C.DIM}Nothing to stop (processes were already gone).{C.RESET}")

    _print(f"{C.DIM}Note: partial .part files were intentionally kept so downloads can resume.{C.RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)