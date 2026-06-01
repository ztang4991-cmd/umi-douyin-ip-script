from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import cgi


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = ROOT / "uploads"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "mamba.sqlite3"

SESSIONS: dict[str, dict] = {}
ORDER_STATUSES = {"意向", "已定", "完成", "退订", "作废"}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, _ = password_hash.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), password_hash)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('owner','manager','sales')),
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  owner_user_id INTEGER,
  name TEXT NOT NULL,
  phone TEXT,
  wechat TEXT,
  wedding_date TEXT,
  guest_count INTEGER,
  source TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  pool_status TEXT NOT NULL DEFAULT 'private',
  intention_level TEXT DEFAULT 'B',
  psychology_stage TEXT DEFAULT '观望比较',
  concerns TEXT DEFAULT '',
  objections TEXT DEFAULT '',
  next_follow_up_at TEXT,
  third_party_name TEXT,
  referrer_name TEXT,
  reported_at TEXT,
  report_screenshot_path TEXT,
  commission_rule TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
  FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS lead_collaborators (
  lead_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  PRIMARY KEY (lead_id, user_id),
  FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  lead_id INTEGER,
  opportunity_id INTEGER,
  order_id INTEGER,
  user_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  contact_method TEXT DEFAULT '微信',
  next_follow_up_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
  FOREIGN KEY (lead_id) REFERENCES leads(id),
  FOREIGN KEY (opportunity_id) REFERENCES opportunities(id),
  FOREIGN KEY (order_id) REFERENCES orders(id),
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  lead_id INTEGER NOT NULL,
  owner_user_id INTEGER,
  stage TEXT NOT NULL DEFAULT '跟进中',
  estimated_amount REAL DEFAULT 0,
  expected_date TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
  FOREIGN KEY (lead_id) REFERENCES leads(id),
  FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS venues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  resource_type TEXT NOT NULL CHECK(resource_type IN ('banquet_hall','private_room')),
  capacity INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  venue_id INTEGER NOT NULL,
  opportunity_id INTEGER,
  order_id INTEGER,
  booking_type TEXT NOT NULL CHECK(booking_type IN ('meal','tasting','temporary_lock')),
  booking_date TEXT NOT NULL,
  time_slot TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, venue_id, booking_date, time_slot),
  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
  FOREIGN KEY (venue_id) REFERENCES venues(id),
  FOREIGN KEY (opportunity_id) REFERENCES opportunities(id),
  FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  opportunity_id INTEGER,
  lead_id INTEGER NOT NULL,
  owner_user_id INTEGER,
  status TEXT NOT NULL DEFAULT '意向',
  total_amount REAL DEFAULT 0,
  table_count INTEGER DEFAULT 0,
  contract_pdf_path TEXT,
  third_party_name TEXT,
  referrer_name TEXT,
  commission_method TEXT,
  commission_rate REAL DEFAULT 0,
  estimated_commission REAL DEFAULT 0,
  commission_status TEXT DEFAULT '未结算',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
  FOREIGN KEY (opportunity_id) REFERENCES opportunities(id),
  FOREIGN KEY (lead_id) REFERENCES leads(id),
  FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  paid_at TEXT NOT NULL,
  method TEXT DEFAULT '转账',
  note TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(id),
  FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);
"""


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        migrate_orders_status_check(conn)
        repair_orders_old_foreign_keys(conn)
        if not conn.execute("SELECT id FROM tenants LIMIT 1").fetchone():
            created_at = now_iso()
            cur = conn.execute("INSERT INTO tenants(name, created_at) VALUES (?, ?)", ("Mamba 婚宴演示门店", created_at))
            tenant_id = cur.lastrowid
            users = [
                ("老板", "owner@mamba.local", "owner123", "owner"),
                ("销售一号", "sales@mamba.local", "sales123", "sales"),
            ]
            for name, email, password, role in users:
                conn.execute(
                    "INSERT INTO users(tenant_id, name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (tenant_id, name, email, hash_password(password), role, created_at),
                )
            conn.execute(
                "INSERT INTO venues(tenant_id, name, resource_type, capacity, created_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, "水晶宴会厅", "banquet_hall", 320, created_at),
            )
            conn.execute(
                "INSERT INTO venues(tenant_id, name, resource_type, capacity, created_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, "牡丹包间", "private_room", 30, created_at),
            )


def migrate_orders_status_check(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
    if not row or "CHECK(status IN" not in row["sql"]:
        return
    conn.execute("ALTER TABLE orders RENAME TO orders_old")
    conn.executescript("""
    CREATE TABLE orders (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tenant_id INTEGER NOT NULL,
      opportunity_id INTEGER,
      lead_id INTEGER NOT NULL,
      owner_user_id INTEGER,
      status TEXT NOT NULL DEFAULT '意向',
      total_amount REAL DEFAULT 0,
      table_count INTEGER DEFAULT 0,
      contract_pdf_path TEXT,
      third_party_name TEXT,
      referrer_name TEXT,
      commission_method TEXT,
      commission_rate REAL DEFAULT 0,
      estimated_commission REAL DEFAULT 0,
      commission_status TEXT DEFAULT '未结算',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (tenant_id) REFERENCES tenants(id),
      FOREIGN KEY (opportunity_id) REFERENCES opportunities(id),
      FOREIGN KEY (lead_id) REFERENCES leads(id),
      FOREIGN KEY (owner_user_id) REFERENCES users(id)
    );
    INSERT INTO orders SELECT * FROM orders_old;
    DROP TABLE orders_old;
    """)


def repair_orders_old_foreign_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE '%orders_old%'"
    ).fetchall()
    if not rows:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    for row in rows:
        name = row["name"]
        if name == "followups":
            conn.execute("ALTER TABLE followups RENAME TO followups_bad_fk")
            conn.executescript("""
            CREATE TABLE followups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              lead_id INTEGER,
              opportunity_id INTEGER,
              order_id INTEGER,
              user_id INTEGER NOT NULL,
              content TEXT NOT NULL,
              contact_method TEXT DEFAULT '微信',
              next_follow_up_at TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY (tenant_id) REFERENCES tenants(id),
              FOREIGN KEY (lead_id) REFERENCES leads(id),
              FOREIGN KEY (opportunity_id) REFERENCES opportunities(id),
              FOREIGN KEY (order_id) REFERENCES orders(id),
              FOREIGN KEY (user_id) REFERENCES users(id)
            );
            INSERT INTO followups SELECT * FROM followups_bad_fk;
            DROP TABLE followups_bad_fk;
            """)
        elif name == "bookings":
            conn.execute("ALTER TABLE bookings RENAME TO bookings_bad_fk")
            conn.executescript("""
            CREATE TABLE bookings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              venue_id INTEGER NOT NULL,
              opportunity_id INTEGER,
              order_id INTEGER,
              booking_type TEXT NOT NULL CHECK(booking_type IN ('meal','tasting','temporary_lock')),
              booking_date TEXT NOT NULL,
              time_slot TEXT NOT NULL,
              note TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(tenant_id, venue_id, booking_date, time_slot),
              FOREIGN KEY (tenant_id) REFERENCES tenants(id),
              FOREIGN KEY (venue_id) REFERENCES venues(id),
              FOREIGN KEY (opportunity_id) REFERENCES opportunities(id),
              FOREIGN KEY (order_id) REFERENCES orders(id)
            );
            INSERT INTO bookings SELECT * FROM bookings_bad_fk;
            DROP TABLE bookings_bad_fk;
            """)
        elif name == "payments":
            conn.execute("ALTER TABLE payments RENAME TO payments_bad_fk")
            conn.executescript("""
            CREATE TABLE payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id INTEGER NOT NULL,
              order_id INTEGER NOT NULL,
              amount REAL NOT NULL,
              paid_at TEXT NOT NULL,
              method TEXT DEFAULT '转账',
              note TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY (tenant_id) REFERENCES tenants(id),
              FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            INSERT INTO payments SELECT * FROM payments_bad_fk;
            DROP TABLE payments_bad_fk;
            """)
    conn.execute("PRAGMA foreign_keys = ON")
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "MambaBanquet/0.1"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                self.handle_api(method, path, parse_qs(parsed.query))
            else:
                self.serve_static(path)
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str):
        if path == "/":
            path = "/index.html"
        base = UPLOAD_DIR if path.startswith("/uploads/") else STATIC_DIR
        rel_path = path.removeprefix("/uploads/") if base == UPLOAD_DIR else path.lstrip("/")
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base.resolve())) or not target.exists() or target.is_dir():
            self.send_error(404)
            return
        content_type = "text/html; charset=utf-8"
        if target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif target.suffix == ".pdf":
            content_type = "application/pdf"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        with target.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def current_user(self) -> dict:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise ApiError(401, "请先登录")
        token = auth.removeprefix("Bearer ").strip()
        session = SESSIONS.get(token)
        if not session or session["expires_at"] < time.time():
            raise ApiError(401, "登录已过期")
        return session["user"]

    def require_role(self, user: dict, roles: set[str]):
        if user["role"] not in roles:
            raise ApiError(403, "当前账号没有权限执行该操作")

    def handle_api(self, method: str, path: str, query: dict):
        public = {("/api/login", "POST"), ("/api/bootstrap", "POST")}
        if (path, method) not in public:
            user = self.current_user()
        else:
            user = None

        if path == "/api/login" and method == "POST":
            return self.login()
        if path == "/api/bootstrap" and method == "POST":
            return self.bootstrap()
        if path == "/api/me" and method == "GET":
            return self.send_json({"user": user})
        if path == "/api/users" and method == "GET":
            return self.list_users(user)
        if path == "/api/leads":
            if method == "GET":
                return self.list_leads(user, query)
            if method == "POST":
                return self.create_lead(user)
        if path.startswith("/api/leads/"):
            return self.handle_lead_item(user, method, path)
        if path == "/api/duplicates" and method == "GET":
            return self.duplicates(user, query)
        if path == "/api/opportunities":
            if method == "GET":
                return self.list_opportunities(user)
        if path.startswith("/api/opportunities/"):
            return self.handle_opportunity_item(user, method, path)
        if path == "/api/venues":
            if method == "GET":
                return self.list_venues(user)
            if method == "POST":
                return self.create_venue(user)
        if path == "/api/bookings":
            if method == "GET":
                return self.list_bookings(user, query)
            if method == "POST":
                return self.create_booking(user)
        if path == "/api/orders":
            if method == "GET":
                return self.list_orders(user)
            if method == "POST":
                return self.create_order(user)
        if path.startswith("/api/orders/"):
            return self.handle_order_item(user, method, path)
        if path == "/api/dashboard" and method == "GET":
            return self.dashboard(user, query)
        raise ApiError(404, "接口不存在")

    def login(self):
        data = self.read_json()
        with db() as conn:
            row = conn.execute(
                "SELECT u.*, t.name AS tenant_name FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE email=?",
                (data.get("email", "").strip(),),
            ).fetchone()
        if not row or not verify_password(data.get("password", ""), row["password_hash"]):
            raise ApiError(401, "邮箱或密码错误")
        token = secrets.token_urlsafe(32)
        user = {k: row[k] for k in ("id", "tenant_id", "name", "email", "role", "tenant_name")}
        SESSIONS[token] = {"user": user, "expires_at": time.time() + 60 * 60 * 24}
        self.send_json({"token": token, "user": user})

    def bootstrap(self):
        data = self.read_json()
        tenant_name = (data.get("tenant_name") or "").strip()
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        if not tenant_name or not name or not email or len(password) < 6:
            raise ApiError(400, "门店名、姓名、邮箱和至少 6 位密码必填")
        with db() as conn:
            if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                raise ApiError(409, "该邮箱已存在")
            created_at = now_iso()
            cur = conn.execute("INSERT INTO tenants(name, created_at) VALUES (?, ?)", (tenant_name, created_at))
            tenant_id = cur.lastrowid
            conn.execute(
                "INSERT INTO users(tenant_id, name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, 'owner', ?)",
                (tenant_id, name, email, hash_password(password), created_at),
            )
        self.send_json({"ok": True})

    def list_users(self, user):
        with db() as conn:
            rows = conn.execute("SELECT id, name, email, role FROM users WHERE tenant_id=? ORDER BY id", (user["tenant_id"],)).fetchall()
        self.send_json({"users": rows_to_dicts(rows)})

    def lead_visible_clause(self, user):
        if user["role"] in ("owner", "manager"):
            return "l.tenant_id=?", [user["tenant_id"]]
        return "(l.tenant_id=? AND (l.owner_user_id=? OR l.pool_status='public' OR EXISTS (SELECT 1 FROM lead_collaborators c WHERE c.lead_id=l.id AND c.user_id=?)))", [user["tenant_id"], user["id"], user["id"]]

    def list_leads(self, user, query):
        where, params = self.lead_visible_clause(user)
        status = query.get("status", [""])[0]
        if status:
            where += " AND l.status=?"
            params.append(status)
        with db() as conn:
            rows = conn.execute(
                f"""SELECT l.*, u.name AS owner_name,
                    (SELECT COUNT(*) FROM followups f WHERE f.lead_id=l.id) AS followup_count
                    FROM leads l LEFT JOIN users u ON u.id=l.owner_user_id
                    WHERE {where} ORDER BY l.updated_at DESC""",
                params,
            ).fetchall()
        self.send_json({"leads": rows_to_dicts(rows)})

    def create_lead(self, user):
        data = self.read_json()
        name = (data.get("name") or "").strip()
        if not name:
            raise ApiError(400, "客户姓名必填")
        now = now_iso()
        with db() as conn:
            cur = conn.execute(
                """INSERT INTO leads(
                    tenant_id, owner_user_id, name, phone, wechat, wedding_date, guest_count, source,
                    pool_status, intention_level, psychology_stage, concerns, objections, next_follow_up_at,
                    third_party_name, referrer_name, reported_at, commission_rule, created_at, updated_at
                  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user["tenant_id"], data.get("owner_user_id") or user["id"], name, data.get("phone"), data.get("wechat"),
                    data.get("wedding_date"), data.get("guest_count") or 0, data.get("source"),
                    data.get("pool_status") or "private", data.get("intention_level") or "B",
                    data.get("psychology_stage") or "观望比较", data.get("concerns") or "", data.get("objections") or "",
                    data.get("next_follow_up_at"), data.get("third_party_name"), data.get("referrer_name"),
                    data.get("reported_at"), data.get("commission_rule"), now, now,
                ),
            )
            lead_id = cur.lastrowid
        self.send_json({"lead": self.get_lead(user, lead_id)})

    def get_lead(self, user, lead_id: int) -> dict:
        where, params = self.lead_visible_clause(user)
        params.append(lead_id)
        with db() as conn:
            lead = conn.execute(f"SELECT l.*, u.name AS owner_name FROM leads l LEFT JOIN users u ON u.id=l.owner_user_id WHERE {where} AND l.id=?", params).fetchone()
            if not lead:
                raise ApiError(404, "线索不存在")
            followups = conn.execute(
                "SELECT f.*, u.name AS user_name FROM followups f JOIN users u ON u.id=f.user_id WHERE f.tenant_id=? AND f.lead_id=? ORDER BY f.created_at DESC",
                (user["tenant_id"], lead_id),
            ).fetchall()
            opp = conn.execute("SELECT * FROM opportunities WHERE tenant_id=? AND lead_id=? ORDER BY id DESC LIMIT 1", (user["tenant_id"], lead_id)).fetchone()
            collaborators = conn.execute("SELECT u.id, u.name FROM lead_collaborators c JOIN users u ON u.id=c.user_id WHERE c.lead_id=?", (lead_id,)).fetchall()
        result = dict(lead)
        result["followups"] = rows_to_dicts(followups)
        result["opportunity"] = row_to_dict(opp)
        result["collaborators"] = rows_to_dicts(collaborators)
        result["assistant"] = build_assistant(result)
        return result

    def handle_lead_item(self, user, method, path):
        parts = path.split("/")
        lead_id = int(parts[3])
        if len(parts) == 4 and method == "GET":
            return self.send_json({"lead": self.get_lead(user, lead_id)})
        if len(parts) == 4 and method == "PUT":
            return self.update_lead(user, lead_id)
        if len(parts) == 5 and parts[4] == "followups" and method == "POST":
            return self.add_followup(user, lead_id=lead_id)
        if len(parts) == 5 and parts[4] == "convert" and method == "POST":
            return self.convert_lead(user, lead_id)
        if len(parts) == 5 and parts[4] == "collaborators" and method == "POST":
            return self.add_collaborator(user, lead_id)
        raise ApiError(404, "接口不存在")

    def update_lead(self, user, lead_id: int):
        self.get_lead(user, lead_id)
        data = self.read_json()
        fields = [
            "owner_user_id", "name", "phone", "wechat", "wedding_date", "guest_count", "source", "status",
            "pool_status", "intention_level", "psychology_stage", "concerns", "objections", "next_follow_up_at",
            "third_party_name", "referrer_name", "reported_at", "commission_rule",
        ]
        updates = {k: data[k] for k in fields if k in data}
        if not updates:
            return self.send_json({"lead": self.get_lead(user, lead_id)})
        updates["updated_at"] = now_iso()
        sql = ", ".join([f"{k}=?" for k in updates])
        with db() as conn:
            conn.execute(f"UPDATE leads SET {sql} WHERE tenant_id=? AND id=?", [*updates.values(), user["tenant_id"], lead_id])
        self.send_json({"lead": self.get_lead(user, lead_id)})

    def add_followup(self, user, lead_id=None, opportunity_id=None, order_id=None):
        data = self.read_json()
        content = (data.get("content") or "").strip()
        if not content:
            raise ApiError(400, "跟进内容必填")
        next_at = data.get("next_follow_up_at")
        with db() as conn:
            conn.execute(
                """INSERT INTO followups(tenant_id, lead_id, opportunity_id, order_id, user_id, content, contact_method, next_follow_up_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user["tenant_id"], lead_id, opportunity_id, order_id, user["id"], content, data.get("contact_method") or "微信", next_at, now_iso()),
            )
            if lead_id and next_at:
                conn.execute("UPDATE leads SET next_follow_up_at=?, updated_at=? WHERE tenant_id=? AND id=?", (next_at, now_iso(), user["tenant_id"], lead_id))
        if lead_id:
            self.send_json({"lead": self.get_lead(user, lead_id)})
        else:
            self.send_json({"ok": True})

    def convert_lead(self, user, lead_id: int):
        lead = self.get_lead(user, lead_id)
        if lead["opportunity"]:
            return self.send_json({"opportunity": lead["opportunity"]})
        data = self.read_json()
        with db() as conn:
            cur = conn.execute(
                """INSERT INTO opportunities(tenant_id, lead_id, owner_user_id, stage, estimated_amount, expected_date, created_at, updated_at)
                   VALUES (?, ?, ?, '跟进中', ?, ?, ?, ?)""",
                (user["tenant_id"], lead_id, data.get("owner_user_id") or lead["owner_user_id"] or user["id"], data.get("estimated_amount") or 0, data.get("expected_date"), now_iso(), now_iso()),
            )
            conn.execute("UPDATE leads SET status='opportunity', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), user["tenant_id"], lead_id))
            opp = conn.execute("SELECT * FROM opportunities WHERE id=?", (cur.lastrowid,)).fetchone()
        self.send_json({"opportunity": row_to_dict(opp)})

    def add_collaborator(self, user, lead_id):
        self.get_lead(user, lead_id)
        data = self.read_json()
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO lead_collaborators(lead_id, user_id) VALUES (?, ?)", (lead_id, int(data["user_id"])))
        self.send_json({"lead": self.get_lead(user, lead_id)})

    def duplicates(self, user, query):
        phone = query.get("phone", [""])[0]
        wechat = query.get("wechat", [""])[0]
        name = query.get("name", [""])[0]
        wedding_date = query.get("wedding_date", [""])[0]
        clauses = ["tenant_id=?"]
        params = [user["tenant_id"]]
        dup_clauses = []
        if phone:
            dup_clauses.append("phone=?")
            params.append(phone)
        if wechat:
            dup_clauses.append("wechat=?")
            params.append(wechat)
        if name and wedding_date:
            dup_clauses.append("(name=? AND wedding_date=?)")
            params.extend([name, wedding_date])
        if not dup_clauses:
            return self.send_json({"duplicates": []})
        with db() as conn:
            rows = conn.execute(f"SELECT id, name, phone, wechat, wedding_date, status, created_at FROM leads WHERE {' AND '.join(clauses)} AND ({' OR '.join(dup_clauses)}) ORDER BY updated_at DESC", params).fetchall()
        self.send_json({"duplicates": rows_to_dicts(rows)})

    def list_opportunities(self, user):
        with db() as conn:
            rows = conn.execute(
                """SELECT o.*, l.name AS lead_name, l.phone, l.third_party_name, u.name AS owner_name
                   FROM opportunities o JOIN leads l ON l.id=o.lead_id LEFT JOIN users u ON u.id=o.owner_user_id
                   WHERE o.tenant_id=? ORDER BY o.updated_at DESC""",
                (user["tenant_id"],),
            ).fetchall()
        self.send_json({"opportunities": rows_to_dicts(rows)})

    def handle_opportunity_item(self, user, method, path):
        parts = path.split("/")
        opp_id = int(parts[3])
        if len(parts) == 5 and parts[4] == "followups" and method == "POST":
            return self.add_followup(user, opportunity_id=opp_id)
        if len(parts) == 5 and parts[4] == "order" and method == "POST":
            return self.create_order(user, opportunity_id=opp_id)
        raise ApiError(404, "接口不存在")

    def list_venues(self, user):
        with db() as conn:
            rows = conn.execute("SELECT * FROM venues WHERE tenant_id=? ORDER BY id", (user["tenant_id"],)).fetchall()
        self.send_json({"venues": rows_to_dicts(rows)})

    def create_venue(self, user):
        self.require_role(user, {"owner", "manager"})
        data = self.read_json()
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO venues(tenant_id, name, resource_type, capacity, created_at) VALUES (?, ?, ?, ?, ?)",
                (user["tenant_id"], data["name"], data.get("resource_type") or "banquet_hall", data.get("capacity") or 0, now_iso()),
            )
            venue = conn.execute("SELECT * FROM venues WHERE id=?", (cur.lastrowid,)).fetchone()
        self.send_json({"venue": row_to_dict(venue)})

    def list_bookings(self, user, query):
        start = query.get("start", [today_iso()])[0]
        end = query.get("end", [(date.today() + timedelta(days=45)).isoformat()])[0]
        with db() as conn:
            rows = conn.execute(
                """SELECT b.*, v.name AS venue_name, v.resource_type, l.name AS customer_name
                   FROM bookings b JOIN venues v ON v.id=b.venue_id
                   LEFT JOIN orders o ON o.id=b.order_id
                   LEFT JOIN leads l ON l.id=o.lead_id
                   WHERE b.tenant_id=? AND b.booking_date BETWEEN ? AND ?
                   ORDER BY b.booking_date, b.time_slot, v.name""",
                (user["tenant_id"], start, end),
            ).fetchall()
        self.send_json({"bookings": rows_to_dicts(rows)})

    def create_booking(self, user):
        data = self.read_json()
        try:
            with db() as conn:
                cur = conn.execute(
                    """INSERT INTO bookings(tenant_id, venue_id, opportunity_id, order_id, booking_type, booking_date, time_slot, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user["tenant_id"], data["venue_id"], data.get("opportunity_id"), data.get("order_id"),
                        data["booking_type"], data["booking_date"], data["time_slot"], data.get("note"), now_iso(),
                    ),
                )
                booking = conn.execute("SELECT * FROM bookings WHERE id=?", (cur.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            raise ApiError(409, "该资源在同一日期和时间段已被占用")
        self.send_json({"booking": row_to_dict(booking)})

    def list_orders(self, user):
        with db() as conn:
            rows = conn.execute(
                """SELECT o.*, l.name AS customer_name, l.phone, u.name AS owner_name,
                    COALESCE((SELECT SUM(amount) FROM payments p WHERE p.order_id=o.id), 0) AS paid_amount
                   FROM orders o JOIN leads l ON l.id=o.lead_id LEFT JOIN users u ON u.id=o.owner_user_id
                   WHERE o.tenant_id=? ORDER BY o.updated_at DESC""",
                (user["tenant_id"],),
            ).fetchall()
        self.send_json({"orders": rows_to_dicts(rows)})

    def create_order(self, user, opportunity_id=None):
        data = self.read_json()
        status = data.get("status") or "意向"
        if status not in ORDER_STATUSES:
            raise ApiError(400, "订单状态不合法")
        with db() as conn:
            opp = None
            if opportunity_id or data.get("opportunity_id"):
                opp = conn.execute(
                    """SELECT o.*, l.third_party_name, l.referrer_name, l.id AS lead_id, l.owner_user_id
                       FROM opportunities o JOIN leads l ON l.id=o.lead_id
                       WHERE o.tenant_id=? AND o.id=?""",
                    (user["tenant_id"], opportunity_id or data.get("opportunity_id")),
                ).fetchone()
                if not opp:
                    raise ApiError(404, "商机不存在")
            lead_id = data.get("lead_id") or opp["lead_id"]
            lead = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?", (user["tenant_id"], lead_id)).fetchone()
            if not lead:
                raise ApiError(404, "线索不存在")
            total = float(data.get("total_amount") or opp["estimated_amount"] if opp else data.get("total_amount") or 0)
            rate = float(data.get("commission_rate") or 0)
            cur = conn.execute(
                """INSERT INTO orders(
                    tenant_id, opportunity_id, lead_id, owner_user_id, status, total_amount, table_count,
                    third_party_name, referrer_name, commission_method, commission_rate, estimated_commission,
                    commission_status, created_at, updated_at
                  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user["tenant_id"], opportunity_id or data.get("opportunity_id"), lead_id, data.get("owner_user_id") or lead["owner_user_id"] or user["id"],
                    status, total, data.get("table_count") or 0,
                    lead["third_party_name"], lead["referrer_name"], data.get("commission_method") or lead["commission_rule"],
                    rate, total * rate / 100, data.get("commission_status") or "未结算", now_iso(), now_iso(),
                ),
            )
            conn.execute("UPDATE leads SET status='ordered', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), user["tenant_id"], lead_id))
            if opportunity_id or data.get("opportunity_id"):
                conn.execute("UPDATE opportunities SET stage='已转订单', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), user["tenant_id"], opportunity_id or data.get("opportunity_id")))
            order = conn.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()
        self.send_json({"order": row_to_dict(order)})

    def handle_order_item(self, user, method, path):
        parts = path.split("/")
        order_id = int(parts[3])
        if len(parts) == 5 and parts[4] == "payments" and method == "POST":
            return self.add_payment(user, order_id)
        if len(parts) == 5 and parts[4] == "contract" and method == "POST":
            return self.upload_contract(user, order_id)
        if len(parts) == 5 and parts[4] == "followups" and method == "POST":
            return self.add_followup(user, order_id=order_id)
        if len(parts) == 4 and method == "PUT":
            return self.update_order(user, order_id)
        raise ApiError(404, "接口不存在")

    def update_order(self, user, order_id):
        data = self.read_json()
        if data.get("status") and data["status"] not in ORDER_STATUSES:
            raise ApiError(400, "订单状态不合法")
        fields = ["status", "total_amount", "table_count", "commission_method", "commission_rate", "estimated_commission", "commission_status"]
        updates = {k: data[k] for k in fields if k in data}
        if "total_amount" in updates and "commission_rate" in updates and "estimated_commission" not in updates:
            updates["estimated_commission"] = float(updates["total_amount"]) * float(updates["commission_rate"]) / 100
        updates["updated_at"] = now_iso()
        with db() as conn:
            conn.execute(f"UPDATE orders SET {', '.join([f'{k}=?' for k in updates])} WHERE tenant_id=? AND id=?", [*updates.values(), user["tenant_id"], order_id])
            order = conn.execute("SELECT * FROM orders WHERE tenant_id=? AND id=?", (user["tenant_id"], order_id)).fetchone()
        self.send_json({"order": row_to_dict(order)})

    def add_payment(self, user, order_id):
        data = self.read_json()
        with db() as conn:
            conn.execute(
                "INSERT INTO payments(tenant_id, order_id, amount, paid_at, method, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user["tenant_id"], order_id, float(data["amount"]), data.get("paid_at") or today_iso(), data.get("method") or "转账", data.get("note"), now_iso()),
            )
        self.send_json({"ok": True})

    def upload_contract(self, user, order_id):
        ctype, pdict = cgi.parse_header(self.headers.get("Content-Type"))
        if ctype != "multipart/form-data":
            raise ApiError(400, "请上传 PDF 文件")
        pdict["boundary"] = bytes(pdict["boundary"], "utf-8")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"}, keep_blank_values=True)
        item = form["file"] if "file" in form else None
        if not item or not item.filename.lower().endswith(".pdf"):
            raise ApiError(400, "合同仅支持 PDF")
        tenant_dir = UPLOAD_DIR / str(user["tenant_id"])
        tenant_dir.mkdir(exist_ok=True)
        filename = f"order-{order_id}-{int(time.time())}.pdf"
        target = tenant_dir / filename
        with target.open("wb") as f:
            shutil.copyfileobj(item.file, f)
        rel = str(target.relative_to(ROOT)).replace("\\", "/")
        with db() as conn:
            conn.execute("UPDATE orders SET contract_pdf_path=?, updated_at=? WHERE tenant_id=? AND id=?", (rel, now_iso(), user["tenant_id"], order_id))
        self.send_json({"path": rel})

    def dashboard(self, user, query):
        drill = query.get("drill", [""])[0]
        with db() as conn:
            if drill == "third_party_orders":
                rows = conn.execute("SELECT * FROM orders WHERE tenant_id=? AND third_party_name IS NOT NULL ORDER BY updated_at DESC", (user["tenant_id"],)).fetchall()
                return self.send_json({"items": rows_to_dicts(rows)})
            stats = {}
            stats["lead_count"] = conn.execute("SELECT COUNT(*) c FROM leads WHERE tenant_id=?", (user["tenant_id"],)).fetchone()["c"]
            stats["opportunity_count"] = conn.execute("SELECT COUNT(*) c FROM opportunities WHERE tenant_id=?", (user["tenant_id"],)).fetchone()["c"]
            stats["order_count"] = conn.execute("SELECT COUNT(*) c FROM orders WHERE tenant_id=?", (user["tenant_id"],)).fetchone()["c"]
            stats["order_amount"] = conn.execute("SELECT COALESCE(SUM(total_amount),0) c FROM orders WHERE tenant_id=? AND status IN ('已定','完成','意向')", (user["tenant_id"],)).fetchone()["c"]
            stats["paid_amount"] = conn.execute("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE tenant_id=?", (user["tenant_id"],)).fetchone()["c"]
            stats["estimated_commission"] = conn.execute("SELECT COALESCE(SUM(estimated_commission),0) c FROM orders WHERE tenant_id=?", (user["tenant_id"],)).fetchone()["c"]
            third_party_amount = conn.execute("SELECT COALESCE(SUM(total_amount),0) c FROM orders WHERE tenant_id=? AND third_party_name IS NOT NULL AND third_party_name<>''", (user["tenant_id"],)).fetchone()["c"]
            stats["third_party_amount"] = third_party_amount
            stats["third_party_share"] = (third_party_amount / stats["order_amount"] * 100) if stats["order_amount"] else 0
            stats["commission_cost_rate"] = (stats["estimated_commission"] / stats["order_amount"] * 100) if stats["order_amount"] else 0
            stats["overdue_followups"] = rows_to_dicts(conn.execute(
                "SELECT id, name, phone, next_follow_up_at FROM leads WHERE tenant_id=? AND next_follow_up_at IS NOT NULL AND next_follow_up_at < ? AND status NOT IN ('ordered','invalid') ORDER BY next_follow_up_at",
                (user["tenant_id"], today_iso()),
            ).fetchall())
            stats["sales"] = rows_to_dicts(conn.execute(
                """SELECT u.name, COUNT(DISTINCT l.id) leads, COUNT(DISTINCT o.id) orders, COALESCE(SUM(o.total_amount),0) amount
                   FROM users u LEFT JOIN leads l ON l.owner_user_id=u.id LEFT JOIN orders o ON o.owner_user_id=u.id
                   WHERE u.tenant_id=? GROUP BY u.id ORDER BY amount DESC""",
                (user["tenant_id"],),
            ).fetchall())
            stats["channels"] = rows_to_dicts(conn.execute(
                """SELECT COALESCE(NULLIF(third_party_name,''), '自有/未知') channel, COUNT(*) orders, COALESCE(SUM(total_amount),0) amount, COALESCE(SUM(estimated_commission),0) commission
                   FROM orders WHERE tenant_id=? GROUP BY channel ORDER BY amount DESC""",
                (user["tenant_id"],),
            ).fetchall())
        self.send_json(stats)


def build_assistant(entity: dict) -> dict:
    level = entity.get("intention_level") or "B"
    stage = entity.get("psychology_stage") or "观望比较"
    concerns = entity.get("concerns") or "档期、预算、厅型"
    objections = entity.get("objections") or "价格顾虑"
    cadence = {
        "A": "24 小时内跟进，重点确认到店/试菜和关键决策人。",
        "B": "48 小时内跟进，补充案例、厅型匹配和预算区间。",
        "C": "3-5 天轻触达，保留关系并观察婚期变化。",
    }.get(level, "48 小时内跟进。")
    templates = [
        f"我先帮您把{concerns}这几项逐个确认清楚，再给您整理可选方案。",
        "档期和优惠我这边不能直接承诺，需要以门店最终确认为准；我可以先帮您做内部确认。",
        f"关于{objections}，我建议我们先拆成预算、桌数、服务内容三块看，避免只看单价。"
    ]
    return {
        "red_lines": ["只建议，不自动承诺", "不自动报价", "不自动锁档期", "不自动判定返佣归属"],
        "intention_level": level,
        "psychology_stage": stage,
        "concerns": concerns,
        "objections": objections,
        "cadence": cadence,
        "templates": templates,
    }


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8765"))
    print(f"Mamba Banquet System v0.1 running at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
