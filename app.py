"""Portfolio backend: serves the site and handles the contact form.

Run (dev):   python app.py
Run (prod):  gunicorn app:app
"""
import hmac
import json
import os
import re
import smtplib
import sqlite3
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "messages.db"))
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "")  # only if the site is on a different domain
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IST = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
if os.getenv("TRUST_PROXY") == "1":  # behind Render / Railway / Nginx
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------- database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                message TEXT NOT NULL,
                ip TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # add new columns to an older database without losing data
        have = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        for col in ("phone", "source", "campaign"):
            if col not in have:
                db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")


# ---------- helpers ----------
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limited(key, limit=5, window=3600):
    """In-memory limit: `limit` messages per `window` seconds per IP."""
    now = time.time()
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def clean_phone(raw):
    """Return a normalised phone number, or None if invalid."""
    p = re.sub(r"[\s\-().]", "", str(raw))
    if re.fullmatch(r"(\+?91|0)?[6-9]\d{9}", p):  # Indian mobile
        return "+91" + p[-10:]
    if re.fullmatch(r"\+\d{8,15}", p):  # other countries, must start with +
        return p
    return None


def clean_tag(raw):
    """utm_source / utm_campaign: keep it short and safe (letters, digits, - _ .)."""
    return re.sub(r"[^a-z0-9_.\-]", "", str(raw).lower())[:60]


def send_mail(lead):
    host, user, pw = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    to = os.getenv("MAIL_TO")
    if not (host and user and pw and to):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"New lead from {lead['name']} ({lead['source']})"
        msg["From"] = user
        msg["To"] = to
        msg["Reply-To"] = lead["email"]
        msg.set_content(
            f"Name: {lead['name']}\nMobile: {lead['phone']}\nEmail: {lead['email']}\n"
            f"Source: {lead['source']}  Campaign: {lead['campaign']}\n\n{lead['message']}"
        )
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
    except Exception as exc:
        app.logger.error("Email send failed: %s", exc)


def send_to_sheet(lead):
    """Add the lead as a new row in Google Sheets via an Apps Script web app."""
    url = os.getenv("SHEETS_WEBHOOK_URL")
    if not url:
        return
    try:
        payload = dict(lead, secret=os.getenv("SHEETS_SECRET", ""))
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as exc:
        app.logger.error("Google Sheet send failed: %s", exc)


@app.after_request
def add_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGIN and origin == ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


# ---------- routes ----------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.route("/api/contact", methods=["POST", "OPTIONS"])
def contact():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}

    if data.get("website"):  # honeypot: bots fill this hidden field
        return jsonify(ok=True)

    name = re.sub(r"\s+", " ", str(data.get("name", ""))).strip()
    email = str(data.get("email", "")).strip()
    message = str(data.get("message", "")).strip()
    phone = clean_phone(data.get("phone", ""))
    source = clean_tag(data.get("source", "")) or "direct"
    campaign = clean_tag(data.get("campaign", ""))

    errors = {}
    if not 2 <= len(name) <= 80:
        errors["name"] = "Please enter your name (2–80 characters)."
    if phone is None:
        errors["phone"] = "Please enter a valid mobile number, e.g. 98765 43210."
    if len(email) > 120 or not EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email address."
    if not 10 <= len(message) <= 2000:
        errors["message"] = "Message should be between 10 and 2000 characters."
    if errors:
        return jsonify(ok=False, errors=errors), 400

    ip = request.remote_addr or "unknown"
    if rate_limited(ip):
        return jsonify(ok=False, error="Too many messages. Please try again later."), 429

    db = get_db()
    db.execute(
        "INSERT INTO messages (name, phone, email, message, source, campaign, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, phone, email, message, source, campaign, ip),
    )
    db.commit()

    lead = {
        "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "name": name, "phone": phone, "email": email,
        "message": message, "source": source, "campaign": campaign,
    }
    threading.Thread(target=send_mail, args=(lead,), daemon=True).start()
    threading.Thread(target=send_to_sheet, args=(lead,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/messages")
def list_messages():
    """Private. Header:  X-Admin-Token: <ADMIN_TOKEN>"""
    token = os.getenv("ADMIN_TOKEN", "")
    supplied = request.headers.get("X-Admin-Token", "")
    if not token or not hmac.compare_digest(token.encode(), supplied.encode()):
        return jsonify(error="Unauthorized"), 401
    rows = get_db().execute(
        "SELECT id, name, phone, email, message, source, campaign, created_at "
        "FROM messages ORDER BY id DESC LIMIT 200"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
