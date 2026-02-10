#!/usr/bin/env python3
import errno
import json
import os
import errno
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codex_uptime import compute_uptime

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
SOURCES_FILE = DATA_DIR / "sources.json"
SOURCES_DIR = DATA_DIR / "sources"
LOCAL_SESSIONS_DIR = Path(
    os.environ.get("CODEX_SESSIONS_DIR", str(Path.home() / ".codex" / "sessions"))
).expanduser()
INCLUDE_LOCAL = os.environ.get("CODEX_INCLUDE_LOCAL", "1") != "0"
DEFAULT_HOST = os.environ.get("CODEX_UPTIME_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CODEX_UPTIME_PORT", "8008"))
MAX_PORT_ATTEMPTS = int(os.environ.get("CODEX_PORT_RETRY_COUNT", "20"))


def content_type_for(path: Path) -> str:
    if path.suffix == ".html":
        return "text/html; charset=utf-8"
    if path.suffix == ".css":
        return "text/css; charset=utf-8"
    if path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    if path.suffix == ".json":
        return "application/json; charset=utf-8"
    return "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/uptime":
            self.handle_api(parsed)
            return
        if parsed.path == "/api/sources":
            self.handle_sources_list()
            return
        self.handle_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sources":
            self.handle_sources_create()
            return
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "sources" and parts[3] == "sync":
            self.handle_sources_sync(parts[2])
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "sources":
            self.handle_sources_delete(parts[2])
            return
        self.send_error(404)

    def handle_api(self, parsed) -> None:
        params = parse_qs(parsed.query or "")
        window = params.get("window", ["all"])[0]
        start = params.get("start", [""])[0]
        end = params.get("end", [""])[0]
        granularity = params.get("granularity", [""])[0]
        try:
            result = compute_uptime(
                roots=get_session_roots(),
                window=window,
                start=start,
                end=end,
                granularity=granularity,
            )
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        self.send_json(result, status=200)

    def handle_sources_list(self) -> None:
        sources = sanitize_sources(load_sources())
        self.send_json({"sources": sources, "include_local": INCLUDE_LOCAL}, status=200)

    def handle_sources_create(self) -> None:
        payload = self.read_json()
        if not payload:
            self.send_json({"error": "Missing JSON body."}, status=400)
            return
        host = str(payload.get("host") or "").strip()
        user = str(payload.get("user") or "").strip()
        password = str(payload.get("password") or "")
        label = str(payload.get("label") or "").strip() or host
        path = str(payload.get("path") or "").strip() or "~/.codex/sessions"
        port = int(payload.get("port") or 22)
        if not host or not user or not password:
            self.send_json({"error": "host, user, and password are required."}, status=400)
            return

        sources = load_sources()
        source_id = uuid.uuid4().hex[:10]
        sources.append(
            {
                "id": source_id,
                "label": label,
                "host": host,
                "user": user,
                "port": port,
                "path": path,
                "password": password,
                "last_sync": None,
                "last_error": None,
            }
        )
        save_sources(sources)
        self.send_json({"id": source_id}, status=201)

    def handle_sources_sync(self, source_id: str) -> None:
        sources = load_sources()
        source = next((item for item in sources if item.get("id") == source_id), None)
        if not source:
            self.send_json({"error": "Source not found."}, status=404)
            return

        try:
            sync_source(source)
            source["last_sync"] = datetime.now(timezone.utc).isoformat()
            source["last_error"] = None
            save_sources(sources)
            self.send_json({"ok": True}, status=200)
        except Exception as exc:
            source["last_error"] = str(exc)
            save_sources(sources)
            self.send_json({"error": str(exc)}, status=500)

    def handle_sources_delete(self, source_id: str) -> None:
        sources = load_sources()
        existing = next((item for item in sources if item.get("id") == source_id), None)
        if not existing:
            self.send_json({"error": "Source not found."}, status=404)
            return

        updated = [item for item in sources if item.get("id") != source_id]
        try:
            source_root = SOURCES_DIR / source_id
            if source_root.exists():
                shutil.rmtree(source_root)
            save_sources(updated)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)
            return
        self.send_json({"ok": True}, status=200)

    def handle_static(self, raw_path: str) -> None:
        path = raw_path
        if path == "/" or path == "":
            path = "/index.html"
        file_path = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(WEB_DIR.resolve())):
            self.send_error(404)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        try:
            data = file_path.read_bytes()
        except OSError:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type_for(file_path))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.write_response_body(data)

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.write_response_body(data)

    def write_response_body(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before reading the response.
            return
        except OSError as exc:
            # Treat expected disconnect errors as benign.
            if exc.errno in (errno.EPIPE, errno.ECONNRESET):
                return
            raise

    def read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def log_message(self, fmt: str, *args) -> None:
        return


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def log_event(message: str, level: str = "INFO") -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{stamp}] [{level}] {message}")


def load_sources() -> list:
    if not SOURCES_FILE.exists():
        return []
    try:
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8")).get("sources", [])
    except Exception:
        return []


def save_sources(sources: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(json.dumps({"sources": sources}, indent=2), encoding="utf-8")


def sanitize_sources(sources: list) -> list:
    sanitized = []
    for source in sources:
        filtered = dict(source)
        filtered.pop("password", None)
        sanitized.append(filtered)
    return sanitized


def get_session_roots() -> list:
    roots = []
    if INCLUDE_LOCAL and LOCAL_SESSIONS_DIR.exists():
        roots.append(LOCAL_SESSIONS_DIR)
    for source in load_sources():
        source_dir = SOURCES_DIR / source.get("id", "") / "sessions"
        if source_dir.exists():
            roots.append(source_dir)
    return roots


def resolve_sshpass() -> str:
    found = shutil.which("sshpass")
    if found:
        return found
    candidates = ["/opt/homebrew/bin/sshpass", "/usr/local/bin/sshpass"]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return ""


def sync_source(source: dict) -> None:
    sshpass_path = resolve_sshpass()
    if not sshpass_path:
        raise RuntimeError("sshpass not found in PATH or common locations.")

    host = source.get("host")
    user = source.get("user")
    password = source.get("password")
    port = int(source.get("port") or 22)
    path = source.get("path") or "~/.codex/sessions"

    dest_dir = SOURCES_DIR / source.get("id") / "sessions"
    dest_dir.mkdir(parents=True, exist_ok=True)

    remote_base = f"{user}@{host}:{path.rstrip('/')}"
    if shutil.which("rsync"):
        remote = f"{remote_base}/"
        cmd = [
            sshpass_path,
            "-p",
            password,
            "rsync",
            "-az",
            "-e",
            f"ssh -p {port}",
            remote,
            str(dest_dir),
        ]
    else:
        remote = f"{remote_base}/."
        cmd = [
            sshpass_path,
            "-p",
            password,
            "scp",
            "-r",
            "-P",
            str(port),
            remote,
            str(dest_dir),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Sync failed.")


def create_server(host: str, start_port: int, max_attempts: int) -> tuple[ReusableThreadingHTTPServer, int]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for offset in range(max_attempts):
        port = start_port + offset
        try:
            server = ReusableThreadingHTTPServer((host, port), Handler)
            if offset > 0:
                log_event(
                    f"Requested port {start_port} is busy. Using fallback port {port}.",
                    level="WARN",
                )
            return server, port
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE and offset < max_attempts - 1:
                log_event(f"Port {port} is already in use; trying {port + 1}.", level="WARN")
                continue
            raise

    raise RuntimeError("Failed to bind server socket.")


def main() -> int:
    if not WEB_DIR.exists():
        log_event(f"Missing web assets directory: {WEB_DIR}", level="ERROR")
        return 2
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server, port = create_server(DEFAULT_HOST, DEFAULT_PORT, MAX_PORT_ATTEMPTS)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            log_event(
                f"Could not bind to ports {DEFAULT_PORT}-{DEFAULT_PORT + MAX_PORT_ATTEMPTS - 1}: address already in use.",
                level="ERROR",
            )
            return 1
        log_event(f"Failed to start server: {exc}", level="ERROR")
        return 1
    except Exception as exc:
        log_event(f"Failed to start server: {exc}", level="ERROR")
        return 1

    log_event(f"Serving on http://{DEFAULT_HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("Shutdown requested by user.", level="INFO")
    finally:
        server.server_close()
        log_event("Server stopped.", level="INFO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
