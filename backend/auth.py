"""
UrbanSanity — Contrôle d'accès (solution Geo Wakanda)
======================================================
Parcours :
  1. Demande d'accès (nom, e-mail, organisation, fonction, motif, mot de passe)
  2. Vérification de l'adresse e-mail (lien envoyé via Brevo)
  3. Validation manuelle par l'administrateur (page /admin)
  4. Connexion par e-mail + mot de passe (cookie de session signé)

Variables d'environnement (Railway → Variables) :
  DATABASE_URL        ${{Postgres.DATABASE_URL}}  (sinon SQLite local, effacé à chaque redéploiement)
  SECRET_KEY          longue chaîne aléatoire (signature des sessions et des liens)
  ADMIN_EMAIL         e-mail de l'administrateur (reçoit les notifications)
  ADMIN_PASSWORD      mot de passe administrateur
  BREVO_API_KEY       clé API Brevo (envoi des e-mails)
  MAIL_SENDER_EMAIL   expéditeur vérifié dans Brevo (ex. votre Gmail)
  MAIL_SENDER_NAME    nom affiché (défaut : "Geo Wakanda · UrbanSanity")
  APP_URL             (optionnel) URL publique, ex. https://xxx.up.railway.app
  AUTH_ENABLED        (optionnel) "false" pour désactiver le contrôle d'accès
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

# ── Configuration ───────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "urbansanity_users.db"))

SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(48)
    print("[auth] ⚠️  SECRET_KEY absente : clé temporaire générée. Les sessions et liens "
          "seront invalidés à chaque redémarrage. Définissez SECRET_KEY dans Railway.")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
MAIL_SENDER_EMAIL = os.getenv("MAIL_SENDER_EMAIL", "").strip() or ADMIN_EMAIL
MAIL_SENDER_NAME = os.getenv("MAIL_SENDER_NAME", "Geo Wakanda · UrbanSanity").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")

COOKIE_NAME = "us_session"
SESSION_TTL = 7 * 24 * 3600          # 7 jours
VERIFY_TTL = 48 * 3600               # 48 h
RESET_TTL = 3600                     # 1 h
PBKDF2_ITER = 260_000

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

STATUS_LABELS = {
    "pending_email": "E-mail non vérifié",
    "pending_approval": "En attente de validation",
    "active": "Actif",
    "rejected": "Refusé",
    "disabled": "Désactivé",
}

# ── Base de données (PostgreSQL sur Railway, SQLite en local) ───────────────
_pool = None
_sqlite_lock = threading.Lock()


def _init_pool():
    global _pool
    if IS_PG and _pool is None:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _pool = ConnectionPool(url, min_size=1, max_size=5,
                               kwargs={"autocommit": True, "row_factory": dict_row}, open=True)


def db(sql: str, params: tuple = (), fetch: str = "none"):
    """Exécute une requête. fetch = 'none' | 'one' | 'all'. Placeholders : '?'."""
    if IS_PG:
        _init_pool()
        sql = sql.replace("?", "%s")
        with _pool.connection() as conn:
            cur = conn.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
    with _sqlite_lock:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, params)
            res = None
            if fetch == "one":
                r = cur.fetchone()
                res = dict(r) if r else None
            elif fetch == "all":
                res = [dict(r) for r in cur.fetchall()]
            conn.commit()
            return res
        finally:
            conn.close()


def init_db():
    id_col = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_col},
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            organisation TEXT,
            fonction TEXT,
            motif TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending_email',
            created_at TEXT NOT NULL,
            verified_at TEXT,
            approved_at TEXT,
            last_login TEXT,
            admin_note TEXT
        )
    """)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    return db("SELECT * FROM users WHERE email = ?", (email.strip().lower(),), "one")


def get_user(uid: int) -> Optional[Dict[str, Any]]:
    return db("SELECT * FROM users WHERE id = ?", (uid,), "one")


# Petit cache pour éviter une requête SQL à chaque fichier statique
_user_cache: Dict[int, tuple] = {}


def get_user_cached(uid: int) -> Optional[Dict[str, Any]]:
    hit = _user_cache.get(uid)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    u = get_user(uid)
    _user_cache[uid] = (time.time(), u)
    return u


def invalidate_user(uid: int):
    _user_cache.pop(uid, None)


# ── Mots de passe ───────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt_b64, hash_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt_b64), int(it))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def pw_version(user: Dict[str, Any]) -> str:
    """Empreinte courte du mot de passe : change → sessions et liens de reset invalidés."""
    return hashlib.sha256(user["password_hash"].encode()).hexdigest()[:12]


# ── Jetons signés (session, vérification, reset) ────────────────────────────
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(purpose: str, uid: int, ttl: int, **extra) -> str:
    payload = {"p": purpose, "uid": uid, "exp": int(time.time()) + ttl, **extra}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str, purpose: str) -> Optional[Dict[str, Any]]:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("p") != purpose or payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def session_user(request: Request) -> Optional[Dict[str, Any]]:
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None
    data = read_token(tok, "session")
    if not data:
        return None
    user = get_user_cached(int(data["uid"]))
    if not user or user["status"] != "active" or data.get("pv") != pw_version(user):
        return None
    return user


# ── URL publique ────────────────────────────────────────────────────────────
def base_url(request: Request) -> str:
    if APP_URL:
        return APP_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def is_https(request: Request) -> bool:
    return base_url(request).startswith("https://")


# ── E-mails (Brevo) ─────────────────────────────────────────────────────────
def email_layout(base: str, title: str, body_html: str, button: Optional[tuple] = None) -> str:
    btn = ""
    if button:
        label, url = button
        btn = (f'<p style="margin:26px 0 8px"><a href="{url}" style="background:#0F4C5C;color:#fff;'
               f'text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:700;display:inline-block">'
               f'{html.escape(label)}</a></p>'
               f'<p style="font-size:12px;color:#6B7F8B">Si le bouton ne fonctionne pas, copiez ce lien :<br>'
               f'<a href="{url}" style="color:#0F4C5C;word-break:break-all">{url}</a></p>')
    return f"""<!doctype html><html><body style="margin:0;background:#F0F4F6;font-family:Segoe UI,Arial,sans-serif;color:#1E2E36">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F0F4F6;padding:28px 12px"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#fff;border-radius:12px;overflow:hidden">
<tr><td style="background:#0C0D50;padding:18px 24px" align="center">
<img src="{base}/assets/geowakanda-logo.png" alt="Geo Wakanda" width="220" style="display:block;width:220px;max-width:100%;height:auto"></td></tr>
<tr><td style="padding:26px 28px 8px">
<p style="margin:0 0 4px;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#C8A415;font-weight:700">UrbanSanity · Waste Collection Planning</p>
<h1 style="margin:0 0 14px;font-size:20px;color:#0F4C5C">{html.escape(title)}</h1>
<div style="font-size:14.5px;line-height:1.6">{body_html}</div>{btn}</td></tr>
<tr><td style="padding:18px 28px 24px;font-size:11.5px;color:#93A5AF;border-top:1px solid #E7EDF0">
Une solution <strong style="color:#0F4C5C">Geo Wakanda</strong> — plateforme d'aide à la planification de la collecte des déchets.<br>
Cet e-mail est automatique, merci de ne pas y répondre.</td></tr>
</table></td></tr></table></body></html>"""


def send_email(to_email: str, to_name: str, subject: str, html_content: str) -> bool:
    if not BREVO_API_KEY or not MAIL_SENDER_EMAIL:
        links = re.findall(r'href="([^"]+)"', html_content)
        print(f"[auth] (e-mail non envoyé : BREVO_API_KEY/MAIL_SENDER_EMAIL manquant) → {to_email} | "
              f"{subject} | liens : {links[-1] if links else '-'}")
        return False
    try:
        r = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "accept": "application/json", "content-type": "application/json"},
            json={
                "sender": {"name": MAIL_SENDER_NAME, "email": MAIL_SENDER_EMAIL},
                "to": [{"email": to_email, "name": to_name or to_email}],
                "subject": subject,
                "htmlContent": html_content,
            },
            timeout=20,
        )
        if r.status_code >= 300:
            print(f"[auth] Erreur Brevo {r.status_code} : {r.text[:300]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[auth] Envoi e-mail impossible : {e}")
        return False


def mail_verification(base: str, user: Dict[str, Any]):
    tok = make_token("verify", user["id"], VERIFY_TTL)
    url = f"{base}/verify-email?token={tok}"
    body = (f"<p>Bonjour {html.escape(user['name'])},</p>"
            "<p>Nous avons bien reçu votre demande d'accès à la plateforme <strong>UrbanSanity</strong>. "
            "Confirmez d'abord votre adresse e-mail en cliquant sur le bouton ci-dessous (lien valable 48 heures).</p>"
            "<p>Votre demande sera ensuite examinée par l'équipe Geo Wakanda. Vous recevrez un e-mail dès que votre accès sera activé.</p>")
    send_email(user["email"], user["name"], "Confirmez votre adresse e-mail — UrbanSanity",
               email_layout(base, "Confirmez votre adresse e-mail", body, ("Confirmer mon adresse e-mail", url)))


def mail_admin_new_request(base: str, user: Dict[str, Any]):
    if not ADMIN_EMAIL:
        return
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#6B7F8B'>{k}</td><td style='padding:4px 0'><strong>{html.escape(v or '—')}</strong></td></tr>"
        for k, v in [("Nom", user["name"]), ("E-mail", user["email"]), ("Organisation", user.get("organisation")),
                     ("Fonction", user.get("fonction")), ("Motif", user.get("motif"))])
    body = f"<p>Une nouvelle demande d'accès (e-mail vérifié) attend votre validation :</p><table style='font-size:14px'>{rows}</table>"
    send_email(ADMIN_EMAIL, "Administrateur", f"Nouvelle demande d'accès : {user['name']}",
               email_layout(base, "Nouvelle demande d'accès", body, ("Ouvrir l'administration", f"{base}/admin")))


def mail_approved(base: str, user: Dict[str, Any]):
    body = (f"<p>Bonjour {html.escape(user['name'])},</p>"
            "<p>Bonne nouvelle : votre accès à la plateforme <strong>UrbanSanity</strong> a été validé par l'équipe Geo Wakanda.</p>"
            "<p>Connectez-vous avec votre adresse e-mail et le mot de passe choisi lors de votre demande.</p>")
    send_email(user["email"], user["name"], "Votre accès UrbanSanity est activé",
               email_layout(base, "Votre accès est activé", body, ("Se connecter", f"{base}/login")))


def mail_rejected(base: str, user: Dict[str, Any], note: str = ""):
    extra = f"<p><em>Message de l'administrateur :</em><br>{html.escape(note)}</p>" if note else ""
    body = (f"<p>Bonjour {html.escape(user['name'])},</p>"
            "<p>Votre demande d'accès à la plateforme UrbanSanity n'a pas été retenue pour le moment.</p>"
            f"{extra}<p>Pour toute question, vous pouvez contacter l'équipe Geo Wakanda.</p>")
    send_email(user["email"], user["name"], "Votre demande d'accès UrbanSanity",
               email_layout(base, "Demande d'accès non retenue", body))


def mail_reset(base: str, user: Dict[str, Any]):
    tok = make_token("reset", user["id"], RESET_TTL, pv=pw_version(user))
    url = f"{base}/reset-password?token={tok}"
    body = (f"<p>Bonjour {html.escape(user['name'])},</p>"
            "<p>Vous avez demandé à réinitialiser votre mot de passe UrbanSanity. Ce lien est valable 1 heure et ne peut servir qu'une fois.</p>"
            "<p>Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet e-mail.</p>")
    send_email(user["email"], user["name"], "Réinitialisation de votre mot de passe — UrbanSanity",
               email_layout(base, "Réinitialiser votre mot de passe", body, ("Choisir un nouveau mot de passe", url)))


# ── Limitation des tentatives de connexion ──────────────────────────────────
_attempts: Dict[str, list] = {}


def _rate_key(request: Request, email: str) -> str:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    return f"{ip}|{email}"


def too_many_attempts(key: str, limit: int = 8, window: int = 900) -> bool:
    now = time.time()
    lst = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = lst
    return len(lst) >= limit


def record_failure(key: str):
    _attempts.setdefault(key, []).append(time.time())


# ── Modèles ────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class RegisterIn(BaseModel):
    name: str
    email: str
    organisation: str = ""
    fonction: str = ""
    motif: str = ""
    password: str


class LoginIn(BaseModel):
    email: str
    password: str
    next: str = "/"


class EmailIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


class RejectIn(BaseModel):
    note: str = ""


def public_user(u: Dict[str, Any]) -> Dict[str, Any]:
    return {k: u.get(k) for k in ("id", "email", "name", "organisation", "fonction", "motif", "role", "status",
                                  "created_at", "verified_at", "approved_at", "last_login", "admin_note")} | {
        "status_label": STATUS_LABELS.get(u.get("status"), u.get("status"))}


def _clean(s: str, n: int) -> str:
    return (s or "").strip()[:n]


def _safe_next(nxt: str) -> str:
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/login"):
        return "/"
    return nxt


# ── Installation dans l'application FastAPI ────────────────────────────────
PUBLIC_EXACT = {"/login", "/verify-email", "/reset-password", "/health", "/api/health", "/favicon.ico"}
PUBLIC_PREFIXES = ("/api/auth/", "/assets/")


def ensure_admin():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        print("[auth] ⚠️  ADMIN_EMAIL / ADMIN_PASSWORD non définis : personne ne pourra valider les demandes.")
        return
    u = get_user_by_email(ADMIN_EMAIL)
    if not u:
        db("INSERT INTO users (email, name, organisation, fonction, password_hash, role, status, created_at, verified_at, approved_at) "
           "VALUES (?, ?, ?, ?, ?, 'admin', 'active', ?, ?, ?)",
           (ADMIN_EMAIL, "Administrateur Geo Wakanda", "Geo Wakanda", "Administrateur",
            hash_password(ADMIN_PASSWORD), now_iso(), now_iso(), now_iso()))
        print(f"[auth] Compte administrateur créé : {ADMIN_EMAIL}")
    else:
        pw_hash = u["password_hash"] if check_password(ADMIN_PASSWORD, u["password_hash"]) else hash_password(ADMIN_PASSWORD)
        db("UPDATE users SET role='admin', status='active', password_hash=? WHERE id=?", (pw_hash, u["id"]))


def setup_auth(app: FastAPI):
    """Ajoute le middleware de protection et les routes d'authentification.
    À appeler AVANT l'enregistrement de la route « catch-all » du frontend."""

    @app.on_event("startup")
    def _startup():
        if not AUTH_ENABLED:
            print("[auth] Contrôle d'accès DÉSACTIVÉ (AUTH_ENABLED=false).")
            return
        init_db()
        ensure_admin()
        print(f"[auth] Base : {'PostgreSQL' if IS_PG else 'SQLite (' + SQLITE_PATH + ')'} · "
              f"e-mails : {'Brevo' if BREVO_API_KEY else 'désactivés (liens dans les logs)'}")

    # ── Middleware : tout est protégé sauf les pages publiques ──
    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if ".." in path:
            return JSONResponse({"detail": "Chemin invalide"}, status_code=400)
        if request.method == "OPTIONS" or path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        user = await run_in_threadpool(session_user, request)
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Authentification requise", "code": "auth_required"}, status_code=401)
            return RedirectResponse(f"/login?next={quote(path)}", status_code=302)
        if (path.startswith("/admin") or path.startswith("/api/admin/")) and user["role"] != "admin":
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Réservé à l'administrateur"}, status_code=403)
            return RedirectResponse("/", status_code=302)
        request.state.user = user
        return await call_next(request)

    # ── Pages ──
    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if AUTH_ENABLED and session_user(request):
            return RedirectResponse("/", status_code=302)
        return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))

    @app.get("/reset-password", include_in_schema=False)
    def reset_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))

    @app.get("/verify-email", include_in_schema=False)
    def verify_email(request: Request, background: BackgroundTasks, token: str = ""):
        data = read_token(token, "verify")
        if not data:
            return RedirectResponse("/login?msg=invalid_link", status_code=302)
        u = get_user(int(data["uid"]))
        if not u:
            return RedirectResponse("/login?msg=invalid_link", status_code=302)
        if u["status"] == "pending_email":
            db("UPDATE users SET status='pending_approval', verified_at=? WHERE id=?", (now_iso(), u["id"]))
            invalidate_user(u["id"])
            background.add_task(mail_admin_new_request, base_url(request), u)
        return RedirectResponse("/login?msg=verified", status_code=302)

    # ── API publique d'authentification ──
    @app.post("/api/auth/register")
    def register(body: RegisterIn, request: Request, background: BackgroundTasks):
        email = body.email.strip().lower()
        name = _clean(body.name, 120)
        if not name:
            raise HTTPException(400, "Veuillez indiquer votre nom.")
        if not EMAIL_RE.match(email):
            raise HTTPException(400, "Adresse e-mail invalide.")
        if len(body.password) < 8:
            raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères.")
        fields = (name, _clean(body.organisation, 160), _clean(body.fonction, 120), _clean(body.motif, 1000))
        existing = get_user_by_email(email)
        if existing:
            if existing["status"] != "pending_email":
                raise HTTPException(409, "Une demande existe déjà pour cette adresse e-mail. Connectez-vous ou "
                                         "utilisez « Mot de passe oublié ».")
            db("UPDATE users SET name=?, organisation=?, fonction=?, motif=?, password_hash=? WHERE id=?",
               (*fields, hash_password(body.password), existing["id"]))
            uid = existing["id"]
        else:
            db("INSERT INTO users (name, organisation, fonction, motif, email, password_hash, role, status, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, 'user', 'pending_email', ?)",
               (*fields, email, hash_password(body.password), now_iso()))
            uid = get_user_by_email(email)["id"]
        background.add_task(mail_verification, base_url(request), get_user(uid))
        return {"ok": True, "email": email}

    @app.post("/api/auth/resend-verification")
    def resend(body: EmailIn, request: Request, background: BackgroundTasks):
        u = get_user_by_email(body.email)
        if u and u["status"] == "pending_email":
            background.add_task(mail_verification, base_url(request), u)
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request):
        email = body.email.strip().lower()
        key = _rate_key(request, email)
        if too_many_attempts(key):
            raise HTTPException(429, "Trop de tentatives. Réessayez dans 15 minutes.")
        u = get_user_by_email(email)
        if not u or not check_password(body.password, u["password_hash"]):
            record_failure(key)
            raise HTTPException(401, "E-mail ou mot de passe incorrect.")
        messages = {
            "pending_email": ("Vous devez d'abord confirmer votre adresse e-mail (vérifiez vos spams).", "pending_email"),
            "pending_approval": ("Votre demande est en attente de validation par l'équipe Geo Wakanda.", "pending_approval"),
            "rejected": ("Votre demande d'accès n'a pas été retenue.", "rejected"),
            "disabled": ("Votre compte a été désactivé. Contactez l'équipe Geo Wakanda.", "disabled"),
        }
        if u["status"] in messages:
            msg, code = messages[u["status"]]
            return JSONResponse({"detail": msg, "code": code}, status_code=403)
        db("UPDATE users SET last_login=? WHERE id=?", (now_iso(), u["id"]))
        tok = make_token("session", u["id"], SESSION_TTL, pv=pw_version(u))
        resp = JSONResponse({"ok": True, "redirect": _safe_next(body.next)})
        resp.set_cookie(COOKIE_NAME, tok, max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=is_https(request), path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/auth/me")
    def me(request: Request):
        if not AUTH_ENABLED:
            return {"auth_enabled": False}
        u = session_user(request)
        if not u:
            raise HTTPException(401, "Non connecté")
        return {"auth_enabled": True, **public_user(u)}

    @app.post("/api/auth/forgot")
    def forgot(body: EmailIn, request: Request, background: BackgroundTasks):
        u = get_user_by_email(body.email)
        if u and u["status"] in ("active", "pending_approval"):
            background.add_task(mail_reset, base_url(request), u)
        return {"ok": True}

    @app.post("/api/auth/reset")
    def reset(body: ResetIn):
        data = read_token(body.token, "reset")
        if not data:
            raise HTTPException(400, "Lien invalide ou expiré. Refaites une demande.")
        u = get_user(int(data["uid"]))
        if not u or data.get("pv") != pw_version(u):
            raise HTTPException(400, "Ce lien a déjà été utilisé ou a expiré.")
        if len(body.password) < 8:
            raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères.")
        if u["role"] == "admin":
            raise HTTPException(400, "Le mot de passe administrateur se modifie via la variable ADMIN_PASSWORD sur Railway.")
        db("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), u["id"]))
        invalidate_user(u["id"])
        return {"ok": True}

    # ── API administrateur (protégée par le middleware) ──
    def _target(uid: int, request: Request) -> Dict[str, Any]:
        u = get_user(uid)
        if not u:
            raise HTTPException(404, "Utilisateur introuvable")
        if u["id"] == request.state.user["id"]:
            raise HTTPException(400, "Vous ne pouvez pas modifier votre propre compte administrateur.")
        return u

    @app.get("/api/admin/users")
    def admin_users():
        rows = db("SELECT * FROM users ORDER BY created_at DESC", (), "all") or []
        return {"users": [public_user(r) for r in rows],
                "email_enabled": bool(BREVO_API_KEY and MAIL_SENDER_EMAIL)}

    @app.post("/api/admin/users/{uid}/approve")
    def admin_approve(uid: int, request: Request, background: BackgroundTasks):
        u = _target(uid, request)
        db("UPDATE users SET status='active', approved_at=?, verified_at=COALESCE(verified_at, ?) WHERE id=?",
           (now_iso(), now_iso(), uid))
        invalidate_user(uid)
        if u["status"] != "disabled":
            background.add_task(mail_approved, base_url(request), u)
        return {"ok": True}

    @app.post("/api/admin/users/{uid}/reject")
    def admin_reject(uid: int, body: RejectIn, request: Request, background: BackgroundTasks):
        u = _target(uid, request)
        note = _clean(body.note, 1000)
        db("UPDATE users SET status='rejected', admin_note=? WHERE id=?", (note, uid))
        invalidate_user(uid)
        background.add_task(mail_rejected, base_url(request), u, note)
        return {"ok": True}

    @app.post("/api/admin/users/{uid}/disable")
    def admin_disable(uid: int, request: Request):
        _target(uid, request)
        db("UPDATE users SET status='disabled' WHERE id=?", (uid,))
        invalidate_user(uid)
        return {"ok": True}

    @app.post("/api/admin/users/{uid}/resend")
    def admin_resend(uid: int, request: Request, background: BackgroundTasks):
        u = _target(uid, request)
        if u["status"] != "pending_email":
            raise HTTPException(400, "Cet e-mail est déjà vérifié.")
        background.add_task(mail_verification, base_url(request), u)
        return {"ok": True}

    @app.delete("/api/admin/users/{uid}")
    def admin_delete(uid: int, request: Request):
        _target(uid, request)
        db("DELETE FROM users WHERE id=?", (uid,))
        invalidate_user(uid)
        return {"ok": True}
