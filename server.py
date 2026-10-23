#!/usr/bin/env python3
"""TransitLink local prototype server and SQLite account API."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TRANSITLINK_DB", ROOT / "data" / "transitlink.sqlite3"))
PBKDF2_ROUNDS = 310_000


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with connect_db() as db:
        db.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          full_name TEXT NOT NULL,
          email TEXT NOT NULL COLLATE NOCASE UNIQUE,
          phone TEXT,
          role TEXT NOT NULL CHECK (role IN ('passenger', 'driver')),
          password_hash TEXT NOT NULL,
          password_salt TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS buses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
          fleet_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
          plate_number TEXT NOT NULL COLLATE NOCASE UNIQUE,
          model TEXT NOT NULL,
          capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 100),
          route_code TEXT NOT NULL CHECK (route_code IN ('R1','R3','R7')),
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
        """)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return salt.hex(), derived.hex()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status, data, cookie=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 2 or size > 16_384:
                raise ValueError("Request is empty or too large")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            return value
        except (ValueError, json.JSONDecodeError):
            raise ValueError("Please submit valid form information")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/accounts/register", "/accounts/login", "/accounts/logout"):
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})
        try:
            data = self.read_json() if path != "/api/logout" else {}
            if path == "/accounts/register":
                return self.register(data)
            if path == "/accounts/login":
                return self.login(data)
            return self.logout()
        except ValueError as exc:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "email" in message:
                error = "An account with this email already exists. Try logging in."
            elif "fleet_code" in message:
                error = "That fleet code is already registered."
            elif "plate_number" in message:
                error = "That number plate is already registered."
            else:
                error = "Those details could not be saved. Check the bus information."
            return self.send_json(HTTPStatus.CONFLICT, {"error": error})

    def register(self, data):
        role = data.get("role")
        name = str(data.get("fullName", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        phone = str(data.get("phone", "")).strip()
        password = data.get("password", "")
        if role not in ("passenger", "driver"):
            raise ValueError("Choose passenger or driver account.")
        if not name or len(name) > 120:
            raise ValueError("Enter your name (up to 120 characters).")
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Enter a valid email address.")
        if len(phone) > 40:
            raise ValueError("Phone number is too long.")
        if not isinstance(password, str) or len(password) < 8 or len(password) > 256:
            raise ValueError("Password must be between 8 and 256 characters.")
        bus = data.get("bus") or {}
        if role == "driver":
            required = ("fleetCode", "plateNumber", "model", "capacity", "routeCode")
            if any(not str(bus.get(k, "")).strip() for k in required):
                raise ValueError("Complete all bus and route details to register as a driver.")
            try:
                capacity = int(bus["capacity"])
            except (TypeError, ValueError):
                raise ValueError("Enter a valid seating capacity.")
            if not 1 <= capacity <= 100:
                raise ValueError("Seating capacity must be from 1 to 100.")
            if bus["routeCode"] not in ("R1", "R3", "R7"):
                raise ValueError("Choose a valid route.")
            if any(len(str(bus[k]).strip()) > 100 for k in ("fleetCode", "plateNumber", "model")):
                raise ValueError("Bus details are too long.")
        salt, digest = hash_password(password)
        with connect_db() as db:
            cur = db.execute("INSERT INTO accounts(full_name,email,phone,role,password_hash,password_salt) VALUES(?,?,?,?,?,?)",
                             (name, email, phone or None, role, digest, salt))
            if role == "driver":
                db.execute("INSERT INTO buses(account_id,fleet_code,plate_number,model,capacity,route_code) VALUES(?,?,?,?,?,?)",
                           (cur.lastrowid, str(bus["fleetCode"]).strip(), str(bus["plateNumber"]).strip().upper(), str(bus["model"]).strip(), capacity, bus["routeCode"]))
        return self.send_json(HTTPStatus.CREATED, {"ok": True})

    def login(self, data):
        email = str(data.get("email", "")).strip().lower()
        password = data.get("password", "")
        role = data.get("role")
        if not email or not isinstance(password, str):
            raise ValueError("Enter your email and password.")
        with connect_db() as db:
            account = db.execute("SELECT * FROM accounts WHERE email=? AND role=?", (email, role)).fetchone()
            if not account:
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "We couldn't find a matching account. Check your account type and details."})
            _, candidate = hash_password(password, bytes.fromhex(account["password_salt"]))
            if not hmac.compare_digest(candidate, account["password_hash"]):
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Email or password is incorrect."})
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions(token_hash,account_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), account["id"], int(time.time()) + 60 * 60 * 24 * 14, int(time.time())))
        cookie = f"transitlink_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={60*60*24*14}"
        return self.send_json(HTTPStatus.OK, {"ok": True, "role": role}, cookie)

    def logout(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("transitlink_session")
        if token:
            token_hash = hashlib.sha256(token.value.encode()).hexdigest()
            with connect_db() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        return self.send_json(HTTPStatus.OK, {"ok": True}, "transitlink_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/accounts/session":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("transitlink_session")
            if not token:
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"authenticated": False})
            token_hash = hashlib.sha256(token.value.encode()).hexdigest()
            with connect_db() as db:
                row = db.execute("""SELECT a.id,a.full_name,a.email,a.role,b.fleet_code,b.plate_number,b.model,b.capacity,b.route_code
                  FROM sessions s JOIN accounts a ON a.id=s.account_id LEFT JOIN buses b ON b.account_id=a.id
                  WHERE s.token_hash=? AND s.expires_at>?""", (token_hash, int(time.time()))).fetchone()
            if not row:
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"authenticated": False})
            return self.send_json(HTTPStatus.OK, {"authenticated": True, "account": dict(row)})
        return super().do_GET()


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    print(f"TransitLink running at http://127.0.0.1:{port}")
    print(f"SQLite database: {DB_PATH}")
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nTransitLink server stopped")
