"""Admin accounts and login sessions.

Passwords are hashed with scrypt and never stored. A login sets the
anchor_admin cookie to a random token; the database keeps only its SHA-256,
so a leaked row can't be replayed. Admins are created with
python -m scripts.create_admin.
"""

import base64
import functools
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from psycopg.rows import dict_row

from app.db import DB_ERRORS, pool

SESSION_COOKIE = "anchor_admin"
SESSION_TTL = timedelta(hours=8)
# Browsers accept Secure cookies on http://localhost except older Safari;
# ADMIN_COOKIE_SECURE=false is for that local case only.
COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "true").lower() != "false"
MIN_PASSWORD_LENGTH = 12
# scrypt cost: about 0.1 s and 32 MB per hash.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
# Failed logins allowed per email, and per client address (shared by
# everyone behind one network), within LOGIN_WINDOW seconds.
LOGIN_MAX_FAILURES_EMAIL = 5
LOGIN_MAX_FAILURES_IP = 20
LOGIN_WINDOW = 15 * 60
DEFAULT_NEXT = "/admin/upload"

LOGIN_HTML = Path(__file__).parent / "static" / "login.html"

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

def normalize_email(email: str) -> str:
    return email.strip().lower()

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                         maxmem=SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(key)}"

def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = unb64(key)
        actual = hashlib.scrypt(password.encode(), salt=unb64(salt), n=int(n), r=int(r),
                                p=int(p), maxmem=SCRYPT_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)

# Checked against when the email is unknown, so a login takes as long
# whether or not the account exists.
@functools.cache
def dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe())

def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def create_session(admin_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with pool.connection() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE expires_at <= now()")
        conn.execute(
            "INSERT INTO admin_sessions (token_sha256, admin_id, expires_at) "
            "VALUES (%s, %s, now() + %s)",
            (token_digest(token), admin_id, SESSION_TTL),
        )
    return token

def session_admin(token: str) -> dict | None:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT a.id, a.name, a.email
            FROM admin_sessions s
            JOIN admins a ON a.id = s.admin_id
            WHERE s.token_sha256 = %s AND s.expires_at > now()
            """,
            (token_digest(token),),
        ).fetchone()

class LoginRequired(Exception):
    """Raised for a logged-out page request; the app redirects to the login page."""

    def __init__(self, next_path: str):
        self.next_path = next_path

# Cookies are SameSite=Lax, which already stops other sites posting with
# them; refusing a foreign Origin covers older browsers too.
def check_origin(request: Request):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-site request refused")

def current_admin(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return session_admin(token)
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")

# Dependency for API endpoints: 401 when logged out.
def require_admin(request: Request) -> dict:
    check_origin(request)
    admin = current_admin(request)
    if admin is None:
        raise HTTPException(status_code=401, detail="Log in as an admin first")
    return admin

# Dependency for everything under /admin: pages redirect to the login page,
# /admin/api/ endpoints answer 401 like require_admin.
def require_admin_page(request: Request) -> dict:
    check_origin(request)
    admin = current_admin(request)
    if admin is None:
        if request.method == "GET" and not request.url.path.startswith("/admin/api/"):
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise LoginRequired(path)
        raise HTTPException(status_code=401, detail="Log in as an admin first")
    return admin

# Where to go after logging in: only a local /admin page, so the login form
# can't be used to send someone to another site.
def safe_next(next_path: str | None) -> str:
    if not next_path or "\\" in next_path:
        return DEFAULT_NEXT
    parts = urlsplit(next_path)
    if parts.scheme or parts.netloc or not (
        parts.path == "/admin" or parts.path.startswith("/admin/")
    ) or parts.path.startswith("/admin/log"):
        return DEFAULT_NEXT
    return next_path

# Failed login times by ("email", address) and ("ip", address). In memory,
# so a restart clears them.
login_failures: dict[tuple[str, str], deque] = defaultdict(deque)

def recent_failures(key: tuple[str, str], now: float) -> int:
    times = login_failures[key]
    while times and now - times[0] > LOGIN_WINDOW:
        times.popleft()
    return len(times)

router = APIRouter(include_in_schema=False)

@router.get("/admin/login")
def login_page(request: Request, next: str | None = None):
    if current_admin(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return FileResponse(LOGIN_HTML)

@router.post("/admin/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          next: str | None = Form(None)):
    check_origin(request)
    email = normalize_email(email)
    keys = [("email", email), ("ip", request.client.host if request.client else "")]
    now = time.monotonic()
    if recent_failures(keys[0], now) >= LOGIN_MAX_FAILURES_EMAIL or \
            recent_failures(keys[1], now) >= LOGIN_MAX_FAILURES_IP:
        raise HTTPException(status_code=429, detail="Too many failed logins; try again in 15 minutes")
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            admin = cur.execute(
                "SELECT id, password_hash FROM admins WHERE email = %s", (email,)
            ).fetchone()
        if not verify_password(password, admin["password_hash"] if admin else dummy_hash()) \
                or admin is None:
            for key in keys:
                login_failures[key].append(now)
            raise HTTPException(status_code=401, detail="Wrong email or password")
        login_failures.pop(keys[0], None)
        token = create_session(admin["id"])
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    response = JSONResponse({"next": safe_next(next)})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), path="/",
        httponly=True, secure=COOKIE_SECURE, samesite="lax",
    )
    return response

@router.post("/admin/logout")
def logout(request: Request):
    check_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            with pool.connection() as conn:
                conn.execute("DELETE FROM admin_sessions WHERE token_sha256 = %s",
                             (token_digest(token),))
        except DB_ERRORS:
            raise HTTPException(status_code=503, detail="Database unavailable")
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE,
                           samesite="lax")
    return response
