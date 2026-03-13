import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "clinic.db"

SESSION_COOKIE_NAME = "clinic_session"
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours
PASSWORD_HASH_ITERATIONS = 200_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(dt_str: str) -> datetime:
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    raw = handler.rfile.read(length) if length > 0 else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("Invalid JSON body")


def _cookie_get(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    cookie_header = handler.headers.get("Cookie")
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    if name not in cookie:
        return None
    return cookie[name].value


def _set_cookie(handler: BaseHTTPRequestHandler, name: str, value: str, *, max_age: int | None) -> None:
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    handler.send_header("Set-Cookie", "; ".join(parts))


def _clear_cookie(handler: BaseHTTPRequestHandler, name: str) -> None:
    handler.send_header("Set-Cookie", f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")


def _hash_password(password: str, salt: bytes) -> str:
    import hashlib

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return digest.hex()


def _make_salt() -> bytes:
    return secrets.token_bytes(16)


@dataclass(frozen=True)
class AuthedUser:
    id: int
    username: str
    role: str  # "doctor" | "receptionist"


def db_connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def db_init() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK (role IN ('doctor','receptionist')),
              salt BLOB NOT NULL,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appointments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              patient_name TEXT NOT NULL,
              patient_phone TEXT,
              reason TEXT,
              start_at_utc TEXT NOT NULL,
              end_at_utc TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('booked','canceled','completed')),
              created_by_user_id INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              canceled_at TEXT,
              completed_at TEXT,
              reminder_sent_at TEXT,
              notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_appointments_time
              ON appointments(start_at_utc, end_at_utc, status);

            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER REFERENCES users(id),
              action TEXT NOT NULL,
              details_json TEXT,
              created_at TEXT NOT NULL
            );
            """
        )

        user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if user_count == 0:
            for username, role, password in [
                ("doctor", "doctor", "doctor123"),
                ("reception", "receptionist", "reception123"),
            ]:
                salt = _make_salt()
                conn.execute(
                    "INSERT INTO users(username, role, salt, password_hash, created_at) VALUES (?,?,?,?,?)",
                    (username, role, salt, _hash_password(password, salt), _to_iso_z(_utc_now())),
                )
            conn.execute(
                "INSERT INTO audit_log(user_id, action, details_json, created_at) VALUES (NULL,?,?,?)",
                ("seed_users", json.dumps({"doctor": "doctor", "receptionist": "reception"}), _to_iso_z(_utc_now())),
            )

        appt_count = conn.execute("SELECT COUNT(*) AS c FROM appointments").fetchone()["c"]
        if appt_count == 0:
            receptionist_id = conn.execute("SELECT id FROM users WHERE role='receptionist'").fetchone()["id"]
            seed_demo_appointments(conn, receptionist_id)


def seed_demo_appointments(
    conn: sqlite3.Connection,
    receptionist_id: int,
    *,
    date_local: str | None = None,
    tz_offset_minutes: int | None = None,
) -> None:
    now_local = datetime.now().astimezone()
    if date_local is None:
        date_local = now_local.date().isoformat()
    if tz_offset_minutes is None:
        offset = now_local.utcoffset() or timedelta(0)
        tz_offset_minutes = -int(offset.total_seconds() // 60)

    demo = [
        ("Riya Sharma", "9876543210", "Follow-up (BP)", f"{date_local}T10:00", 20, "booked"),
        ("Arjun Mehta", "9000011111", "Fever + cough", f"{date_local}T10:30", 15, "booked"),
        ("Neha Iyer", "9123456789", "Skin rash consult", f"{date_local}T11:00", 30, "booked"),
        ("Kabir Das", "9990001112", "Dental pain", f"{date_local}T12:00", 15, "booked"),
        ("Sana Khan", "9812345678", "Diet consult", f"{date_local}T12:30", 30, "booked"),
        ("Rahul Nair", "9898989898", "Routine check", f"{date_local}T13:30", 15, "canceled"),
        ("Aanya Gupta", "9000022222", "Headache follow-up", f"{date_local}T14:00", 15, "completed"),
    ]

    now = _utc_now()
    for name, phone, reason, start_local, minutes, status in demo:
        start_utc = (datetime.fromisoformat(start_local) + timedelta(minutes=tz_offset_minutes)).replace(tzinfo=timezone.utc)
        end_utc = start_utc + timedelta(minutes=minutes)
        canceled_at = _to_iso_z(now) if status == "canceled" else None
        completed_at = _to_iso_z(now) if status == "completed" else None
        conn.execute(
            """
            INSERT INTO appointments(
              patient_name, patient_phone, reason,
              start_at_utc, end_at_utc,
              status, created_by_user_id,
              created_at, updated_at, notes,
              canceled_at, completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                name,
                phone,
                reason,
                _to_iso_z(start_utc),
                _to_iso_z(end_utc),
                status,
                receptionist_id,
                _to_iso_z(now),
                _to_iso_z(now),
                "Seeded demo data.",
                canceled_at,
                completed_at,
            ),
        )
    _audit(conn, receptionist_id, "seed_appointments", {"count": len(demo), "date_local": date_local})


def _get_authed_user(handler: BaseHTTPRequestHandler) -> AuthedUser | None:
    session_id = _cookie_get(handler, SESSION_COOKIE_NAME)
    if not session_id:
        return None
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT s.expires_at, u.id AS uid, u.username, u.role
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        try:
            expires = _parse_iso(row["expires_at"])
        except Exception:
            return None
        if expires <= _utc_now():
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return None
        return AuthedUser(id=row["uid"], username=row["username"], role=row["role"])


def _require_auth(handler: BaseHTTPRequestHandler) -> AuthedUser | None:
    user = _get_authed_user(handler)
    if not user:
        _json_response(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Not authenticated"})
        return None
    return user


def _require_role(handler: BaseHTTPRequestHandler, role: str) -> AuthedUser | None:
    user = _require_auth(handler)
    if not user:
        return None
    if user.role != role:
        _json_response(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error": "Forbidden"})
        return None
    return user


def _audit(conn: sqlite3.Connection, user_id: int | None, action: str, details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(user_id, action, details_json, created_at) VALUES (?,?,?,?)",
        (user_id, action, json.dumps(details) if details is not None else None, _to_iso_z(_utc_now())),
    )


def _overlaps(conn: sqlite3.Connection, start_utc: datetime, end_utc: datetime, *, exclude_id: int | None = None) -> bool:
    params = {"start": _to_iso_z(start_utc), "end": _to_iso_z(end_utc)}
    sql = """
      SELECT COUNT(*) AS c
      FROM appointments
      WHERE status='booked'
        AND NOT (end_at_utc <= :start OR start_at_utc >= :end)
    """
    if exclude_id is not None:
        sql += " AND id != :exclude"
        params["exclude"] = exclude_id
    c = conn.execute(sql, params).fetchone()["c"]
    return c > 0


def _parse_local_start_and_duration(payload: dict) -> tuple[datetime, datetime]:
    start_local = payload.get("start_local")
    duration_minutes = payload.get("duration_minutes")
    tz_offset_minutes = payload.get("tz_offset_minutes")
    if not isinstance(start_local, str) or "T" not in start_local:
        raise ValueError("start_local required")
    if not isinstance(duration_minutes, int) or duration_minutes <= 0 or duration_minutes > 240:
        raise ValueError("duration_minutes must be 1..240")
    if not isinstance(tz_offset_minutes, int) or tz_offset_minutes < -840 or tz_offset_minutes > 840:
        raise ValueError("tz_offset_minutes invalid")

    local_naive = datetime.fromisoformat(start_local)
    # JS getTimezoneOffset: minutes to add to local to get UTC (IST returns -330)
    start_utc = (local_naive + timedelta(minutes=tz_offset_minutes)).replace(tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(minutes=duration_minutes)
    return start_utc, end_utc


class ClinicHandler(BaseHTTPRequestHandler):
    server_version = "ClinicOS/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if os.environ.get("CLINICOS_VERBOSE") == "1":
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            user = _get_authed_user(self)
            _redirect(self, "/app" if user else "/login")
            return

        if path == "/login":
            html = (BASE_DIR / "static" / "login.html").read_text(encoding="utf-8")
            _text_response(self, HTTPStatus.OK, html, "text/html; charset=utf-8")
            return

        if path == "/app":
            user = _get_authed_user(self)
            if not user:
                _redirect(self, "/login")
                return
            html = (BASE_DIR / "static" / "app.html").read_text(encoding="utf-8")
            _text_response(self, HTTPStatus.OK, html, "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            file_path = (BASE_DIR / path.lstrip("/")).resolve()
            static_root = (BASE_DIR / "static").resolve()
            if not str(file_path).startswith(str(static_root)):
                _text_response(self, HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
                return
            if not file_path.exists() or not file_path.is_file():
                _text_response(self, HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
                return
            ext = file_path.suffix.lower()
            content_type = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".ico": "image/x-icon",
                ".html": "text/html; charset=utf-8",
            }.get(ext, "application/octet-stream")
            data = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/me":
            user = _require_auth(self)
            if not user:
                return
            _json_response(self, HTTPStatus.OK, {"ok": True, "user": {"id": user.id, "username": user.username, "role": user.role}})
            return

        if path == "/api/appointments":
            user = _require_auth(self)
            if not user:
                return
            qs = parse_qs(parsed.query)
            date = (qs.get("date", [None])[0] or "").strip()
            with db_connect() as conn:
                if date:
                    try:
                        tz_offset_minutes = int(self.headers.get("X-TZ-Offset", "0"))
                    except ValueError:
                        tz_offset_minutes = 0
                    day_local = datetime.fromisoformat(date).replace(hour=0, minute=0, second=0, microsecond=0)
                    start_utc = (day_local + timedelta(minutes=tz_offset_minutes)).replace(tzinfo=timezone.utc)
                    end_utc = start_utc + timedelta(days=1)
                    rows = conn.execute(
                        """
                        SELECT *
                        FROM appointments
                        WHERE NOT (end_at_utc <= ? OR start_at_utc >= ?)
                        ORDER BY start_at_utc ASC
                        """,
                        (_to_iso_z(start_utc), _to_iso_z(end_utc)),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM appointments ORDER BY start_at_utc ASC LIMIT 200").fetchall()
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "appointments": [
                        {
                            "id": r["id"],
                            "patient_name": r["patient_name"],
                            "patient_phone": r["patient_phone"],
                            "reason": r["reason"],
                            "start_at_utc": r["start_at_utc"],
                            "end_at_utc": r["end_at_utc"],
                            "status": r["status"],
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                            "canceled_at": r["canceled_at"],
                            "completed_at": r["completed_at"],
                            "reminder_sent_at": r["reminder_sent_at"],
                            "notes": r["notes"],
                        }
                        for r in rows
                    ],
                },
            )
            return

        if path == "/api/stats":
            user = _require_auth(self)
            if not user:
                return
            qs = parse_qs(parsed.query)
            date = (qs.get("date", [None])[0] or "").strip()
            with db_connect() as conn:
                if date:
                    try:
                        tz_offset_minutes = int(self.headers.get("X-TZ-Offset", "0"))
                    except ValueError:
                        tz_offset_minutes = 0
                    day_local = datetime.fromisoformat(date).replace(hour=0, minute=0, second=0, microsecond=0)
                    start_utc = (day_local + timedelta(minutes=tz_offset_minutes)).replace(tzinfo=timezone.utc)
                    end_utc = start_utc + timedelta(days=1)
                    row = conn.execute(
                        """
                        SELECT
                          SUM(CASE WHEN status='booked' THEN 1 ELSE 0 END) AS booked,
                          SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END) AS canceled,
                          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                          COUNT(*) AS total
                        FROM appointments
                        WHERE NOT (end_at_utc <= ? OR start_at_utc >= ?)
                        """,
                        (_to_iso_z(start_utc), _to_iso_z(end_utc)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT
                          SUM(CASE WHEN status='booked' THEN 1 ELSE 0 END) AS booked,
                          SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END) AS canceled,
                          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                          COUNT(*) AS total
                        FROM appointments
                        """
                    ).fetchone()
            _json_response(self, HTTPStatus.OK, {"ok": True, "stats": dict(row)})
            return

        _text_response(self, HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/api/auth/login":
            try:
                payload = _read_json(self)
            except ValueError as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
                return
            username = (payload.get("username") or "").strip()
            password = payload.get("password") or ""
            if not username or not isinstance(password, str):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "username and password required"})
                return
            with db_connect() as conn:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                if not row or _hash_password(password, row["salt"]) != row["password_hash"]:
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Invalid credentials"})
                    return
                session_id = secrets.token_urlsafe(32)
                created = _utc_now()
                expires = created + timedelta(seconds=SESSION_TTL_SECONDS)
                conn.execute(
                    "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                    (session_id, row["id"], _to_iso_z(created), _to_iso_z(expires)),
                )
                _audit(conn, row["id"], "login", {"username": username})

            self.send_response(HTTPStatus.OK)
            _set_cookie(self, SESSION_COOKIE_NAME, session_id, max_age=SESSION_TTL_SECONDS)
            body = json.dumps({"ok": True, "user": {"id": row["id"], "username": row["username"], "role": row["role"]}}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/auth/logout":
            user = _get_authed_user(self)
            session_id = _cookie_get(self, SESSION_COOKIE_NAME)
            with db_connect() as conn:
                if session_id:
                    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                if user:
                    _audit(conn, user.id, "logout")
            self.send_response(HTTPStatus.OK)
            _clear_cookie(self, SESSION_COOKIE_NAME)
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/availability":
            user = _require_auth(self)
            if not user:
                return
            try:
                payload = _read_json(self)
                start_utc, end_utc = _parse_local_start_and_duration(payload)
            except ValueError as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
                return
            exclude_id = payload.get("exclude_appointment_id")
            if exclude_id is not None and not isinstance(exclude_id, int):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "exclude_appointment_id must be int"})
                return
            with db_connect() as conn:
                clash = _overlaps(conn, start_utc, end_utc, exclude_id=exclude_id)
            _json_response(self, HTTPStatus.OK, {"ok": True, "available": not clash})
            return

        if path == "/api/appointments":
            user = _require_role(self, "receptionist")
            if not user:
                return
            try:
                payload = _read_json(self)
                start_utc, end_utc = _parse_local_start_and_duration(payload)
            except ValueError as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
                return
            patient_name = (payload.get("patient_name") or "").strip()
            patient_phone = (payload.get("patient_phone") or "").strip()
            reason = (payload.get("reason") or "").strip()
            notes = (payload.get("notes") or "").strip()
            if not patient_name:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "patient_name required"})
                return

            with db_connect() as conn:
                if _overlaps(conn, start_utc, end_utc):
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": "Time slot already booked"})
                    return
                now = _utc_now()
                cur = conn.execute(
                    """
                    INSERT INTO appointments(
                      patient_name, patient_phone, reason,
                      start_at_utc, end_at_utc,
                      status, created_by_user_id,
                      created_at, updated_at, notes
                    ) VALUES (?,?,?,?,?,'booked',?,?,?,?)
                    """,
                    (
                        patient_name,
                        patient_phone or None,
                        reason or None,
                        _to_iso_z(start_utc),
                        _to_iso_z(end_utc),
                        user.id,
                        _to_iso_z(now),
                        _to_iso_z(now),
                        notes or None,
                    ),
                )
                appt_id = cur.lastrowid
                _audit(conn, user.id, "appointment_booked", {"appointment_id": appt_id})
            _json_response(self, HTTPStatus.OK, {"ok": True, "appointment_id": appt_id})
            return

        if path.startswith("/api/appointments/") and path.endswith("/cancel"):
            user = _require_role(self, "receptionist")
            if not user:
                return
            appt_id_str = path.removeprefix("/api/appointments/").removesuffix("/cancel")
            try:
                appt_id = int(appt_id_str)
            except ValueError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid appointment id"})
                return
            with db_connect() as conn:
                row = conn.execute("SELECT status FROM appointments WHERE id = ?", (appt_id,)).fetchone()
                if not row:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
                    return
                if row["status"] != "booked":
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": "Only booked appointments can be canceled"})
                    return
                now = _utc_now()
                conn.execute(
                    "UPDATE appointments SET status='canceled', canceled_at=?, updated_at=? WHERE id=?",
                    (_to_iso_z(now), _to_iso_z(now), appt_id),
                )
                _audit(conn, user.id, "appointment_canceled", {"appointment_id": appt_id})
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return

        if path.startswith("/api/appointments/") and path.endswith("/reschedule"):
            user = _require_role(self, "receptionist")
            if not user:
                return
            appt_id_str = path.removeprefix("/api/appointments/").removesuffix("/reschedule")
            try:
                appt_id = int(appt_id_str)
            except ValueError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid appointment id"})
                return
            try:
                payload = _read_json(self)
                start_utc, end_utc = _parse_local_start_and_duration(payload)
            except ValueError as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
                return
            with db_connect() as conn:
                row = conn.execute("SELECT status FROM appointments WHERE id = ?", (appt_id,)).fetchone()
                if not row:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
                    return
                if row["status"] != "booked":
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": "Only booked appointments can be rescheduled"})
                    return
                if _overlaps(conn, start_utc, end_utc, exclude_id=appt_id):
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": "Time slot already booked"})
                    return
                now = _utc_now()
                conn.execute(
                    "UPDATE appointments SET start_at_utc=?, end_at_utc=?, updated_at=? WHERE id=?",
                    (_to_iso_z(start_utc), _to_iso_z(end_utc), _to_iso_z(now), appt_id),
                )
                _audit(conn, user.id, "appointment_rescheduled", {"appointment_id": appt_id})
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return

        if path.startswith("/api/appointments/") and path.endswith("/complete"):
            user = _require_role(self, "doctor")
            if not user:
                return
            appt_id_str = path.removeprefix("/api/appointments/").removesuffix("/complete")
            try:
                appt_id = int(appt_id_str)
            except ValueError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid appointment id"})
                return
            with db_connect() as conn:
                row = conn.execute("SELECT status FROM appointments WHERE id = ?", (appt_id,)).fetchone()
                if not row:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
                    return
                if row["status"] != "booked":
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": "Only booked appointments can be completed"})
                    return
                now = _utc_now()
                conn.execute(
                    "UPDATE appointments SET status='completed', completed_at=?, updated_at=? WHERE id=?",
                    (_to_iso_z(now), _to_iso_z(now), appt_id),
                )
                _audit(conn, user.id, "appointment_completed", {"appointment_id": appt_id})
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return

        if path.startswith("/api/appointments/") and path.endswith("/remind"):
            user = _require_role(self, "receptionist")
            if not user:
                return
            appt_id_str = path.removeprefix("/api/appointments/").removesuffix("/remind")
            try:
                appt_id = int(appt_id_str)
            except ValueError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid appointment id"})
                return
            with db_connect() as conn:
                row = conn.execute("SELECT status FROM appointments WHERE id = ?", (appt_id,)).fetchone()
                if not row:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
                    return
                if row["status"] != "booked":
                    _json_response(self, HTTPStatus.CONFLICT, {"ok": False, "error": "Only booked appointments can be reminded"})
                    return
                now = _utc_now()
                conn.execute(
                    "UPDATE appointments SET reminder_sent_at=?, updated_at=? WHERE id=?",
                    (_to_iso_z(now), _to_iso_z(now), appt_id),
                )
                _audit(conn, user.id, "reminder_sent", {"appointment_id": appt_id})
            _json_response(self, HTTPStatus.OK, {"ok": True, "message": "Reminder queued (demo)"})
            return

        if path == "/api/demo/reset":
            user = _require_role(self, "doctor")
            if not user:
                return
            try:
                payload = _read_json(self)
            except ValueError as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
                return
            date_local = payload.get("date_local")
            tz_offset_minutes = payload.get("tz_offset_minutes")
            if date_local is not None and not isinstance(date_local, str):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "date_local must be string"})
                return
            if tz_offset_minutes is not None and not isinstance(tz_offset_minutes, int):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "tz_offset_minutes must be int"})
                return
            with db_connect() as conn:
                receptionist_id = conn.execute("SELECT id FROM users WHERE role='receptionist'").fetchone()["id"]
                conn.execute("DELETE FROM appointments")
                seed_demo_appointments(conn, receptionist_id, date_local=date_local, tz_offset_minutes=tz_offset_minutes)
                _audit(conn, user.id, "demo_reset", {"date_local": date_local})
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})


def run() -> None:
    db_init()
    host = os.environ.get("CLINICOS_HOST", "127.0.0.1")
    port = int(os.environ.get("CLINICOS_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), ClinicHandler)
    print(f"ClinicOS running on http://{host}:{port}")
    print("Demo logins: receptionist=reception / reception123 | doctor=doctor / doctor123")
    server.serve_forever()


if __name__ == "__main__":
    run()
