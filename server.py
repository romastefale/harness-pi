#!/usr/bin/env python3
"""Private HarnessPI web client backed by the official DeepSeek Harness SDK."""
import argparse
import hashlib
import hmac
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
DEFAULT_HOME = "/data/.dsh" if os.environ.get("RAILWAY_ENVIRONMENT") else str(Path.home() / ".dsh")
DSH_HOME = Path(os.environ.get("DSH_HOME", DEFAULT_HOME)).expanduser().resolve()
DB_PATH = DSH_HOME / "harness-pi-client.sqlite3"
DEFAULT_WORKSPACE = Path(os.environ.get("HARNESS_WORKSPACE", str(DSH_HOME / "workspace"))).expanduser().resolve()
RUN_LOCK = threading.Lock()
RUNNING = set()
RUNNING_LOCK = threading.Lock()
MODEL = os.environ.get("DSH_MODEL", "deepseek-v4-flash")
PUBLIC_HOST = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "harness-pi.up.railway.app").lower()
PUBLIC_HOSTS = {PUBLIC_HOST, "harness-pi.up.railway.app"}
APP_HOST = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "127.0.0.1"
APP_PORT = int(os.environ.get("PORT", "8765"))


@contextmanager
def database():
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE);
      CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_json TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS runs (session_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL);
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


def access_password():
    value = os.environ.get("Senha_Acesso", "").strip()
    if len(value) >= 2 and (value[0], value[-1]) in (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’")):
        value = value[1:-1]
    return value


def save_event(session_id, event):
    if not isinstance(event, dict):
        return
    with database() as db:
        db.execute("INSERT INTO events(session_id,event_json,created_at) VALUES (?,?,?)",
                   (session_id, json.dumps(event, ensure_ascii=False), now_ms()))
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), session_id))


def run_prompt(session_id, prompt, workspace, model, effort, max_tokens):
    try:
        if DeepSeekHarness is None:
            raise RuntimeError("SDK ausente. Rode `python -m pip install -r requirements.txt`.")
        key = os.environ.get("Chave_SK", "")
        if not key:
            raise RuntimeError("Configure Chave_SK nas variáveis do Railway.")
        options = {
            "provider": "deepseek-official",
            "model": model or MODEL,
            "api_key": key,
            "cwd": workspace,
            "dsh_home": str(DSH_HOME),
            "profile": "sdk",
            "max_tokens": max_tokens,
        }
        if effort:
            options["reasoning_effort"] = effort
        with RUN_LOCK:
            with DeepSeekHarness(**options) as harness:
                result = harness.run(prompt, session_id=session_id,
                                     on_notification=lambda n: save_event(session_id, n.payload.get("event"))
                                     if n.method == "session.event" else None)
        answer = str(result.final_response or "").strip()
        if answer:
            with database() as db:
                db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES (?,?,?,?)",
                           (session_id, "assistant", answer, now_ms()))
                has_answer = any(json.loads(row[0]).get("type") == "assistant/message"
                                 for row in db.execute("SELECT event_json FROM events WHERE session_id=?", (session_id,)).fetchall())
                if not has_answer:
                    db.execute("INSERT INTO events(session_id,event_json,created_at) VALUES (?,?,?)",
                               (session_id, json.dumps({"type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": answer}]}}}, ensure_ascii=False), now_ms()))
        status, error = "complete", ""
        if not answer:
            status, error = "failed", f"Harness encerrou sem resposta final ({result.finish_reason or 'sem motivo'})."
    except Exception as exc:
        status, error = "failed", str(exc) or exc.__class__.__name__
    finally:
        if 'status' not in locals():
            status, error = "failed", "A execução terminou sem estado conhecido."
        with database() as db:
            db.execute("INSERT OR REPLACE INTO runs(session_id,status,error,updated_at) VALUES (?,?,?,?)",
                       (session_id, status, error, now_ms()))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), session_id))
        with RUNNING_LOCK:
            RUNNING.discard(session_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "HarnessPI/1.0"

    def log_message(self, _fmt, *_args):
        pass

    def send_json(self, status, value, origin=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        if origin:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("vary", "Origin")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path not in ("/", "/index.html", "/harness-pi/"):
            self.send_error(404)
            return
        body = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self' https://harness-pi.up.railway.app; img-src data:; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path != "/api/harness-pi":
            self.send_json(404, {"ok": False, "error": "Rota não encontrada."})
            return
        host, origin = self.headers.get("host", "").lower(), self.headers.get("origin", "")
        if not self.server.valid_request(host, origin):
            self.send_json(403, {"ok": False, "error": "Origem recusada."})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Solicitação inválida.")
            body = json.loads(self.rfile.read(length))
            if body.get("action") != "login" and not self.server.authenticated(self.headers.get("authorization", "")):
                self.send_json(401, {"ok": False, "error": "Entre com sua senha privada."}, origin)
                return
            self.send_json(200, {"ok": True, "value": self.dispatch(body)}, origin)
        except Exception as exc:
            status = 401 if isinstance(exc, PermissionError) else 400
            self.send_json(status, {"ok": False, "error": str(exc) or exc.__class__.__name__}, origin)

    def do_OPTIONS(self):
        origin = self.headers.get("origin", "")
        if urlparse(self.path).path != "/api/harness-pi" or self.headers.get("host", "").lower() not in PUBLIC_HOSTS or origin not in self.server.allowed_origins:
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("access-control-allow-origin", origin)
        self.send_header("access-control-allow-methods", "POST, OPTIONS")
        self.send_header("access-control-allow-headers", "Authorization, Content-Type")
        self.send_header("access-control-max-age", "600")
        self.send_header("vary", "Origin")
        self.end_headers()

    def dispatch(self, body):
        action = body.get("action")
        if action == "login":
            password = access_password()
            if not password:
                raise RuntimeError("Configure Senha_Acesso nas variáveis do Railway.")
            if not hmac.compare_digest(str(body.get("password", "")), password):
                raise PermissionError("Senha incorreta.")
            return {"authorized": True}
        if action == "status":
            if not os.environ.get("Chave_SK"):
                raise RuntimeError("Configure Chave_SK nas variáveis do Railway.")
            return {"ready": True, "mode": "sdk", "model": MODEL, "workspacePath": str(DEFAULT_WORKSPACE)}
        if action == "workspace":
            raw_path = str(body.get("path", "")).strip() or str(DEFAULT_WORKSPACE)
            if Path(raw_path).expanduser().resolve() == DEFAULT_WORKSPACE:
                DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
            path = Path(raw_path).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("O caminho precisa ser uma pasta existente no servidor.")
            workspace_id = hashlib.sha256(str(path).encode()).hexdigest()[:24]
            with database() as db:
                db.execute("INSERT OR IGNORE INTO workspaces(id,path) VALUES (?,?)", (workspace_id, str(path)))
            return {"workspaceId": workspace_id, "path": str(path)}
        if action == "sessions":
            with database() as db:
                rows = db.execute("SELECT s.id,w.path,s.updated_at,COALESCE(r.status,'ready') status FROM sessions s JOIN workspaces w ON w.id=s.workspace_id LEFT JOIN runs r ON r.session_id=s.id ORDER BY s.updated_at DESC").fetchall()
            return {"items": [{"sessionId": r["id"], "updatedAt": r["updated_at"], "cwd": r["path"], "running": r["status"] == "running"} for r in rows]}
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
                events = db.execute("SELECT seq,event_json FROM events WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
                run = db.execute("SELECT status,error FROM runs WHERE session_id=?", (session_id,)).fetchone()
                messages = db.execute("SELECT seq,role,content FROM messages WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
            records = [{"seq": row["seq"], "event": json.loads(row["event_json"])} for row in events]
            if not records:
                for row in messages:
                    event_type = "user/message" if row["role"] == "user" else "assistant/message"
                    records.append({"seq": row["seq"], "event": {"type": event_type, "data": {"message": {"content": [{"type": "text", "text": row["content"]}]}}}})
            return {"page": {"records": records}, "running": bool(run and run["status"] == "running"), "runStatus": run["status"] if run else "ready", "error": run["error"] if run else ""}
        if action == "prompt":
            session_id, prompt = str(body.get("sessionId", "")), str(body.get("text", "")).strip()
            if not session_id or not prompt:
                raise ValueError("A solicitação está vazia ou a sessão é inválida.")
            key = os.environ.get("Chave_SK", "")
            if not key:
                raise RuntimeError("Configure Chave_SK nas variáveis do Railway.")
            model = str(body.get("model", MODEL)).strip()[:120] or MODEL
            effort = str(body.get("reasoningEffort", "")).strip()
            if effort and effort not in {"low", "medium", "high"}:
                raise ValueError("Nível de raciocínio inválido.")
            try:
                max_tokens = int(body.get("maxTokens", 49152))
            except (TypeError, ValueError):
                raise ValueError("Limite de tokens inválido.")
            if not 1024 <= max_tokens <= 49152:
                raise ValueError("O limite deve ficar entre 1.024 e 49.152 tokens.")
            with database() as db:
                row = db.execute("SELECT w.path FROM sessions s JOIN workspaces w ON w.id=s.workspace_id WHERE s.id=?", (session_id,)).fetchone()
                if not row:
                    raise ValueError("Sessão não encontrada.")
                db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES (?,?,?,?)", (session_id, "user", prompt, now_ms()))
                db.execute("INSERT INTO events(session_id,event_json,created_at) VALUES (?,?,?)",
                           (session_id, json.dumps({"type": "user/message", "data": {"message": {"content": [{"type": "text", "text": prompt}]}}}, ensure_ascii=False), now_ms()))
                db.execute("INSERT OR REPLACE INTO runs(session_id,status,error,updated_at) VALUES (?,?,?,?)", (session_id, "running", "", now_ms()))
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), session_id))
            with RUNNING_LOCK:
                if session_id in RUNNING:
                    raise ValueError("Esta sessão já está executando uma tarefa.")
                RUNNING.add(session_id)
            threading.Thread(target=run_prompt, args=(session_id, prompt, row["path"], model, effort, max_tokens), daemon=True).start()
            return {"sessionId": session_id, "accepted": True}
        raise ValueError("Ação desconhecida.")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allowed_origins = ("https://harness-pi.up.railway.app", "https://romastefale.github.io")

    def valid_request(self, host, origin):
        if host in PUBLIC_HOSTS:
            return origin in self.allowed_origins
        return host in (f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}") and origin == f"http://{host}"

    def authenticated(self, value):
        password = access_password()
        return bool(password) and value.startswith("Bearer ") and hmac.compare_digest(value[7:], password)


def main():
    global MODEL
    parser = argparse.ArgumentParser(description="HarnessPI — cliente privado do DeepSeek Harness")
    parser.add_argument("--host", default=APP_HOST)
    parser.add_argument("--port", type=int, default=APP_PORT)
    parser.add_argument("--model", default=MODEL, help="ID de um modelo disponível no perfil SDK")
    args = parser.parse_args()
    MODEL = args.model
    with database() as db:
        db.execute("UPDATE runs SET status='interrupted', error='O servidor reiniciou durante esta execução.' WHERE status='running'")
    server = LocalServer((args.host, args.port), Handler)
    print(f"HarnessPI: {PUBLIC_HOST if args.host == '0.0.0.0' else f'http://{args.host}:{args.port}'}")
    print(f"Harness home: {DSH_HOME}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
