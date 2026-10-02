# -*- coding: utf-8 -*-
"""
VideoDownloader — launcher + supervisor daemon.

Usage:
    python start.py                 # ensure running, print links box, open browser
    python start.py --daemon        # supervisor loop (internal / auto-start)
    python start.py --no-browser    # don't open a browser tab
    python start.py --no-autostart  # don't register auto-start at login
    python start.py --status        # print the links box and exit

Standard library ONLY.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
PIDS_FILE = PROJECT_DIR / ".pids.json"
SUPERVISOR_LOG = PROJECT_DIR / "supervisor.log"
FLASK_LOG = PROJECT_DIR / "flask.log"
NODE_LOG = PROJECT_DIR / "node.log"
DEPS_MARKER = PROJECT_DIR / ".venv" / ".deps_ok"
REQ_FILE = PROJECT_DIR / "requirements.txt"

NODE_PORT_DEFAULT = 7421
FLASK_PORT_DEFAULT = 7422
FORBIDDEN_PORTS = {
    3000, 3001, 4200, 5000, 5173, 5174, 5432,
    5555, 8000, 8080, 8081, 8888, 9000,
}

LOG_TRUNCATE_BYTES = 2 * 1024 * 1024  # 2 MB

SUPERVISOR_CMD_MARK = "start.py"
FLASK_CMD_MARK = "app.py"
NODE_CMD_MARK = "server.js"

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
        # Also triggers virtual terminal processing on recent Windows builds.
        os.system("")
    except Exception:
        pass
    try:
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | 0x0004)
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
    BLU = "\x1b[34m" if _is_tty() else ""
    MAG = "\x1b[35m" if _is_tty() else ""
    CYN = "\x1b[36m" if _is_tty() else ""


def _print(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Process / PID helpers
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
    """Return the command line of a PID (best-effort, may be empty)."""
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
        # Fallback: PowerShell
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
    return (marker.lower() in cmd_l) and (proj in cmd_l or marker == NODE_CMD_MARK)


def _kill_pid_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            for _ in range(30):
                if not _pid_alive(pid):
                    return
                time.sleep(0.1)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# .pids.json helpers
# ---------------------------------------------------------------------------
def _read_pids() -> dict:
    try:
        with open(PIDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_pids(data: dict) -> None:
    tmp = PIDS_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, PIDS_FILE)
    except Exception:
        pass


def _delete_pids() -> None:
    try:
        if PIDS_FILE.exists():
            PIDS_FILE.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
def _truncate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_TRUNCATE_BYTES:
            path.write_text("", encoding="utf-8")
    except Exception:
        pass


def _tail_log(path: Path, n: int = 20) -> str:
    try:
        if not path.exists():
            return f"(no log: {path.name})"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as e:
        return f"(could not read {path.name}: {e})"


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------
def _check_python() -> bool:
    if sys.version_info < (3, 10):
        _print(f"{C.RED}Python 3.10 or newer is required. You have "
               f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}.{C.RESET}")
        _print("Download: https://www.python.org/downloads/")
        return False
    return True


def _find_node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return None
        m = re.match(r"v?(\d+)", out.stdout.strip())
        if not m or int(m.group(1)) < 18:
            return None
        return node
    except Exception:
        return None


def _check_node() -> str | None:
    node = _find_node()
    if not node:
        _print(f"{C.RED}Node.js 18 or newer is required and must be on PATH.{C.RESET}")
        _print("Download: https://nodejs.org/en/download")
        return None
    return node


# ---------------------------------------------------------------------------
# venv
# ---------------------------------------------------------------------------
def _venv_python() -> Path:
    if os.name == "nt":
        return PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    return PROJECT_DIR / ".venv" / "bin" / "python"


def _venv_pythonw() -> Path | None:
    if os.name == "nt":
        p = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"
        return p if p.exists() else None
    return None


def _ensure_venv() -> bool:
    py = _venv_python()
    if py.exists():
        return True
    _print(f"{C.CYN}• Creating virtual environment (.venv)…{C.RESET}")
    try:
        import venv  # stdlib
        venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(str(PROJECT_DIR / ".venv"))
    except Exception as e:
        _print(f"{C.RED}Could not create venv: {e}{C.RESET}")
        return False
    # pip may need bootstrap on some platforms.
    if not py.exists():
        _print(f"{C.RED}venv created but python executable is missing.{C.RESET}")
        return False
    return True


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb", encoding=None) as f:  # type: ignore[arg-type]
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _install_deps(py: Path) -> bool:
    if not REQ_FILE.exists():
        _print(f"{C.RED}requirements.txt not found next to start.py.{C.RESET}")
        return False
    try:
        current = _sha256_file(REQ_FILE)
    except Exception as e:
        _print(f"{C.RED}Could not hash requirements.txt: {e}{C.RESET}")
        return False

    if DEPS_MARKER.exists():
        try:
            if DEPS_MARKER.read_text(encoding="utf-8").strip() == current:
                return True
        except Exception:
            pass

    _print(f"{C.CYN}• Installing Python dependencies (first run may take a minute)…{C.RESET}")
    try:
        r = subprocess.run(
            [str(py), "-m", "pip", "install", "-r", str(REQ_FILE), "--disable-pip-version-check"],
            cwd=str(PROJECT_DIR),
        )
    except Exception as e:
        _print(f"{C.RED}pip failed: {e}{C.RESET}")
        return False
    if r.returncode != 0:
        _print(f"{C.RED}Dependency installation failed (offline? no internet?).{C.RESET}")
        _print("Try again when you have an internet connection.")
        return False
    try:
        DEPS_MARKER.parent.mkdir(parents=True, exist_ok=True)
        DEPS_MARKER.write_text(current, encoding="utf-8")
    except Exception:
        pass
    return True


def _try_update_ytdlp(py: Path) -> None:
    try:
        subprocess.run(
            [str(py), "-m", "pip", "install", "-U", "yt-dlp[default]", "--disable-pip-version-check"],
            capture_output=True, timeout=60,
        )
    except Exception:
        pass


def _resolve_ffmpeg(py: Path) -> str | None:
    """Ask imageio-ffmpeg for a bundled ffmpeg path, else use PATH."""
    try:
        code = (
            "import imageio_ffmpeg, sys;"
            "sys.stdout.write(imageio_ffmpeg.get_ffmpeg_exe() or '')"
        )
        out = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            candidate = out.stdout.strip()
            if Path(candidate).exists():
                return candidate
    except Exception:
        pass
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    return None


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------
def _port_free(port: int) -> bool:
    # Bind on 0.0.0.0
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(("0.0.0.0", port))
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass
    # Nothing should answer on 127.0.0.1 either
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        c.settimeout(0.25)
        c.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        try:
            c.close()
        except Exception:
            pass


def _is_our_port(port: int) -> bool:
    """Return True if the port is used by OUR processes (per .pids.json)."""
    pids = _read_pids()
    node_port = pids.get("node_port")
    flask_port = pids.get("flask_port")
    if port not in (node_port, flask_port):
        return False
    # Confirm the recorded PID actually exists
    for key in ("supervisor_pid", "node_pid", "flask_pid"):
        pid = int(pids.get(key) or 0)
        if pid > 0 and _pid_alive(pid):
            return True
    return False


def _pick_ports(saved: dict | None) -> tuple[int, int]:
    """Try saved ports, then defaults, then scan upward from 7421."""
    saved = saved or {}
    s_node = int(saved.get("node_port") or 0)
    s_flask = int(saved.get("flask_port") or 0)

    candidates: list[tuple[int, int]] = []
    if s_node and s_flask:
        candidates.append((s_node, s_flask))
    candidates.append((NODE_PORT_DEFAULT, FLASK_PORT_DEFAULT))

    for n, f in candidates:
        if n in FORBIDDEN_PORTS or f in FORBIDDEN_PORTS:
            continue
        if n == f:
            continue
        if _is_our_port(n) or _is_our_port(f):
            # already ours — treat as free to reuse
            return n, f
        if _port_free(n) and _port_free(f) and _port_free(n + 1000) is not None:
            # extra bind for safety is unnecessary; keep simple
            return n, f

    # Scan upward
    for base in range(NODE_PORT_DEFAULT, 7500):
        if base in FORBIDDEN_PORTS:
            continue
        if base + 1 in FORBIDDEN_PORTS:
            continue
        if base == base + 1:
            continue
        if _port_free(base) and _port_free(base + 1):
            return base, base + 1
    raise RuntimeError("No free port pair found in range 7421..7499")


# ---------------------------------------------------------------------------
# Network helpers (stdlib only)
# ---------------------------------------------------------------------------
def _primary_lan_ip() -> str | None:
    for probe in ("10.255.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 1))
                ip = s.getsockname()[0]
                if ip and not ip.startswith(("127.", "169.254.")):
                    return ip
            finally:
                s.close()
        except Exception:
            continue
    return None


def _all_lan_ips() -> list[tuple[str, str]]:
    """Return list of (iface_guess, ip). Stdlib only — best effort."""
    results: list[tuple[str, str]] = []
    seen = set()
    # Primary first
    p = _primary_lan_ip()
    if p:
        results.append(("primary", p))
        seen.add(p)
    # Other local IPv4 addresses via getaddrinfo of hostname.
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "169.254.")) or ip in seen:
                continue
            results.append(("host", ip))
            seen.add(ip)
    except Exception:
        pass
    return results


def _fetch_network_from_flask(flask_port: int) -> dict | None:
    try:
        url = f"http://127.0.0.1:{flask_port}/api/network"
        with urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            if data.get("ok"):
                return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------
def _http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _wait_for_health(node_port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{node_port}/api/health"
    while time.time() < deadline:
        if _http_ok(url, 1.5):
            return True
        time.sleep(0.6)
    return False


# ---------------------------------------------------------------------------
# Links box + QR
# ---------------------------------------------------------------------------
def _box_border(width: int = 66) -> str:
    return "=" * width


def _print_links_box(node_port: int, download_dir: str, addresses: list[dict] | None = None,
                     primary_ip: str | None = None) -> None:
    local_url = f"http://localhost:{node_port}"
    mdns_url = f"http://videodownloader.local:{node_port}"

    if addresses is None:
        addresses = [{"iface": iface, "ip": ip, "url": f"http://{ip}:{node_port}", "primary": ip == primary_ip}
                     for iface, ip in _all_lan_ips()]
        if primary_ip is None and addresses:
            addresses[0]["primary"] = True
            primary_ip = addresses[0]["ip"]

    primary_url = f"http://{primary_ip}:{node_port}" if primary_ip else None

    _print()
    _print(f"{C.BOLD}{C.MAG}{_box_border()}{C.RESET}")
    _print(f"{C.BOLD}{C.MAG}  VideoDownloader is RUNNING (in the background){C.RESET}")
    _print(f"{C.BOLD}{C.MAG}{_box_border()}{C.RESET}")
    _print(f"  This PC        : {C.CYN}{local_url}{C.RESET}")
    if primary_url:
        _print(f"  {C.BOLD}{C.GRN}MOBILE / OTHER{C.RESET} : {C.BOLD}{C.GRN}{primary_url}{C.RESET}    "
               f"{C.DIM}<-- open this on your phone (same Wi-Fi){C.RESET}")
        # Extra adapters
        for a in addresses:
            if a.get("primary"):
                continue
            _print(f"    also         : {a['url']}  {C.DIM}({a['iface']}){C.RESET}")
    else:
        _print(f"  {C.YEL}No network detected. Connect to Wi-Fi to use it from your phone.{C.RESET}")
    _print(f"  Name link      : {C.BLU}{mdns_url}{C.RESET}")
    _print(f"  Downloads      : {download_dir}")
    _print(f"  Stop           : {C.YEL}python stop.py{C.RESET}")
    _print(f"{C.BOLD}{C.MAG}{_box_border()}{C.RESET}")
    _print()

    # QR code for phone
    if primary_url:
        _print_qr(primary_url)

    _print(f"{C.DIM}Phone and PC must be on the same Wi-Fi. If the page does not open on the "
           f"phone, see the tips below.{C.RESET}")


def _print_qr(url: str) -> None:
    """Print QR by running the venv python with the qrcode package."""
    py = _venv_python()
    if not py.exists():
        return
    try:
        code = (
            "import sys, qrcode;"
            "q=qrcode.QRCode(border=1);"
            "q.add_data(sys.argv[1]);"
            "q.make();"
            "q.print_ascii(invert=True)"
        )
        r = subprocess.run([str(py), "-c", code, url], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            _print(r.stdout)
    except Exception:
        pass


def _lan_self_test(node_port: int, primary_ip: str | None) -> bool:
    if not primary_ip:
        return False
    url = f"http://{primary_ip}:{node_port}/node-health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            ok = 200 <= r.status < 300
    except Exception:
        ok = False
    if ok:
        _print(f"{C.GRN}LAN check: OK (your phone can open the MOBILE link){C.RESET}")
    else:
        _print(f"{C.YEL}LAN check FAILED. Most likely Windows Firewall is blocking the port. "
               f"Run this in an Administrator PowerShell:{C.RESET}")
        _print(f"  netsh advfirewall firewall add rule name=VideoDownloader dir=in "
               f"action=allow protocol=TCP localport={node_port} profile=any")
        _print(f"{C.YEL}Make sure the Wi-Fi is not set to 'Public network' with client isolation / "
               f"AP isolation enabled on the router, and that the phone is on the same Wi-Fi.{C.RESET}")
    return ok


# ---------------------------------------------------------------------------
# Auto-start registration
# ---------------------------------------------------------------------------
def _register_autostart(project_dir: Path) -> None:
    try:
        if os.name == "nt":
            _register_autostart_windows(project_dir)
        elif sys.platform == "darwin":
            _register_autostart_macos(project_dir)
        else:
            _register_autostart_linux(project_dir)
    except Exception as e:
        _print(f"{C.DIM}(auto-start registration skipped: {e}){C.RESET}")


def _register_autostart_windows(project_dir: Path) -> None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    vbs = startup / "VideoDownloader.vbs"
    pyw = _venv_pythonw()
    py_for_vbs = pyw if pyw else _venv_python()
    # Use pythonw if available to avoid any console flashing.
    vbs_content = (
        'Set WShell = CreateObject("WScript.Shell")\r\n'
        f'WShell.CurrentDirectory = "{project_dir}"\r\n'
        f'WShell.Run """{py_for_vbs}"" ""{project_dir / "start.py"}"" --daemon --no-browser", 0, False\r\n'
    )
    try:
        vbs.write_text(vbs_content, encoding="utf-8")
    except Exception:
        pass


def _register_autostart_linux(project_dir: Path) -> None:
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    unit = systemd_dir / "videodownloader.service"
    py = _venv_python()
    unit.write_text(
        "[Unit]\n"
        "Description=VideoDownloader supervisor\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={project_dir}\n"
        f"ExecStart={py} {project_dir / 'start.py'} --daemon --no-browser\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=8)
        subprocess.run(["systemctl", "--user", "enable", "videodownloader.service"],
                       capture_output=True, timeout=8)
    except Exception:
        # Fallback: crontab @reboot
        try:
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=8)
            lines = cur.stdout.splitlines() if cur.returncode == 0 else []
            entry = f"@reboot cd {project_dir} && {py} start.py --daemon --no-browser >/dev/null 2>&1"
            if not any("videodownloader" in l.lower() or "start.py --daemon" in l for l in lines):
                lines.append(entry)
                p = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                                   capture_output=True, text=True, timeout=8)
        except Exception:
            pass


def _register_autostart_macos(project_dir: Path) -> None:
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    plist = launch_dir / "com.videodownloader.supervisor.plist"
    py = _venv_python()
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>Label</key><string>com.videodownloader.supervisor</string>\n'
        '  <key>ProgramArguments</key>\n'
        '  <array>\n'
        f'    <string>{py}</string>\n'
        f'    <string>{project_dir / "start.py"}</string>\n'
        '    <string>--daemon</string><string>--no-browser</string>\n'
        '  </array>\n'
        f'  <key>WorkingDirectory</key><string>{project_dir}</string>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        '</dict></plist>\n',
        encoding="utf-8",
    )
    try:
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=5)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------
def _configure_firewall(node_port: int) -> None:
    if os.name != "nt":
        return
    # Delete old rule (ignore errors), then add new one.
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", "name=VideoDownloader"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             "name=VideoDownloader", "dir=in", "action=allow",
             "protocol=TCP", f"localport={node_port}", "profile=any"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            _print(f"{C.YEL}Could not add firewall rule automatically. Run this in an "
                   f"Administrator PowerShell:{C.RESET}")
            _print(f"  netsh advfirewall firewall add rule name=VideoDownloader dir=in "
                   f"action=allow protocol=TCP localport={node_port} profile=any")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Subprocess launcher helpers
# ---------------------------------------------------------------------------
def _popen_flags_detached() -> dict:
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        breakaway = 0x01000000
        flags |= breakaway
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def _popen_flags_child() -> dict:
    """Detached from the supervisor's console (daemon children)."""
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def _spawn_supervisor(project_dir: Path, node_port: int, flask_port: int,
                      download_dir: str, ffmpeg: str) -> int:
    env = os.environ.copy()
    env["VD_NODE_PORT"] = str(node_port)
    env["VD_FLASK_PORT"] = str(flask_port)
    env["VD_PROJECT_DIR"] = str(project_dir)
    env["VD_DOWNLOAD_DIR"] = str(download_dir)
    env["VD_FFMPEG"] = str(ffmpeg)
    env["PYTHONIOENCODING"] = "utf-8"

    logf = open(SUPERVISOR_LOG, "a", encoding="utf-8")
    cmd = [sys.executable, str(project_dir / "start.py"), "--daemon"]
    flags = _popen_flags_detached()
    try:
        proc = subprocess.Popen(cmd, cwd=str(project_dir), env=env,
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, **flags)
    except OSError:
        # Retry without breakaway
        if os.name == "nt":
            flags = {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 8)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200),
                     "close_fds": True}
            proc = subprocess.Popen(cmd, cwd=str(project_dir), env=env,
                                    stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, **flags)
        else:
            raise
    return proc.pid


# ---------------------------------------------------------------------------
# Launcher flow (default mode)
# ---------------------------------------------------------------------------
def _already_running() -> dict | None:
    pids = _read_pids()
    sup = int(pids.get("supervisor_pid") or 0)
    if not sup or not _pid_alive(sup):
        return None
    if not _is_our_process(sup, SUPERVISOR_CMD_MARK):
        return None
    return pids


def _show_status_and_exit(pids: dict, download_dir: str) -> int:
    node_port = int(pids.get("node_port") or NODE_PORT_DEFAULT)
    flask_port = int(pids.get("flask_port") or FLASK_PORT_DEFAULT)
    net = _fetch_network_from_flask(flask_port)
    if net:
        addresses = net.get("addresses") or []
        primary = None
        for a in addresses:
            if a.get("primary"):
                primary = a.get("ip")
                break
        _print_links_box(node_port, download_dir, addresses, primary)
    else:
        primary_ip = _primary_lan_ip()
        _print_links_box(node_port, download_dir, None, primary_ip)
    _lan_self_test(node_port, _primary_lan_ip())
    return 0


def run_default(args: argparse.Namespace) -> int:
    # Change cwd to project folder first
    try:
        os.chdir(PROJECT_DIR)
    except Exception:
        pass

    if not _check_python():
        return 1

    if args.status:
        pids = _already_running()
        if not pids:
            _print(f"{C.YEL}VideoDownloader is not running.{C.RESET}")
            _print("Start it with: python start.py")
            return 0
        return _show_status_and_exit(pids, _read_pids_download_dir(pids))

    node_path = _check_node()
    if not node_path:
        return 1

    # Truncate oversized logs
    for lf in (SUPERVISOR_LOG, FLASK_LOG, NODE_LOG):
        _truncate_log(lf)

    running = _already_running()
    if running:
        node_port = int(running.get("node_port") or NODE_PORT_DEFAULT)
        _print(f"{C.GRN}VideoDownloader is already running.{C.RESET}")
        download_dir = _read_pids_download_dir(running)
        # Try to refresh the network from Flask
        net = _fetch_network_from_flask(int(running.get("flask_port") or FLASK_PORT_DEFAULT))
        if net:
            addresses = net.get("addresses") or []
            primary = next((a.get("ip") for a in addresses if a.get("primary")), None)
            _print_links_box(node_port, download_dir, addresses, primary)
            _lan_self_test(node_port, primary)
        else:
            primary_ip = _primary_lan_ip()
            _print_links_box(node_port, download_dir, None, primary_ip)
            _lan_self_test(node_port, primary_ip)
        if not args.no_browser:
            try:
                webbrowser.open(f"http://localhost:{node_port}")
            except Exception:
                pass
        return 0

    # Fresh start
    try:
        node_port, flask_port = _pick_ports(_read_pids())
    except RuntimeError as e:
        _print(f"{C.RED}{e}{C.RESET}")
        return 1

    # venv + deps
    if not _ensure_venv():
        return 1
    py = _venv_python()
    if not _install_deps(py):
        return 1

    _try_update_ytdlp(py)

    ffmpeg = _resolve_ffmpeg(py)
    if not ffmpeg:
        _print(f"{C.YEL}Warning: ffmpeg not found — video merging may fail. "
               f"Install ffmpeg or ensure imageio-ffmpeg installed correctly.{C.RESET}")
        ffmpeg = "ffmpeg"

    # Download directory
    download_dir = _detect_downloads_stdlib()

    # Firewall
    _configure_firewall(node_port)

    # Launch supervisor
    _print(f"{C.CYN}• Starting background supervisor…{C.RESET}")
    try:
        sup_pid = _spawn_supervisor(PROJECT_DIR, node_port, flask_port, str(download_dir), str(ffmpeg))
    except Exception as e:
        _print(f"{C.RED}Could not start supervisor: {e}{C.RESET}")
        return 1

    # Wait for health
    if not _wait_for_health(node_port, 40):
        _print(f"{C.RED}The app did not become healthy in time.{C.RESET}")
        _print(f"{C.DIM}--- flask.log (tail) ---{C.RESET}")
        _print(_tail_log(FLASK_LOG, 20))
        _print(f"{C.DIM}--- node.log (tail) ---{C.RESET}")
        _print(_tail_log(NODE_LOG, 20))
        return 1

    # Auto-start
    if not args.no_autostart:
        _register_autostart(PROJECT_DIR)

    # Show links box + self-test
    net = _fetch_network_from_flask(flask_port)
    if net:
        addresses = net.get("addresses") or []
        primary = next((a.get("ip") for a in addresses if a.get("primary")), None)
        _print_links_box(node_port, str(download_dir), addresses, primary)
        _lan_self_test(node_port, primary)
    else:
        primary_ip = _primary_lan_ip()
        _print_links_box(node_port, str(download_dir), None, primary_ip)
        _lan_self_test(node_port, primary_ip)

    if not args.no_browser:
        try:
            webbrowser.open(f"http://localhost:{node_port}")
        except Exception:
            pass

    return 0


def _read_pids_download_dir(pids: dict) -> str:
    d = pids.get("download_dir")
    if d:
        return d
    return str(_detect_downloads_stdlib())


def _detect_downloads_stdlib() -> Path:
    """Detect downloads folder without third-party libs."""
    env = os.environ.get("VD_DOWNLOAD_DIR")
    if env:
        p = Path(env).expanduser()
        try:
            (p / "VideoDownloader").mkdir(parents=True, exist_ok=True)
            return p / "VideoDownloader"
        except Exception:
            pass

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            guid = GUID(0x374DE290, 0x123F, 0x4565,
                        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            fn = ctypes.windll.shell32.SHGetKnownFolderPath
            fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                           ctypes.POINTER(ctypes.c_wchar_p)]
            fn.restype = ctypes.c_long
            ptr = ctypes.c_wchar_p()
            if fn(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0 and ptr.value:
                base = Path(ptr.value)
                try:
                    ctypes.windll.ole32.CoTaskMemFree(ptr)
                except Exception:
                    pass
                if base.exists():
                    out = base / "VideoDownloader"
                    out.mkdir(parents=True, exist_ok=True)
                    return out
        except Exception:
            pass

    elif sys.platform == "darwin":
        base = Path.home() / "Downloads"
        out = base / "VideoDownloader"
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return out

    else:
        try:
            r = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                base = Path(r.stdout.strip())
                out = base / "VideoDownloader"
                out.mkdir(parents=True, exist_ok=True)
                return out
        except Exception:
            pass

    base = Path.home() / "Downloads"
    out = base / "VideoDownloader"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Daemon mode — supervisor loop
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, node_port: int, flask_port: int, download_dir: str, ffmpeg: str):
        self.project_dir = PROJECT_DIR
        self.node_port = node_port
        self.flask_port = flask_port
        self.download_dir = download_dir
        self.ffmpeg = ffmpeg
        self.stop_requested = False
        self.node_proc: subprocess.Popen | None = None
        self.flask_proc: subprocess.Popen | None = None
        self.node_backoff = 1.0
        self.flask_backoff = 1.0
        self.node_started_at = 0.0
        self.flask_started_at = 0.0
        self._lock = __import__("threading").Lock()
        self._node_log = None
        self._flask_log = None

    def _env(self) -> dict:
        env = os.environ.copy()
        env["VD_NODE_PORT"] = str(self.node_port)
        env["VD_FLASK_PORT"] = str(self.flask_port)
        env["VD_PROJECT_DIR"] = str(self.project_dir)
        env["VD_DOWNLOAD_DIR"] = str(self.download_dir)
        env["VD_FFMPEG"] = str(self.ffmpeg)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _open_logs(self) -> None:
        if self._node_log is None:
            self._node_log = open(NODE_LOG, "a", encoding="utf-8")
        if self._flask_log is None:
            self._flask_log = open(FLASK_LOG, "a", encoding="utf-8")

    def _start_node(self) -> None:
        self._open_logs()
        cmd = ["node", str(self.project_dir / "server.js")]
        flags = _popen_flags_child()
        self.node_proc = subprocess.Popen(
            cmd, cwd=str(self.project_dir), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=self._node_log, stderr=self._node_log,
            **flags,
        )
        self.node_started_at = time.time()
        self._write_pids()

    def _start_flask(self) -> None:
        self._open_logs()
        py = _venv_python()
        cmd = [str(py), str(self.project_dir / "app.py")]
        flags = _popen_flags_child()
        self.flask_proc = subprocess.Popen(
            cmd, cwd=str(self.project_dir), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=self._flask_log, stderr=self._flask_log,
            **flags,
        )
        self.flask_started_at = time.time()
        self._write_pids()

    def _write_pids(self) -> None:
        with self._lock:
            data = {
                "supervisor_pid": os.getpid(),
                "node_pid": self.node_proc.pid if self.node_proc else 0,
                "flask_pid": self.flask_proc.pid if self.flask_proc else 0,
                "node_port": self.node_port,
                "flask_port": self.flask_port,
                "started_at": time.time(),
                "download_dir": str(self.download_dir),
            }
        _write_pids(data)

    def run(self) -> int:
        self._install_signal_handlers()
        # Single-instance lock
        existing = _already_running()
        if existing and int(existing.get("supervisor_pid") or 0) != os.getpid():
            # Another supervisor is running. Refuse to start a duplicate.
            _print(f"{C.YEL}Another supervisor is already running (pid "
                   f"{existing.get('supervisor_pid')}). Exiting.{C.RESET}")
            return 0

        _print(f"{C.CYN}• Supervisor starting (node:{self.node_port}, flask:{self.flask_port}){C.RESET}")
        self._start_flask()
        self._start_node()

        try:
            while not self.stop_requested:
                time.sleep(2.0)
                now = time.time()

                # --- Flask ---
                if self.flask_proc is not None and self.flask_proc.poll() is not None:
                    # exited
                    stable = (now - self.flask_started_at) > 60
                    if stable:
                        self.flask_backoff = 1.0
                    delay = self.flask_backoff
                    self.flask_backoff = min(self.flask_backoff * 2, 30.0)
                    _print(f"{C.YEL}• Flask exited (rc={self.flask_proc.returncode}); "
                           f"restarting in {delay:.0f}s…{C.RESET}")
                    time.sleep(delay)
                    if self.stop_requested:
                        break
                    try:
                        self._start_flask()
                    except Exception as e:
                        _print(f"{C.RED}• Flask restart failed: {e}{C.RESET}")

                # --- Node ---
                if self.node_proc is not None and self.node_proc.poll() is not None:
                    stable = (now - self.node_started_at) > 60
                    if stable:
                        self.node_backoff = 1.0
                    delay = self.node_backoff
                    self.node_backoff = min(self.node_backoff * 2, 30.0)
                    _print(f"{C.YEL}• Node exited (rc={self.node_proc.returncode}); "
                           f"restarting in {delay:.0f}s…{C.RESET}")
                    time.sleep(delay)
                    if self.stop_requested:
                        break
                    try:
                        self._start_node()
                    except Exception as e:
                        _print(f"{C.RED}• Node restart failed: {e}{C.RESET}")

                # Keep .pids.json fresh
                self._write_pids()
        finally:
            self._shutdown_children()

        # Cleanup .pids.json on purpose exit
        _delete_pids()
        return 0

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            self.stop_requested = True
        try:
            signal.signal(signal.SIGTERM, handler)
            signal.signal(signal.SIGINT, handler)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, handler)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _shutdown_children(self) -> None:
        _print(f"{C.CYN}• Supervisor shutting down children…{C.RESET}")
        for proc in (self.node_proc, self.flask_proc):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       capture_output=True, timeout=8,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    else:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            try:
                                proc.terminate()
                            except Exception:
                                pass
            except Exception:
                pass
        try:
            if self._node_log:
                self._node_log.close()
        except Exception:
            pass
        try:
            if self._flask_log:
                self._flask_log.close()
        except Exception:
            pass


def run_daemon(args: argparse.Namespace) -> int:
    try:
        os.chdir(PROJECT_DIR)
    except Exception:
        pass

    pids = _read_pids()
    node_port = int(os.environ.get("VD_NODE_PORT") or pids.get("node_port") or NODE_PORT_DEFAULT)
    flask_port = int(os.environ.get("VD_FLASK_PORT") or pids.get("flask_port") or FLASK_PORT_DEFAULT)
    download_dir = os.environ.get("VD_DOWNLOAD_DIR") or str(_detect_downloads_stdlib())
    ffmpeg = os.environ.get("VD_FFMPEG") or "ffmpeg"

    sup = Supervisor(node_port, flask_port, download_dir, ffmpeg)
    return sup.run()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True, description="VideoDownloader launcher")
    p.add_argument("--daemon", action="store_true", help="Run as the supervisor daemon.")
    p.add_argument("--no-browser", action="store_true", help="Do not open a browser tab.")
    p.add_argument("--no-autostart", action="store_true", help="Do not register auto-start.")
    p.add_argument("--status", action="store_true", help="Print the links box and exit.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _enable_utf8()
    _enable_ansi_windows()
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.daemon:
        return run_daemon(args)
    return run_default(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)