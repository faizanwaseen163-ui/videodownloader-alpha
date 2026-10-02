# -*- coding: utf-8 -*-
"""
VideoDownloader — Flask backend.
Runs inside .venv, threaded, on 0.0.0.0:FLASK_PORT (default 7422).

Includes:
  - Speed-tuned yt-dlp options (8 parallel fragments, no chunk size, android_vr client)
  - /api/qr endpoint that emits SVG (pure Python qrcode — no Pillow dependency)
  - Full job engine, SSE, mDNS, network watchdog
"""
from __future__ import annotations

import errno
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# --- Flask ---
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from flask_cors import CORS

# --- Third-party (declared in requirements.txt) ---
import psutil
import requests

try:
    from zeroconf import Zeroconf, ServiceInfo
    _HAS_ZEROCONF = True
except Exception:
    Zeroconf = None  # type: ignore
    ServiceInfo = None  # type: ignore
    _HAS_ZEROCONF = False

import yt_dlp
from yt_dlp.utils import DownloadError, DownloadCancelled

# ---------------------------------------------------------------------------
# Configuration & environment
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(os.environ.get("VD_PROJECT_DIR") or Path(__file__).resolve().parent)
FLASK_PORT = int(os.environ.get("VD_FLASK_PORT") or "7422")
NODE_PORT = int(os.environ.get("VD_NODE_PORT") or "7421")
FFMPEG_PATH = os.environ.get("VD_FFMPEG") or "ffmpeg"
DOWNLOAD_DIR_ENV = os.environ.get("VD_DOWNLOAD_DIR")

LOG_FILE = PROJECT_DIR / "flask.log"
HISTORY_FILE = PROJECT_DIR / "history.json"
HISTORY_CORRUPT = PROJECT_DIR / "history.corrupt.json"
TMP_ROOT_NAME = ".tmp"
MAX_BODY_BYTES = 64 * 1024
MAX_CONCURRENT_DOWNLOADS = 2
INFO_CONCURRENCY_PER_IP = 1
SSE_HEARTBEAT_S = 15
TMP_MAX_AGE_DAYS = 7

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("videodownloader.app")

# ---------------------------------------------------------------------------
# Download directory detection (Windows SHGetKnownFolderPath / XDG / macOS)
# ---------------------------------------------------------------------------
def _detect_downloads_dir() -> Path:
    if DOWNLOAD_DIR_ENV:
        p = Path(DOWNLOAD_DIR_ENV).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    if sys.platform.startswith("win"):
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

            SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
            SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            SHGetKnownFolderPath.restype = ctypes.c_long

            path_ptr = ctypes.c_wchar_p()
            res = SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
            if res == 0 and path_ptr.value:
                p = Path(path_ptr.value)
                try:
                    ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                except Exception:
                    pass
                if p.exists():
                    out = p / "VideoDownloader"
                    out.mkdir(parents=True, exist_ok=True)
                    return out
        except Exception as e:
            log.warning("SHGetKnownFolderPath failed: %s", e)

    elif sys.platform == "darwin":
        p = Path.home() / "Downloads"
        if p.exists():
            out = p / "VideoDownloader"
            out.mkdir(parents=True, exist_ok=True)
            return out

    else:  # Linux/other
        try:
            r = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                p = Path(r.stdout.strip())
                if p.exists():
                    out = p / "VideoDownloader"
                    out.mkdir(parents=True, exist_ok=True)
                    return out
        except Exception:
            pass

    p = Path.home() / "Downloads"
    p.mkdir(parents=True, exist_ok=True)
    out = p / "VideoDownloader"
    out.mkdir(parents=True, exist_ok=True)
    return out


DOWNLOAD_DIR = _detect_downloads_dir()
TMP_ROOT = DOWNLOAD_DIR / TMP_ROOT_NAME
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
_YOUTUBE_RE = re.compile(
    r"^https?://("
    r"(www\.|m\.|music\.)?youtube\.com/(watch\?|shorts/|embed/|live/|v/)"
    r"|youtu\.be/"
    r")",
    re.IGNORECASE,
)

def is_valid_youtube_url(url: str) -> bool:
    if not url or len(url) > 2048:
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    return bool(_YOUTUBE_RE.match(url))

# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------
VIRTUAL_IFACE_HINTS = ("docker", "vbox", "vmware", "vethernet", "br-", "veth", "tun", "tap", "hyper-v")

def get_primary_ip() -> Optional[str]:
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

def collect_addresses() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    primary = get_primary_ip()
    out: List[Dict[str, Any]] = []
    seen = set()
    try:
        ifaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception:
        ifaces, stats = {}, {}

    for iface, addrs in ifaces.items():
        st = stats.get(iface)
        if st is not None and not st.isup:
            continue
        low = iface.lower()
        is_virt = any(h in low for h in VIRTUAL_IFACE_HINTS)
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = a.address
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip in seen:
                continue
            seen.add(ip)
            out.append({
                "iface": iface,
                "ip": ip,
                "url": f"http://{ip}:{NODE_PORT}",
                "primary": (ip == primary),
                "virtual": is_virt,
            })

    if not primary and out:
        for a in out:
            if not a["virtual"]:
                a["primary"] = True
                primary = a["ip"]
                break
        if not primary:
            out[0]["primary"] = True
            primary = out[0]["ip"]

    out.sort(key=lambda a: (not a["primary"], a["virtual"], a["iface"]))
    return out, primary

def get_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"

# ---------------------------------------------------------------------------
# Atomic JSON helpers
# ---------------------------------------------------------------------------
def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class History:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._items: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                return
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for it in data:
                        if isinstance(it, dict) and it.get("id"):
                            self._items[it["id"]] = it
                elif isinstance(data, dict) and "items" in data:
                    for it in data["items"]:
                        if isinstance(it, dict) and it.get("id"):
                            self._items[it["id"]] = it
            except Exception as e:
                log.error("history.json corrupt: %s — backing up", e)
                try:
                    shutil.copy2(self._path, HISTORY_CORRUPT)
                except Exception:
                    pass
                self._items = OrderedDict()

    def _persist(self) -> None:
        items = list(self._items.values())
        atomic_write_json(self._path, items)

    def _refresh_exists(self, it: Dict[str, Any]) -> None:
        p = it.get("path")
        it["exists"] = bool(p and Path(p).exists())

    def add(self, item: Dict[str, Any]) -> None:
        with self._lock:
            self._items[item["id"]] = item
            self._persist()

    def remove(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            it = self._items.pop(item_id, None)
            if it is not None:
                self._persist()
            return it

    def get(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._items.get(item_id)

    def list(self, q: str = "", type_: str = "all", sort: str = "newest") -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._items.values())
            for it in items:
                self._refresh_exists(it)
        ql = (q or "").lower().strip()
        if ql:
            items = [i for i in items if ql in (i.get("title") or "").lower()
                     or ql in (i.get("channel") or "").lower()
                     or ql in (i.get("filename") or "").lower()]
        if type_ == "video":
            items = [i for i in items if i.get("kind") == "video"]
        elif type_ == "audio":
            items = [i for i in items if i.get("kind") == "audio"]
        if sort == "newest":
            items.sort(key=lambda i: i.get("created_at") or 0, reverse=True)
        elif sort == "oldest":
            items.sort(key=lambda i: i.get("created_at") or 0)
        elif sort == "size":
            items.sort(key=lambda i: i.get("size_bytes") or 0, reverse=True)
        elif sort == "name":
            items.sort(key=lambda i: (i.get("title") or "").lower())
        return items

    def count(self) -> int:
        with self._lock:
            return len(self._items)

history = History(HISTORY_FILE)

# ---------------------------------------------------------------------------
# SSE broadcaster
# ---------------------------------------------------------------------------
class SSEHub:
    def __init__(self):
        self._subs: "List[queue.Queue]" = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, data: Any) -> None:
        payload = (event, data)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

sse = SSEHub()

# ---------------------------------------------------------------------------
# Job engine
# ---------------------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_STARTING = "starting"
STATUS_DOWNLOADING = "downloading"
STATUS_MERGING = "merging"
STATUS_FINISHED = "finished"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

class Job:
    def __init__(self, url: str, kind: str, quality_key: str, container: str, audio_key: str):
        self.id = uuid.uuid4().hex
        self.url = url
        self.kind = kind
        self.quality_key = quality_key
        self.container = container or "mkv"
        self.audio_key = audio_key or "mp3-192"

        self.video_id = ""
        self.title = ""
        self.channel = ""
        self.thumbnail = ""
        self.quality_label = ""
        self.fps = 0

        self.status = STATUS_QUEUED
        self.stage = "video"
        self.percent = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.speed_bps = 0.0
        self.eta_s: Optional[float] = None
        self.filename = ""
        self.error: Optional[Dict[str, str]] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

        self._cancel_evt = threading.Event()
        self._lock = threading.RLock()
        self._last_emit = 0.0
        self._streams: Dict[str, Dict[str, Any]] = {}

    def cancel(self) -> None:
        self._cancel_evt.set()

    def is_cancelled(self) -> bool:
        return self._cancel_evt.is_set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "video_id": self.video_id,
                "title": self.title,
                "channel": self.channel,
                "thumbnail": self.thumbnail,
                "kind": self.kind,
                "quality_label": self.quality_label,
                "fps": self.fps,
                "container": self.container,
                "status": self.status,
                "stage": self.stage,
                "percent": round(self.percent, 2),
                "downloaded_bytes": int(self.downloaded_bytes),
                "total_bytes": int(self.total_bytes),
                "speed_bps": round(self.speed_bps, 1),
                "eta_s": int(self.eta_s) if self.eta_s is not None else None,
                "filename": self.filename,
                "error": dict(self.error) if self.error else None,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
            }

    def emit(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and (now - self._last_emit) < 0.25:
                return
            self._last_emit = now
            snap = self.snapshot()
        sse.publish("job", snap)

class JobManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._workers: List[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for i in range(MAX_CONCURRENT_DOWNLOADS):
                t = threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True)
                t.start()
                self._workers.append(t)

    def submit(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
        job.emit(force=True)
        self._queue.put(job.id)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [j.snapshot() for j in self._jobs.values()]

    def _worker(self) -> None:
        while True:
            try:
                jid = self._queue.get()
            except Exception:
                continue
            job = self.get(jid)
            if not job:
                continue
            try:
                self._run_job(job)
            except Exception as e:
                log.exception("job worker crashed for %s: %s", jid, e)

    # ----- actual download -----
    def _run_job(self, job: Job) -> None:
        if job.is_cancelled():
            self._finish_cancelled(job)
            return
        self._set_status(job, STATUS_STARTING)

        try:
            info = self._extract_info(job, download=True)
            if job.is_cancelled():
                self._cleanup_tmp(job)
                self._finish_cancelled(job)
                return
            if info is None:
                if job.is_cancelled():
                    self._cleanup_tmp(job)
                    self._finish_cancelled(job)
                else:
                    self._set_error(job, "UNKNOWN", "Download failed.")
                return
            self._finalize(job, info)
        except DownloadCancelled:
            self._cleanup_tmp(job)
            self._finish_cancelled(job)
        except DownloadError as e:
            if job.is_cancelled():
                self._cleanup_tmp(job)
                self._finish_cancelled(job)
            else:
                code, msg = map_error(str(e))
                self._set_error(job, code, msg)
        except OSError as e:
            if e.errno == errno.ENOSPC:
                self._set_error(job, "DISK_FULL", "The disk is full.")
            elif e.errno in (errno.EACCES, errno.EPERM):
                self._set_error(job, "PERMISSION", "Permission denied while writing the file.")
            else:
                self._set_error(job, "UNKNOWN", f"OS error: {e}")
        except Exception as e:
            log.exception("job %s failed: %s", job.id, e)
            code, msg = map_error(str(e))
            self._set_error(job, code, msg)

    def _set_status(self, job: Job, status: str, stage: Optional[str] = None) -> None:
        with job._lock:
            job.status = status
            if stage:
                job.stage = stage
        job.emit(force=True)

    def _set_error(self, job: Job, code: str, msg: str) -> None:
        with job._lock:
            job.status = STATUS_ERROR
            job.error = {"code": code, "message": msg}
            job.finished_at = time.time()
        job.emit(force=True)

    def _finish_cancelled(self, job: Job) -> None:
        with job._lock:
            job.status = STATUS_CANCELLED
            job.finished_at = time.time()
        job.emit(force=True)

    def _cleanup_tmp(self, job: Job) -> None:
        try:
            if job.video_id and job.quality_key:
                d = TMP_ROOT / f"{job.video_id}_{job.quality_key}"
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    def _build_format(self, job: Job) -> str:
        if job.kind == "audio":
            return "bestaudio/best"
        m = re.match(r"^(\d+)p(\d+)?$", job.quality_key or "")
        if m:
            h = int(m.group(1))
            return f"bestvideo[height<={h}]+bestaudio/best"
        return "bestvideo+bestaudio/best"

    def _ydl_opts(self, job: Job) -> Dict[str, Any]:
        tmp_dir = TMP_ROOT / f"{job.video_id or 'unknown'}_{job.quality_key or 'q'}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # ------------------------------------------------------------------
        # SPEED-TUNED OPTIONS
        # ------------------------------------------------------------------
        opts: Dict[str, Any] = {
            "ffmpeg_location": FFMPEG_PATH,
            "noplaylist": True,
            "retries": 20,
            "fragment_retries": 20,
            "file_access_retries": 5,
            "socket_timeout": 30,
            "continuedl": True,
            "concurrent_fragment_downloads": 8,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "overwrites": False,
            "geo_bypass": True,
            "windowsfilenames": True,
            "outtmpl": str(tmp_dir / "%(title).150B.%(ext)s"),
            "progress_hooks": [lambda d: self._on_progress(job, d)],
            "postprocessor_hooks": [lambda d: self._on_postprocessor(job, d)],
        }

        # Wire node as JS runtime (modern yt-dlp).
        node_path = shutil.which("node")
        if node_path:
            try:
                opts["js_runtimes"] = {"node": {"path": node_path}}
            except Exception:
                pass

        # Prefer faster YouTube CDN clients (android_vr is often faster).
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "web_safari", "web", "default"],
            }
        }

        if job.kind == "audio":
            abr = 192
            if job.audio_key == "mp3-320":
                abr = 320
            if job.audio_key == "m4a":
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
                    {"key": "FFmpegMetadata"},
                ]
            else:
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(abr)},
                    {"key": "FFmpegMetadata"},
                ]
            opts["format"] = "bestaudio/best"
        else:
            opts["format"] = self._build_format(job)
            if job.container == "mp4":
                opts["merge_output_format"] = "mp4"
            else:
                opts["merge_output_format"] = "mkv"
        return opts

    def _extract_info(self, job: Job, download: bool) -> Optional[Dict[str, Any]]:
        opts = self._ydl_opts(job)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(job.url, download=download)
        except DownloadError as e:
            msg = str(e)
            if "Unable to extract" in msg or "signature" in msg.lower() or "nsig" in msg.lower():
                log.warning("extractor/signature error, attempting yt-dlp self-update")
                try:
                    subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                                   capture_output=True, timeout=60)
                except Exception:
                    pass
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(job.url, download=download)
            raise

    def _on_progress(self, job: Job, d: Dict[str, Any]) -> None:
        if job.is_cancelled():
            raise DownloadCancelled("cancelled by user")

        fname = d.get("filename") or ""
        st = job._streams.setdefault(fname, {"downloaded": 0, "total": 0, "speed": 0.0})
        st["downloaded"] = int(d.get("downloaded_bytes") or 0)
        tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if tot:
            st["total"] = int(tot)
        sp = d.get("speed") or 0
        if sp:
            st["speed"] = float(sp)

        total_dl = sum(s["downloaded"] for s in job._streams.values())
        total_all = sum(s["total"] for s in job._streams.values() if s["total"])
        combined_speed = sum(s["speed"] for s in job._streams.values())

        with job._lock:
            alpha = 0.35
            job.speed_bps = (1 - alpha) * job.speed_bps + alpha * combined_speed if job.speed_bps else combined_speed
            job.downloaded_bytes = total_dl
            if total_all > 0:
                job.total_bytes = total_all
                pct = min(99.0, (total_dl / total_all) * 100.0)
                if pct < job.percent:
                    pct = job.percent
                job.percent = pct
            if job.speed_bps > 0 and total_all > total_dl:
                job.eta_s = (total_all - total_dl) / job.speed_bps
            job.stage = "audio" if len(job._streams) > 1 else "video"
            if job.status not in (STATUS_MERGING,):
                job.status = STATUS_DOWNLOADING
        job.emit()

    def _on_postprocessor(self, job: Job, d: Dict[str, Any]) -> None:
        if job.is_cancelled():
            raise DownloadCancelled("cancelled by user")
        status = d.get("status")
        if status == "started":
            with job._lock:
                job.status = STATUS_MERGING
                job.stage = "merging"
                job.percent = max(job.percent, 99.0)
            job.emit(force=True)
        elif status == "finished":
            with job._lock:
                job.stage = "done"
            job.emit(force=True)

    def _finalize(self, job: Job, info: Dict[str, Any]) -> None:
        filepath = None
        try:
            rd = info.get("requested_downloads")
            if rd:
                filepath = rd[0].get("filepath")
        except Exception:
            pass
        if not filepath:
            filepath = info.get("filepath") or info.get("_filename")

        if not filepath or not Path(filepath).exists():
            tmp_dir = TMP_ROOT / f"{job.video_id}_{job.quality_key}"
            if tmp_dir.exists():
                for p in sorted(tmp_dir.glob("*"), key=lambda x: x.stat().st_size, reverse=True):
                    if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus"):
                        filepath = str(p)
                        break

        if not filepath or not Path(filepath).exists():
            self._set_error(job, "UNKNOWN", "Could not locate the downloaded file.")
            return

        src = Path(filepath)
        ext = src.suffix.lstrip(".") or "mkv"

        title = job.title or info.get("title") or "video"
        label = job.quality_label or job.quality_key or "video"
        fps_suffix = "60" if (job.fps or 0) >= 50 else ""
        base = sanitize_filename(f"{title} [{label}{fps_suffix}]")
        target = unique_target(DOWNLOAD_DIR, base, ext)

        try:
            shutil.move(str(src), str(target))
        except Exception as e:
            log.exception("move failed: %s", e)
            self._set_error(job, "PERMISSION", f"Could not move file: {e}")
            return

        self._cleanup_tmp(job)

        try:
            size = target.stat().st_size
        except Exception:
            size = 0

        item = {
            "id": uuid.uuid4().hex,
            "title": job.title or info.get("title") or target.stem,
            "channel": job.channel or info.get("uploader") or "",
            "thumbnail": job.thumbnail or info.get("thumbnail") or "",
            "url": job.url,
            "kind": job.kind,
            "quality_label": job.quality_label or job.quality_key,
            "fps": job.fps,
            "container": ext,
            "size_bytes": size,
            "filename": target.name,
            "path": str(target),
            "created_at": time.time(),
            "exists": True,
        }
        history.add(item)
        sse.publish("history", {"action": "added", "item": item})

        with job._lock:
            job.status = STATUS_FINISHED
            job.stage = "done"
            job.percent = 100.0
            job.filename = target.name
            job.finished_at = time.time()
        job.emit(force=True)

jobs = JobManager()

# ---------------------------------------------------------------------------
# yt-dlp /api/info helpers
# ---------------------------------------------------------------------------
_info_sem_lock = threading.Lock()
_info_sem_per_ip: Dict[str, threading.Semaphore] = {}

def _info_sem_for(ip: str) -> threading.Semaphore:
    with _info_sem_lock:
        s = _info_sem_per_ip.get(ip)
        if s is None:
            s = threading.Semaphore(INFO_CONCURRENCY_PER_IP)
            _info_sem_per_ip[ip] = s
        return s

QUALITY_ORDER_BONUS = {"vp9": 2, "av01": 1, "avc1": 0, "h264": 0}

def build_qualities(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    formats = info.get("formats") or []
    best_audio = None
    for f in formats:
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none"):
            abr = f.get("abr") or 0
            if not best_audio or abr > (best_audio.get("abr") or 0):
                best_audio = f
    audio_size = (best_audio or {}).get("filesize") or (best_audio or {}).get("filesize_approx") or 0

    groups: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for f in formats:
        vcodec = f.get("vcodec")
        if vcodec in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        fps = int(round(f.get("fps") or 0))
        key = (h, fps)
        tbr = f.get("tbr") or 0
        vc = (f.get("vcodec") or "").split(".")[0]
        score = tbr + QUALITY_ORDER_BONUS.get(vc, 0)
        if h > 1080 and vc == "avc1":
            score -= 500
        cur = groups.get(key)
        if not cur or score > cur["score"]:
            groups[key] = {"format": f, "score": score}

    out: List[Dict[str, Any]] = []
    for (h, fps), entry in groups.items():
        f = entry["format"]
        vid_size = f.get("filesize") or f.get("filesize_approx") or 0
        total_est = (vid_size + audio_size) if vid_size else 0
        label = height_label(h)
        fps_out = fps if fps >= 50 else fps
        key = f"{h}p{fps if fps >= 50 else ''}"
        hdr = False
        dr = f.get("dynamic_range")
        if dr and dr != "SDR":
            hdr = True
        out.append({
            "key": key,
            "height": h,
            "fps": fps_out,
            "label": label,
            "hdr": hdr,
            "vcodec": f.get("vcodec") or "",
            "format_id": f.get("format_id"),
            "est_size_bytes": int(total_est),
        })

    out.sort(key=lambda q: (q["height"], q["fps"]))
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for q in out:
        k = (q["height"], q["fps"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(q)
    return uniq

def height_label(h: int) -> str:
    if h >= 2160:
        return "4K"
    if h >= 1440:
        return "1440p"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    if h >= 480:
        return "480p"
    if h >= 360:
        return "360p"
    return f"{h}p"

def duration_text(secs: Optional[int]) -> str:
    if not secs:
        return ""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def fmt_date(yyyymmdd: Optional[str]) -> str:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return ""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"

# ---------------------------------------------------------------------------
# Filename sanitizer
# ---------------------------------------------------------------------------
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *[f"COM{i}" for i in range(1, 10)],
                 *[f"LPT{i}" for i in range(1, 10)]}

def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name or "video")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if not name:
        name = "video"
    stem = name.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        name = "_" + name
    if len(name) > 120:
        name = name[:120].rstrip(". ")
    return name or "video"

def unique_target(folder: Path, base: str, ext: str) -> Path:
    target = folder / f"{base}.{ext}"
    n = 1
    while target.exists():
        target = folder / f"{base} ({n}).{ext}"
        n += 1
    return target

# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
def map_error(msg: str) -> Tuple[str, str]:
    m = (msg or "").lower()
    if "private video" in m:
        return "PRIVATE", "This video is private."
    if "members-only" in m or "members only" in m:
        return "MEMBERS_ONLY", "This video is for channel members only."
    if "age" in m and ("restrict" in m or "confirm" in m or "sign in" in m):
        return "AGE_RESTRICTED", "Age-restricted video (sign-in is not supported)."
    if "not available in your country" in m or ("geo" in m and "block" in m):
        return "REGION_BLOCKED", "This video is blocked in your region."
    if "removed" in m or "unavailable" in m or "no longer" in m:
        return "UNAVAILABLE", "This video has been removed or is unavailable."
    if "is live" in m or "live stream" in m:
        return "LIVE_NOT_SUPPORTED", "Live streams are not supported."
    if "ffmpeg" in m:
        return "FFMPEG_MISSING", "ffmpeg is missing or failed."
    if "no internet" in m or "connection" in m or "resolve" in m or "timed out" in m:
        return "NO_INTERNET", "No internet connection."
    if "requested format" in m:
        return "FORMAT_UNAVAILABLE", "The selected format is no longer available."
    return "UNKNOWN", "Download failed. Please try again."

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

START_TIME = time.time()

def yt_dlp_version() -> str:
    try:
        return getattr(yt_dlp.version, "__version__", "unknown")
    except Exception:
        return "unknown"

def ffmpeg_available() -> bool:
    try:
        r = subprocess.run([FFMPEG_PATH, "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def is_host_request() -> bool:
    xri = (request.headers.get("X-Real-IP") or "").strip()
    if xri.startswith("::ffff:"):
        xri = xri[7:]
    peer = request.remote_addr or ""
    if peer.startswith("::ffff:"):
        peer = peer[7:]
    if peer not in ("127.0.0.1", "::1", "localhost"):
        return False
    if not xri:
        return True
    return xri in ("127.0.0.1", "::1", "localhost")

def error_response(code: str, message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status

# --- global error handlers -------------------------------------------------
@app.errorhandler(404)
def _h404(_e):
    return error_response("NOT_FOUND", "Resource not found.", 404)

@app.errorhandler(405)
def _h405(_e):
    return error_response("METHOD_NOT_ALLOWED", "Method not allowed.", 405)

@app.errorhandler(500)
def _h500(_e):
    return error_response("INTERNAL", "Internal server error.", 500)

@app.errorhandler(Exception)
def _h_exc(e):
    log.exception("unhandled exception")
    return error_response("INTERNAL", str(e) or "Internal error.", 500)

@app.before_request
def _limit_body():
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.content_length or 0
        if cl > MAX_BODY_BYTES:
            return error_response("BODY_TOO_LARGE", "Request body too large.", 413)
    return None

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "service": "flask",
        "version": "1.0.0",
        "uptime_s": int(time.time() - START_TIME),
        "yt_dlp_version": yt_dlp_version(),
        "ffmpeg": ffmpeg_available(),
        "download_dir": str(DOWNLOAD_DIR),
        "is_host": is_host_request(),
    })

@app.get("/api/network")
def api_network():
    addresses, primary = collect_addresses()
    host = get_hostname()
    primary_url = None
    for a in addresses:
        if a["primary"]:
            primary_url = a["url"]
            break
    return jsonify({
        "ok": True,
        "hostname": host,
        "node_port": NODE_PORT,
        "local_url": f"http://localhost:{NODE_PORT}",
        "mdns_url": f"http://videodownloader.local:{NODE_PORT}",
        "primary_url": primary_url,
        "addresses": addresses,
    })

@app.get("/api/qr")
def api_qr():
    """Generate an SVG QR code for the given URL.
    Uses qrcode.image.svg (pure Python — no Pillow / no CDN needed)."""
    text = (request.args.get("url") or "").strip()
    if not text or len(text) > 2048:
        return error_response("INVALID_URL", "url parameter is required (max 2048).", 400)
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage)
        buf = BytesIO()
        img.save(buf)
        data = buf.getvalue()
        return Response(
            data,
            mimetype="image/svg+xml",
            headers={"Cache-Control": "public, max-age=300"},
        )
    except Exception as e:
        log.warning("QR generation failed: %s", e)
        return error_response("QR_FAILED", "Could not generate QR code.", 500)

@app.post("/api/info")
def api_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_valid_youtube_url(url):
        return error_response("INVALID_URL", "Please provide a valid YouTube URL.", 400)

    ip = request.headers.get("X-Real-IP") or request.remote_addr or "?"
    sem = _info_sem_for(ip)
    if not sem.acquire(blocking=False):
        return error_response("RATE_LIMITED", "Too many info requests, try again shortly.", 429)
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 30,
            "geo_bypass": True,
        }
        node_path = shutil.which("node")
        if node_path:
            try:
                opts["js_runtimes"] = {"node": {"path": node_path}}
            except Exception:
                pass
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "web_safari", "web", "default"],
            }
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        code, msg = map_error(str(e))
        return error_response(code, msg, 400)
    except Exception as e:
        log.exception("info failed")
        return error_response("UNKNOWN", str(e), 500)
    finally:
        try:
            sem.release()
        except Exception:
            pass

    if info.get("is_live"):
        return error_response("LIVE_NOT_SUPPORTED", "Live streams are not supported.", 400)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if entries:
            info = entries[0]

    qualities = build_qualities(info)
    video = {
        "id": info.get("id") or "",
        "title": info.get("title") or "",
        "channel": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration_s": int(info.get("duration") or 0),
        "duration_text": duration_text(info.get("duration")),
        "view_count": int(info.get("view_count") or 0),
        "upload_date": fmt_date(info.get("upload_date")),
        "is_live": bool(info.get("is_live")),
    }
    audio_options = [
        {"key": "mp3-320", "label": "MP3 320 kbps"},
        {"key": "mp3-192", "label": "MP3 192 kbps"},
        {"key": "m4a", "label": "M4A (original AAC)"},
    ]
    return jsonify({
        "ok": True,
        "video": video,
        "qualities": qualities,
        "audio_options": audio_options,
        "containers": ["mkv", "mp4"],
    })

@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    kind = (data.get("kind") or "video").strip()
    quality_key = (data.get("quality_key") or "").strip()
    container = (data.get("container") or "mkv").strip().lower()
    audio_key = (data.get("audio_key") or "mp3-192").strip()

    if not is_valid_youtube_url(url):
        return error_response("INVALID_URL", "Please provide a valid YouTube URL.", 400)
    if kind not in ("video", "audio"):
        return error_response("INVALID_KIND", "kind must be 'video' or 'audio'.", 400)
    if container not in ("mkv", "mp4"):
        container = "mkv"
    if kind == "audio" and audio_key not in ("mp3-320", "mp3-192", "m4a"):
        audio_key = "mp3-192"

    job = Job(url, kind, quality_key, container, audio_key)

    try:
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "socket_timeout": 30, "geo_bypass": True,
        }
        node_path = shutil.which("node")
        if node_path:
            try:
                opts["js_runtimes"] = {"node": {"path": node_path}}
            except Exception:
                pass
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "web_safari", "web", "default"],
            }
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if entries:
                info = entries[0]
        if info.get("is_live"):
            return error_response("LIVE_NOT_SUPPORTED", "Live streams are not supported.", 400)
        job.video_id = info.get("id") or ""
        job.title = info.get("title") or ""
        job.channel = info.get("uploader") or info.get("channel") or ""
        job.thumbnail = info.get("thumbnail") or ""
        if kind == "audio":
            job.quality_label = "Audio"
            job.fps = 0
        else:
            m = re.match(r"^(\d+)p(\d+)?$", quality_key or "")
            if m:
                h = int(m.group(1))
                job.quality_label = height_label(h)
                fps = int(m.group(2) or 0)
                job.fps = fps if fps >= 50 else fps
            else:
                job.quality_label = quality_key or "video"
                job.fps = 0
    except DownloadError as e:
        code, msg = map_error(str(e))
        return error_response(code, msg, 400)
    except Exception as e:
        log.exception("download pre-info failed")
        return error_response("UNKNOWN", str(e), 500)

    jobs.submit(job)
    return jsonify({"ok": True, "job": job.snapshot()}), 202

@app.get("/api/jobs")
def api_jobs():
    return jsonify({"ok": True, "jobs": jobs.list()})

@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return error_response("NOT_FOUND", "Job not found.", 404)
    job.cancel()
    return jsonify({"ok": True, "job": job.snapshot()})

@app.post("/api/jobs/<job_id>/retry")
def api_job_retry(job_id: str):
    old = jobs.get(job_id)
    if not old:
        return error_response("NOT_FOUND", "Job not found.", 404)
    new_job = Job(old.url, old.kind, old.quality_key, old.container, old.audio_key)
    new_job.video_id = old.video_id
    new_job.title = old.title
    new_job.channel = old.channel
    new_job.thumbnail = old.thumbnail
    new_job.quality_label = old.quality_label
    new_job.fps = old.fps
    jobs.submit(new_job)
    return jsonify({"ok": True, "job": new_job.snapshot()})

@app.get("/api/history")
def api_history():
    q = request.args.get("q", "")
    type_ = request.args.get("type", "all")
    sort = request.args.get("sort", "newest")
    items = history.list(q=q, type_=type_, sort=sort)
    total = sum((i.get("size_bytes") or 0) for i in items)
    return jsonify({
        "ok": True,
        "items": items,
        "count": len(items),
        "total_size_bytes": total,
    })

@app.delete("/api/history/<item_id>")
def api_history_delete(item_id: str):
    it = history.get(item_id)
    if not it:
        return error_response("NOT_FOUND", "Item not found.", 404)
    delete_file = (request.args.get("delete_file", "false").lower() == "true")
    if delete_file:
        if not is_host_request():
            return error_response("HOST_ONLY", "Only the host PC can delete files.", 403)
        try:
            p = Path(it["path"]).resolve()
            if not _path_inside(p, DOWNLOAD_DIR.resolve()):
                return error_response("FORBIDDEN", "Path outside download folder.", 403)
            if p.exists():
                p.unlink()
        except Exception as e:
            log.warning("delete file failed: %s", e)
    history.remove(item_id)
    sse.publish("history", {"action": "removed", "item": it})
    return jsonify({"ok": True})

@app.post("/api/history/<item_id>/open")
def api_history_open(item_id: str):
    if not is_host_request():
        return error_response("HOST_ONLY", "Only the host PC can open files.", 403)
    it = history.get(item_id)
    if not it:
        return error_response("NOT_FOUND", "Item not found.", 404)
    p = Path(it["path"])
    if not p.exists():
        return error_response("FILE_MISSING", "File is missing on disk.", 404)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        return error_response("OPEN_FAILED", str(e), 500)
    return jsonify({"ok": True})

@app.post("/api/history/<item_id>/open-folder")
def api_history_open_folder(item_id: str):
    if not is_host_request():
        return error_response("HOST_ONLY", "Only the host PC can open folders.", 403)
    it = history.get(item_id)
    if not it:
        return error_response("NOT_FOUND", "Item not found.", 404)
    p = Path(it["path"])
    folder = p.parent
    if not folder.exists():
        return error_response("FILE_MISSING", "Folder is missing.", 404)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as e:
        return error_response("OPEN_FAILED", str(e), 500)
    return jsonify({"ok": True})

def _path_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False

@app.get("/api/file/<item_id>")
def api_file(item_id: str):
    it = history.get(item_id)
    if not it:
        return error_response("NOT_FOUND", "Item not found.", 404)
    p = Path(it["path"])
    try:
        rp = p.resolve(strict=True)
    except FileNotFoundError:
        return error_response("FILE_MISSING", "File no longer exists.", 404)
    if not _path_inside(rp, DOWNLOAD_DIR.resolve()):
        return error_response("FORBIDDEN", "Access denied.", 403)

    resp = send_file(
        str(rp),
        as_attachment=True,
        download_name=rp.name,
        conditional=True,
    )
    return resp

# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
@app.get("/api/events")
def api_events():
    def gen():
        q = sse.subscribe()
        try:
            addresses, primary = collect_addresses()
            primary_url = next((a["url"] for a in addresses if a["primary"]), None)
            network = {
                "hostname": get_hostname(),
                "node_port": NODE_PORT,
                "local_url": f"http://localhost:{NODE_PORT}",
                "mdns_url": f"http://videodownloader.local:{NODE_PORT}",
                "primary_url": primary_url,
                "addresses": addresses,
            }
            snap = {"jobs": jobs.list(), "network": network, "history_count": history.count()}
            yield "event: snapshot\ndata: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
            last_hb = time.time()
            while True:
                try:
                    evt, data = q.get(timeout=1.0)
                    yield f"event: {evt}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    now = time.time()
                    if now - last_hb >= SSE_HEARTBEAT_S:
                        yield ": ping\n\n"
                        last_hb = now
        except GeneratorExit:
            pass
        finally:
            sse.unsubscribe(q)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), mimetype="text/event-stream", headers=headers)

# ---------------------------------------------------------------------------
# Network watchdog + mDNS
# ---------------------------------------------------------------------------
class NetworkWatchdog(threading.Thread):
    def __init__(self):
        super().__init__(name="net-watchdog", daemon=True)
        self._last_sig = None
        self._last_time = time.time()
        self._zc: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None

    def _signature(self, addresses: List[Dict[str, Any]]) -> Tuple:
        return tuple((a["iface"], a["ip"], a["primary"]) for a in addresses)

    def run(self):
        while True:
            try:
                addresses, primary = collect_addresses()
                sig = self._signature(addresses)
                if sig != self._last_sig:
                    self._last_sig = sig
                    primary_url = next((a["url"] for a in addresses if a["primary"]), None)
                    net = {
                        "hostname": get_hostname(),
                        "node_port": NODE_PORT,
                        "local_url": f"http://localhost:{NODE_PORT}",
                        "mdns_url": f"http://videodownloader.local:{NODE_PORT}",
                        "primary_url": primary_url,
                        "addresses": addresses,
                    }
                    sse.publish("network", net)
                    self._re_register_mdns(addresses)
                now = time.time()
                if now - self._last_time > 30:
                    self._re_register_mdns(addresses)
                self._last_time = now
            except Exception as e:
                log.debug("net watchdog error: %s", e)
            time.sleep(3)

    def _re_register_mdns(self, addresses: List[Dict[str, Any]]) -> None:
        if not _HAS_ZEROCONF:
            return
        try:
            if self._zc is None:
                self._zc = Zeroconf()
            ips = [socket.inet_aton(a["ip"]) for a in addresses]
            if not ips:
                return
            host = get_hostname()
            info = ServiceInfo(
                "_http._tcp.local.",
                "VideoDownloader._http._tcp.local.",
                addresses=ips,
                port=NODE_PORT,
                properties={"path": "/"},
                server=f"{host}.local.",
            )
            try:
                if self._info is not None:
                    self._zc.update_service(info)
                else:
                    self._zc.register_service(info)
            except Exception:
                try:
                    self._zc.unregister_service(self._info)
                except Exception:
                    pass
                self._zc.register_service(info)
            self._info = info
        except Exception as e:
            log.debug("mDNS register failed: %s", e)

# ---------------------------------------------------------------------------
# Startup maintenance
# ---------------------------------------------------------------------------
def cleanup_old_tmp():
    try:
        cutoff = time.time() - TMP_MAX_AGE_DAYS * 86400
        for d in TMP_ROOT.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    cleanup_old_tmp()
    jobs.start()
    NetworkWatchdog().start()
    log.info("Flask starting on 0.0.0.0:%d, downloads → %s", FLASK_PORT, DOWNLOAD_DIR)
    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()