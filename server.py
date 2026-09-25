#!/usr/bin/env python3
"""Single-user local Pithomate client backed by the official Harness SDK."""
import argparse
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from deepseek_harness import DeepSeekHarness
except ModuleNotFoundError:
    DeepSeekHarness = None

ROOT = Path(__file__).resolve().parent
DSH_HOME = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))).expanduser().resolve()
DB_PATH = DSH_HOME / "harness-pi-client.sqlite3"
RUN_LOCK = threading.Lock()
MODEL = os.environ.get("DSH_MODEL", "deepseek-v4-flash")


@contextmanager
def database():
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE);
      CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL);
    """)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now_ms():
    return int(time.time() * 1000)


class Handler(BaseHTTPRequestHandler):
    server_version = "PithomateHarness/0.2"

    def log_message(self, _fmt, *_args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path != "/api/harness-pi":
            self.send_json(404, {"ok": False, "error": "Rota não encontrada."})
            return
        host = self.headers.get("host", "").lower()
        if not self.server.valid_host(host) or self.headers.get("origin") != f"http://{host}":
            self.send_json(403, {"ok": False, "error": "Acesso local recusado."})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Solicitação inválida.")
            value = self.dispatch(json.loads(self.rfile.read(length)))
            self.send_json(200, {"ok": True, "value": value})
        except Exception as error:
            self.send_json(400, {"ok": False, "error": str(error) or error.__class__.__name__})

    def dispatch(self, body):
        action = body.get("action")
        if action == "status":
            return {"ready": True, "mode": "sdk"}
        if action == "workspace":
            raw_path = str(body.get("path", "")).strip()
            if not raw_path:
                raise ValueError("Informe o caminho da pasta do projeto.")
            path = Path(raw_path).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("O caminho precisa ser uma pasta existente.")
            workspace_id = hashlib.sha256(str(path).encode()).hexdigest()[:24]
            with database() as db:
                db.execute("INSERT OR IGNORE INTO workspaces(id,path) VALUES (?,?)", (workspace_id, str(path)))
            return {"workspaceId": workspace_id, "path": str(path)}
        if action == "sessions":
            with database() as db:
                rows = db.execute("SELECT s.id,w.path,s.updated_at FROM sessions s JOIN workspaces w ON w.id=s.workspace_id ORDER BY s.updated_at DESC").fetchall()
            return {"items": [{"sessionId": r["id"], "updatedAt": r["updated_at"], "cwd": r["path"], "running": False} for r in rows]}
        if action == "create":
            workspace_id = str(body.get("workspaceId", ""))
            with database() as db:
                if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                    raise ValueError("Workspace inválido. Informe novamente o caminho da pasta.")
                session_id, stamp = str(uuid.uuid4()), now_ms()
                db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session_id, workspace_id, stamp, stamp))
            return {"sessionId": session_id}
        if action == "read":
            session_id = str(body.get("sessionId", ""))
            with database() as db:
                if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                    raise ValueError("Sessão não encontrada.")
                rows = db.execute("SELECT seq,role,content FROM messages WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
            records = []
            for row in rows:
                event_type = "user/message" if row["role"] == "user" else "assistant/message"
                records.append({"seq": row["seq"], "event": {"type": event_type, "data": {"message": {"content": [{"type": "text", "text": row["content"]}]}}}})
            return {"page": {"records": records}, "running": False}
        if action == "prompt":
            if DeepSeekHarness is None:
                raise RuntimeError("SDK ausente. Rode `python -m pip install -r requirements.txt`.")
            session_id, prompt = str(body.get("sessionId", "")), str(body.get("text", "")).strip()
            if not session_id or not prompt:
                raise ValueError("A solicitação está vazia ou a sessão é inválida.")
            with database() as db:
                row = db.execute("SELECT w.path FROM sessions s JOIN workspaces w ON w.id=s.workspace_id WHERE s.id=?", (session_id,)).fetchone()
                if not row:
                    raise ValueError("Sessão não encontrada.")
                stamp = now_ms()
                db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES (?,?,?,?)", (session_id, "user", prompt, stamp))
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (stamp, session_id))
            # Serialize launches because this private client shares one Harness home.
            with RUN_LOCK:
                with DeepSeekHarness(provider="deepseek-official", model=MODEL, cwd=row["path"], dsh_home=str(DSH_HOME), profile="sdk") as harness:
                    result = harness.run(prompt, session_id=session_id)
            answer = str(result.final_response or "").strip()
            if not answer:
                raise RuntimeError(f"Harness encerrou sem resposta final ({result.finish_reason or 'sem motivo'}).")
            with database() as db:
                db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES (?,?,?,?)", (session_id, "assistant", answer, now_ms()))
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), session_id))
            return {"sessionId": session_id, "finishReason": result.finish_reason}
        raise ValueError("Ação desconhecida.")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def valid_host(self, host):
        return host in (f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}")


def main():
    global MODEL
    parser = argparse.ArgumentParser(description="Cliente local Pithomate para DeepSeek Harness")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default=MODEL, help="ID do modelo disponível no perfil SDK")
    args = parser.parse_args()
    MODEL = args.model
    server = LocalServer((args.host, args.port), Handler)
    print(f"Pithomate local: http://{args.host}:{args.port}")
    print(f"Harness home: {DSH_HOME}")
    print("Use Ctrl+C para encerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
