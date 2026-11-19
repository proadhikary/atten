import csv
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, date, time, timedelta
from functools import wraps
from math import radians, sin, cos, sqrt, atan2
from urllib.parse import urlsplit

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   jsonify, abort, make_response, session, current_app)
from flask_login import (LoginManager, login_user, logout_user, login_required,
                         current_user)
from werkzeug.utils import secure_filename

import mailer
import notify
from config import Config
from models import (db, User, Attendance, LeaveRequest, Setting, Device,
                    SecurityFlag)

log = logging.getLogger(__name__)
login_manager = LoginManager()


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(os.path.join(app.root_path, "instance"), exist_ok=True)

    # Behind a reverse proxy (TRUST_PROXY=1) the app may be published under a
    # sub-path, e.g. https://example.org/attendance/. X-Forwarded-Prefix
    # tells url_for to include it, X-Forwarded-Proto keeps external links https.
    # x_for/x_host stay OFF: client_ip() reads X-Forwarded-For itself.
    if Config.TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_host=0,
                                x_proto=1, x_prefix=1)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message_category = "warning"
    app.jinja_env.filters["days"] = fmt_days

    register_routes(app)

    with app.app_context():
        db.create_all()
        run_migrations(app)
        seed_from_csv(app)
        ensure_defaults(app)

    return app


def run_migrations(app):
    """Add columns introduced after a DB was first created (SQLite-safe)."""
    from sqlalchemy import inspect, text

    added = []

    def add_column(table, column, ddl):
        if table not in insp.get_table_names():
            return
        if column in {c["name"] for c in insp.get_columns(table)}:
            return
        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        added.append(f"{table}.{column}")

    insp = inspect(db.engine)
    add_column("users", "marks_attendance",
               "marks_attendance BOOLEAN NOT NULL DEFAULT 1")
    add_column("users", "gender", "gender VARCHAR(10) NOT NULL DEFAULT 'male'")
    add_column("users", "left_on", "left_on DATE")
    add_column("users", "joined_on", "joined_on DATE")
    add_column("attendance", "is_voided", "is_voided BOOLEAN NOT NULL DEFAULT 0")
    add_column("attendance", "void_reason", "void_reason VARCHAR(255)")
    add_column("attendance", "device_id", "device_id INTEGER")
    add_column("attendance", "in_ip", "in_ip VARCHAR(45)")
    add_column("attendance", "out_ip", "out_ip VARCHAR(45)")
    add_column("leave_requests", "balance_at_request", "balance_at_request FLOAT")
    add_column("leave_requests", "is_half_day",
               "is_half_day BOOLEAN NOT NULL DEFAULT 0")

    if added:
        db.session.commit()
        app.logger.info("Migrated: added %s", ", ".join(added))


@login_manager.unauthorized_handler
def unauthorized():
    """API callers get a JSON 401; browsers get sent to the login page."""
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Not logged in — POST /login first "
                       "and send the session cookie."), 401
    flash(login_manager.login_message, login_manager.login_message_category)
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


GENDERS = ("male", "female")


def _gender(raw, fallback="male"):
    """Normalise a submitted gender, keeping the stored one if it isn't valid."""
    value = (raw or "").strip().lower()
    return value if value in GENDERS else (fallback or "male")


def still_in_lab(on=None):
    """SQL condition: the member had joined and not yet left as of `on`
    (default today). Anything that builds a roster — absentees, member counts,
    per-designation averages — filters through this, the same way
    counts_present gates days. SQL twin of User.in_lab_on."""
    on = on or date.today()
    return db.and_(db.or_(User.joined_on.is_(None), User.joined_on <= on),
                   db.or_(User.left_on.is_(None), User.left_on >= on))


def _left_on(raw):
    """Parse a submitted date field (last day, joining date); blank → None.
    Raises ValueError on a malformed date."""
    raw = (raw or "").strip()
    return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None


def _joined_on(raw):
    """Joining date from the roster CSV / forms: 'YYYY-MM' (the lab's people
    page only gives a month → 1st of that month) or a full 'YYYY-MM-DD'."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{1,2}", raw):
        raw += "-01"
    return datetime.strptime(raw, "%Y-%m-%d").date()


def allowed_file(filename):
    return "." in filename and \
        filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS


def save_photo(user, file):
    """Store an uploaded profile photo and delete the one it replaces.
    Returns an error message, or None when it saved (or there was nothing to do)."""
    if not file or not file.filename:
        return None
    if not allowed_file(file.filename):
        return "Unsupported image type. Use JPG, PNG, WEBP or GIF."
    ext = file.filename.rsplit(".", 1)[1].lower()
    fname = secure_filename(f"{user.lab_id}_{uuid.uuid4().hex[:8]}.{ext}")
    file.save(os.path.join(Config.UPLOAD_FOLDER, fname))
    if user.photo:
        old = os.path.join(Config.UPLOAD_FOLDER, user.photo)
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
    user.photo = fname
    return None


def discord_webhook():
    """Admin-set webhook wins; env var is the fallback for deployments."""
    return (Setting.get("discord_webhook") or Config.DISCORD_WEBHOOK_URL or "").strip()


def discord_enabled(event):
    """event: 'attendance' or 'flags'. Both default to on once a URL is set."""
    return Setting.get(f"discord_{event}", "1") == "1"


def webhook_url_problem(url):
    """Why `url` can't be saved as the Discord webhook, or None if it's fine.

    Empty is allowed (it turns notifications off). Anything else must be an
    absolute http(s) URL with a host.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return "That doesn't look like a valid URL."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "That doesn't look like a Discord webhook URL."
    return None


def notify_discord(payload, event="attendance"):
    url = discord_webhook()
    if url and discord_enabled(event):
        notify.send(url, payload)


# --------------------------------------------------------------------------- #
# Email notifications
#
# Leave is the one flow where a message in a Discord channel isn't enough: a
# member who raised a request isn't watching the channel, and an admin may be
# away from it for days. So requests mail the admins and decisions mail the
# member. Same contract as Discord — best-effort, never blocking, never fatal.
# --------------------------------------------------------------------------- #
def smtp_config():
    """Admin-set values win; env vars are the fallback for deployments.

    Returns None when no host/from address is configured, which is the single
    'email is off' signal every caller checks.
    """
    host = (Setting.get("smtp_host") or Config.SMTP_HOST or "").strip()
    from_addr = (Setting.get("smtp_from") or Config.SMTP_FROM or "").strip()
    if not host or not from_addr:
        return None
    try:
        port = int(Setting.get("smtp_port") or Config.SMTP_PORT)
    except (TypeError, ValueError):
        port = Config.SMTP_PORT
    security = Setting.get("smtp_security") or Config.SMTP_SECURITY
    if security not in ("starttls", "ssl", "none"):
        security = "starttls"
    username = (Setting.get("smtp_username") or Config.SMTP_USERNAME or "").strip()
    password = Setting.get("smtp_password") or Config.SMTP_PASSWORD or ""

    # Sent-folder copy. SMTP files nothing, so without this the account's own
    # Sent folder never shows what Atten sent. IMAP defaults to the same
    # account as SMTP — one mailbox, one login, in every setup we expect.
    imap_security = Setting.get("imap_security") or Config.IMAP_SECURITY
    if imap_security not in ("starttls", "ssl", "none"):
        imap_security = "ssl"
    try:
        imap_port = int(Setting.get("imap_port") or Config.IMAP_PORT)
    except (TypeError, ValueError):
        imap_port = Config.IMAP_PORT
    return {
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "password": password,
        "from_addr": from_addr,
        "from_name": (Setting.get("smtp_from_name")
                      or Config.SMTP_FROM_NAME or "Atten").strip(),
        "save_sent": Setting.get("email_save_sent", "0") == "1",
        "imap_host": imap_host(host),
        "imap_port": imap_port,
        "imap_security": imap_security,
        "imap_username": (Setting.get("imap_username")
                          or Config.IMAP_USERNAME or username).strip(),
        "imap_password": (Setting.get("imap_password")
                          or Config.IMAP_PASSWORD or password),
        "sent_folder": (Setting.get("sent_folder") or Config.SENT_FOLDER or "").strip(),
    }


def imap_host(smtp_host_):
    """Blank means 'the same provider': smtp.gmail.com → imap.gmail.com, which is
    the convention everywhere. An explicit setting always wins."""
    explicit = (Setting.get("imap_host") or Config.IMAP_HOST or "").strip()
    if explicit:
        return explicit
    if not smtp_host_:
        return ""
    return re.sub(r"^smtps?\.", "imap.", smtp_host_)


def email_enabled(event):
    """event: 'leave_request' or 'leave_decision'. Both default to on once SMTP is set."""
    return Setting.get(f"email_{event}", "1") == "1"


def admin_recipients():
    """Who hears about a new request: the explicit list if set, else every admin
    with an email on file. Deduplicated, case-insensitively."""
    raw = (Setting.get("email_admin_recipients") or "").strip()
    if raw:
        addrs = [a for a in re.split(r"[\s,;]+", raw) if a]
    else:
        addrs = [u.email for u in User.query.filter_by(is_admin=True).all() if u.email]
    seen, out = set(), []
    for a in addrs:
        key = a.lower()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def send_email(to, built, event):
    """`built` is the (subject, text, html) tuple a mailer builder returns."""
    cfg = smtp_config()
    if not cfg or not email_enabled(event):
        return
    subject, text = built
    mailer.send(cfg, to, subject, text)


def site_link(endpoint, **values):
    """Absolute URL for a link inside an email, built while the request is still
    alive (the sending thread has no request context)."""
    try:
        return url_for(endpoint, _external=True, **values)
    except RuntimeError:
        return None


def fmt_days(n):
    """2.5 → '2.5', 5.0 → '5' — leave counts read better without a bare '.0'."""
    n = round(float(n or 0), 2)
    return str(int(n)) if n == int(n) else f"{n:g}"


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in meters."""
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def lab_geofence():
    lat = Setting.get_float("lab_lat", Config.DEFAULT_LAB_LAT)
    lng = Setting.get_float("lab_lng", Config.DEFAULT_LAB_LNG)
    radius = Setting.get_float("lab_radius", Config.DEFAULT_LAB_RADIUS_M)
    return lat, lng, radius


# --------------------------------------------------------------------------- #
# Lab network check
#
# GPS is self-reported and trivially faked (DevTools sensor override, mock
# location apps). The source IP is not: forging it while holding a TCP session
# to this server is a different order of difficulty. Requiring the request to
# come from the lab's network is the cheapest real defence — but it only proves
# *which network*, never *which room*, so it complements the geofence.
# --------------------------------------------------------------------------- #
def client_ip():
    """The address this request really came from."""
    if Config.TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            # Left-most entry is the original client; the proxy appends its own.
            return fwd.split(",")[0].strip()
        real = request.headers.get("X-Real-IP", "").strip()
        if real:
            return real
    return request.remote_addr or ""


def lab_networks():
    """Parsed allowlist. Bare IPs become /32 (or /128) automatically."""
    raw = Setting.get("lab_networks", "") or ""
    nets = []
    for token in re.split(r"[\s,;]+", raw):
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue                      # ignore junk rather than lock everyone out
    return nets


def network_mode():
    """off — don't check · warn — allow but flag · enforce — block."""
    mode = Setting.get("network_mode", "off")
    return mode if mode in ("off", "warn", "enforce") else "off"


def geofence_mode():
    """off — don't check GPS at all · warn — allow but flag · enforce — block.

    "off" also tells the browser not to ask for a position: geolocation is only
    handed out on a secure origin, so on a plain-http test host asking would
    fail before the request is even sent.
    """
    mode = Setting.get("geofence_mode", Config.DEFAULT_GEOFENCE_MODE)
    return mode if mode in ("off", "warn", "enforce") else "enforce"


def _valid_cidr(token):
    try:
        ipaddress.ip_network(token, strict=False)
        return True
    except ValueError:
        return False


def ip_on_lab_network(ip):
    nets = lab_networks()
    if not nets:
        return True, "no allowlist configured"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, "unrecognised address"
    for net in nets:
        if addr in net:
            return True, str(net)
    return False, "not in the allowlist"


def is_on_approved_leave(user_id, day):
    return LeaveRequest.query.filter(
        LeaveRequest.user_id == user_id,
        LeaveRequest.status == "approved",
        LeaveRequest.start_date <= day,
        LeaveRequest.end_date >= day,
    ).first()


# --------------------------------------------------------------------------- #
# Attendance window
# --------------------------------------------------------------------------- #
def _parse_time(raw, fallback):
    try:
        h, m = raw.split(":")
        return time(int(h), int(m))
    except (AttributeError, ValueError):
        h, m = fallback.split(":")
        return time(int(h), int(m))


def attendance_window():
    """(check-in closes at, check-out closes at) — both local wall-clock times."""
    return (
        _parse_time(Setting.get("checkin_cutoff"), Config.DEFAULT_CHECKIN_CUTOFF),
        _parse_time(Setting.get("checkout_cutoff"), Config.DEFAULT_CHECKOUT_CUTOFF),
    )


def window_state(now=None):
    """What the member is allowed to do right now."""
    now = now or datetime.now()
    ci, co = attendance_window()
    return {
        "checkin_cutoff": ci,
        "checkout_cutoff": co,
        "can_check_in": now.time() <= ci,
        "can_check_out": now.time() <= co,
    }


# --------------------------------------------------------------------------- #
# Leave quota
#
# Members earn `rate` days every month and unused days roll forward, so the
# entitlement on any date is rate × (months elapsed in the current cycle).
# The launch cycle runs from the accrual start date to 31 Dec of that year;
# every cycle after it is a full Jan–Dec year.
#
# Female members accrue a *second*, parallel bucket on the same schedule —
# `menstrual_rate()` days per month — which may only be spent on leave of type
# MENSTRUAL_LEAVE. So a quota is really two buckets: menstrual days can spill
# into the general one once they run out, but general leave can never dip into
# the menstrual grant. `leave_summary` reports the combined totals at the top
# level (that is what a member is shown) with each bucket nested under it.
# --------------------------------------------------------------------------- #
MENSTRUAL_LEAVE = "menstrual"
def _months_between(a, b):
    """Inclusive count of calendar months from a's month to b's month."""
    return max(0, (b.year - a.year) * 12 + (b.month - a.month) + 1)


def menstrual_rate():
    """Extra days per month female members get, usable only as menstrual leave."""
    return Setting.get_float("menstrual_leave_rate",
                             Config.DEFAULT_MENSTRUAL_LEAVE_RATE)


def leave_policy():
    rate = Setting.get_float("leave_rate_per_month", Config.DEFAULT_LEAVE_RATE)
    raw = Setting.get("leave_accrual_start", Config.DEFAULT_LEAVE_START)
    try:
        start = datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        start = datetime.strptime(Config.DEFAULT_LEAVE_START, "%Y-%m-%d").date()
    return rate, start


def leave_cycle(on=None):
    """First and last day of the accrual cycle containing `on`."""
    on = on or date.today()
    _, accrual_start = leave_policy()
    if on.year <= accrual_start.year:
        return accrual_start, date(accrual_start.year, 12, 31)
    return date(on.year, 1, 1), date(on.year, 12, 31)


def _overlap_days(lr, start, end):
    s, e = max(lr.start_date, start), min(lr.end_date, end)
    if e < s:
        return 0
    # A half day is a single date, so it either falls in the window or it doesn't.
    return 0.5 if lr.is_half_day else (e - s).days + 1


def _month_window(d):
    """First and last day of the calendar month containing `d`."""
    first = d.replace(day=1)
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def _is_menstrual(lr):
    return (lr.leave_type or "") == MENSTRUAL_LEAVE


def _menstrual_by_month(rows, cs, ce, status="approved"):
    """Menstrual days used, split by the calendar month they fall in.

    Split because the grant is monthly and does not carry over: a request that
    straddles a month boundary spends each month's own day.
    """
    per_month = {}
    for l in rows:
        if l.status != status or not _is_menstrual(l):
            continue
        d = max(l.start_date, cs)
        while d <= min(l.end_date, ce):
            ms, me = _month_window(d)
            days = _overlap_days(l, max(ms, cs), min(me, ce))
            if days:
                per_month[ms] = per_month.get(ms, 0) + days
            d = me + timedelta(days=1)
    return per_month


def leave_summary(user_id, on=None):
    """Everything needed to show or enforce a member's quota on a given date.

    Two buckets. The general one accrues `rate` days a month and carries forward
    across the cycle. The menstrual one is a flat grant of `menstrual_rate()`
    days *per calendar month* for female members, which expires with the month —
    it never accumulates, so its balance is always about the month `on` falls in.
    Top-level numbers are the two added together (what the member is shown), with
    each bucket kept nested for anything that has to charge the right one.
    """
    on = on or date.today()
    rate, _ = leave_policy()
    cs, ce = leave_cycle(on)
    user = db.session.get(User, user_id)

    # Accrual starts at the later of the cycle start and the joining month:
    # someone who joined in May earns from May, not from January.
    acc = cs
    if user and user.joined_on and user.joined_on.replace(day=1) > cs:
        acc = user.joined_on.replace(day=1)
    months = _months_between(acc, min(on, ce)) if on >= acc else 0
    cycle_months = _months_between(acc, ce) if acc <= ce else 0

    m_rate = menstrual_rate() if (user and user.gets_menstrual_leave) else 0.0

    rows = LeaveRequest.query.filter(
        LeaveRequest.user_id == user_id,
        LeaveRequest.start_date <= ce,
        LeaveRequest.end_date >= cs).all()

    used_gen = sum(_overlap_days(l, cs, ce) for l in rows
                   if l.status == "approved" and not _is_menstrual(l))
    per_month = _menstrual_by_month(rows, cs, ce)

    # Each month's grant is spent on its own month; anything beyond it falls
    # through to the general balance rather than being refused, and unused days
    # simply lapse when the month ends.
    charged = {ms: min(used, m_rate) for ms, used in per_month.items()}
    overflow = sum(per_month[ms] - c for ms, c in charged.items())
    this_month = _month_window(on)[0]
    m_used = charged.get(this_month, 0) if months else 0

    m_earned = m_rate if months else 0.0
    menstrual = {
        "rate": m_rate, "month": this_month,
        "annual": round(m_rate * cycle_months, 2),
        "earned": m_earned, "used": round(m_used, 2),
        "balance": round(m_earned - m_used, 2),
        "used_cycle": round(sum(charged.values()), 2),
    }

    g_annual = round(rate * cycle_months, 2)
    g_earned = min(round(rate * months, 2), g_annual)
    g_used = round(used_gen + overflow, 2)
    general = {"rate": rate, "annual": g_annual, "earned": g_earned,
               "used": g_used, "balance": round(g_earned - g_used, 2)}

    return {
        "rate": round(rate + m_rate, 2), "months": months,
        "annual": round(g_annual + menstrual["annual"], 2),
        "cycle_start": cs, "cycle_end": ce, "accrues_from": acc,
        "earned": round(g_earned + m_earned, 2),
        "used": round(g_used + m_used, 2),
        "pending": sum(_overlap_days(l, cs, ce) for l in rows
                       if l.status == "pending"),
        "balance": round(general["balance"] + menstrual["balance"], 2),
        "general": general, "menstrual": menstrual,
        "has_menstrual": m_rate > 0,
    }


def bucket_balance(summary, leave_type):
    """The balance a request of this type actually draws on.

    Menstrual leave spends this month's grant first and then spills into the
    general balance, so its ceiling is the two combined; everything else is
    capped by the general bucket alone — the grant is not general-purpose leave.
    """
    if leave_type == MENSTRUAL_LEAVE:
        return summary["balance"]
    return summary["general"]["balance"]


def quota_check(lr):
    """How a single request sits against its owner's balance (excludes itself
    while pending, so the number is 'what's left before this request')."""
    s = leave_summary(lr.user_id)
    cs, ce = s["cycle_start"], s["cycle_end"]
    days = _overlap_days(lr, cs, ce)

    if _is_menstrual(lr):
        # A menstrual request is measured against the grant of every month it
        # touches (each expires on its own), plus the general balance behind it.
        m_rate = s["menstrual"]["rate"]
        rows = LeaveRequest.query.filter(
            LeaveRequest.user_id == lr.user_id, LeaveRequest.id != lr.id,
            LeaveRequest.start_date <= ce, LeaveRequest.end_date >= cs).all()
        others = _menstrual_by_month(rows, cs, ce)
        grant_left = mine_charged = 0.0
        for ms, mine in _menstrual_by_month([lr], cs, ce, lr.status).items():
            free = max(0.0, m_rate - others.get(ms, 0))
            grant_left += free
            mine_charged += min(mine, free)
        before = round(s["general"]["balance"] + grant_left, 2)
        if lr.status == "approved":
            # `used` already counts it — give back only what general paid for.
            before = round(before + days - mine_charged, 2)
    else:
        before = s["general"]["balance"]
        if lr.status == "approved":      # already counted in `used`
            before = round(before + days, 2)

    after = round(before - days, 2)
    return {"summary": s, "days": days, "before": before, "after": after,
            "menstrual": _is_menstrual(lr),
            "exceeds": after < 0, "over_by": round(-after, 2) if after < 0 else 0}


# --------------------------------------------------------------------------- #
# Device identity (anti-proxy)
#
# A browser cannot read a MAC address — nothing on the web can. The closest
# workable equivalent is to bind each account to a limited number of browsers:
# a random token in a long-lived HttpOnly cookie, backed by a hash of stable
# browser traits so clearing cookies doesn't hand out a fresh device slot.
# --------------------------------------------------------------------------- #
FP_KEYS = ("ua", "platform", "screen", "tz", "lang", "cores", "memory", "touch")


def device_fingerprint(sig):
    if not isinstance(sig, dict):
        return None
    joined = "|".join(str(sig.get(k, "")) for k in FP_KEYS)
    if len(joined.replace("|", "").strip()) < 8:
        return None                       # too little signal to be meaningful
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def device_label(ua):
    ua = ua or ""
    browser = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else
               "Chrome" if "Chrome/" in ua else "Firefox" if "Firefox/" in ua else
               "Safari" if "Safari/" in ua else "Browser")
    system = "device"
    for token, name in (("iPhone", "iPhone"), ("iPad", "iPad"),
                        ("Android", "Android"), ("Mac OS X", "macOS"),
                        ("Windows", "Windows"), ("Linux", "Linux")):
        if token in ua:
            system = name
            break
    return f"{browser} on {system}"


def resolve_device(user, token, fingerprint, ua, register=True):
    """Match this browser to one of the user's devices, registering it if there
    is a free slot. Returns (device, problem); `problem` is None when all good."""
    dev = Device.query.filter_by(token=token).first() if token else None
    if dev is None and fingerprint:
        dev = Device.query.filter_by(fingerprint=fingerprint,
                                     is_revoked=False).first()

    if dev is not None:
        if dev.user_id != user.id:
            owner = db.session.get(User, dev.user_id)
            who = f"{owner.name} ({owner.lab_id})" if owner else "another member"
            return None, {
                "kind": "shared_device", "device": dev, "owner": owner,
                "message": (f"This device is registered to {who}. Marking attendance "
                            "from someone else's device is not allowed — the attempt "
                            "has been flagged and today's attendance voided."),
                "detail": f"{user.name} ({user.lab_id}) used {dev.label or 'a device'} "
                          f"registered to {who}.",
            }
        if dev.is_revoked:
            return None, {
                "kind": "revoked", "device": dev, "owner": user,
                "message": "This device was removed by the admin. Ask them to allow it again.",
                "detail": "",
            }
        if token and dev.token != token:
            dev.token = token             # cookie was cleared; re-link by fingerprint
        if fingerprint and dev.fingerprint != fingerprint:
            dev.fingerprint = fingerprint
        if ua:
            dev.user_agent = ua[:300]
        dev.last_seen = datetime.utcnow()
        db.session.commit()
        return dev, None

    if not register:
        return None, None                 # check-out from an unknown browser is fine

    limit = Config.MAX_DEVICES_PER_USER
    if Device.query.filter_by(user_id=user.id, is_revoked=False).count() >= limit:
        return None, {
            "kind": "device_limit", "device": None, "owner": user,
            "message": (f"Attendance is limited to {limit} devices per account and yours "
                        "are already registered. Ask the admin to remove an old device."),
            "detail": f"{user.name} ({user.lab_id}) tried to add a {limit + 1}th device "
                      f"({device_label(ua)}).",
        }

    dev = Device(user_id=user.id, token=token or uuid.uuid4().hex,
                 fingerprint=fingerprint, label=device_label(ua),
                 user_agent=(ua or "")[:300], last_seen=datetime.utcnow())
    db.session.add(dev)
    db.session.commit()
    return dev, None


def raise_flag(user, problem, day=None):
    flag = SecurityFlag(
        user_id=user.id, day=day or date.today(), kind=problem["kind"],
        owner_id=problem["owner"].id if problem.get("owner") else None,
        device_id=problem["device"].id if problem.get("device") else None,
        detail=problem.get("detail") or problem.get("message"))
    db.session.add(flag)
    return flag


# Statuses that record leave rather than presence — voiding or restoring a day
# must never overwrite them with "present".
LEAVE_STATUSES = ("on_leave", "half_leave")


def void_attendance(user_id, day, reason):
    """Null out a day's attendance but keep the row as an audit trail."""
    att = Attendance.query.filter_by(user_id=user_id, day=day).first()
    if att is None:
        att = Attendance(user_id=user_id, day=day)
        db.session.add(att)
    att.is_voided = True
    att.void_reason = reason[:255]
    if att.status not in LEAVE_STATUSES:
        att.status = "voided"
    return att


# --------------------------------------------------------------------------- #
# Per-member statistics
# --------------------------------------------------------------------------- #
DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def member_stats(user, days):
    """One person's attendance picture over the last `days` days.

    Saturday and Sunday are optional: members may check in, and those days
    count towards "days present" and hours logged, but every average and rate
    here is taken over Mon–Fri only, so skipping a weekend never drags a
    number down and a short weekend visit never skews the typical day.
    Weekdays before joining or after leaving aren't counted either."""
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = Attendance.query.filter(
        Attendance.user_id == user.id,
        Attendance.day >= start,
        Attendance.day <= today).all()
    by_day = {r.day: r for r in rows}

    daily, worked, checkin_minutes = [], [], []
    dow_present, dow_count, hour_hist = {}, {}, {}
    present_days = leave_days = 0
    weekdays = weekday_present = weekday_absent = 0
    weekend_present = 0
    weekend_hours = 0.0

    d = start
    while d <= today:
        r = by_day.get(d)
        here = bool(r and r.counts_present)
        half = bool(r and r.status == "half_leave" and not r.is_voided)
        on_leave = bool(r and r.status == "on_leave" and not r.is_voided) or half
        w = d.weekday()
        if user.in_lab_on(d):
            dow_count[w] = dow_count.get(w, 0) + 1

        if here:
            present_days += 1
            dow_present[w] = dow_present.get(w, 0) + 1
            hour_hist[r.check_in.hour] = hour_hist.get(r.check_in.hour, 0) + 1
            if w < 5:
                checkin_minutes.append(r.check_in.hour * 60 + r.check_in.minute)
                if r.worked_hours:
                    worked.append(r.worked_hours)
            else:
                weekend_present += 1
                weekend_hours += r.worked_hours or 0
        if on_leave:
            leave_days += 0.5 if half else 1

        if w < 5 and user.in_lab_on(d):  # Mon–Fri baseline, while in the lab
            weekdays += 1
            if here:
                weekday_present += 1
            elif not on_leave:
                weekday_absent += 1

        if here:
            note = "in " + r.check_in.strftime("%H:%M") + (
                " · out " + r.check_out.strftime("%H:%M") if r.check_out
                else " · no check-out")
        elif on_leave:
            note = "Half day leave" if half else "On leave"
        elif r and r.is_voided:
            note = "Voided"
        elif not user.has_joined(d):
            note = "Before joining"
        elif w >= 5:
            note = "Weekend (optional)"
        else:
            note = "No record"

        daily.append({"label": d.strftime("%d %b"),
                      "value": r.worked_hours if (here and r.worked_hours) else 0,
                      "sub": f"{DOW_NAMES[w]} · {note}"})
        d += timedelta(days=1)

    dow = [{"label": DOW_NAMES[w],
            "value": round(100 * dow_present.get(w, 0) / dow_count[w]) if dow_count.get(w) else 0,
            "sub": f"present {dow_present.get(w, 0)} of {dow_count.get(w, 0)}"
                   + (" · optional" if w >= 5 else "")}
           for w in range(7)]
    hour_dist = [{"label": f"{h:02d}", "value": hour_hist.get(h, 0),
                  "sub": f"{h:02d}:00–{h:02d}:59"}
                 for h in range(6, 24)] if hour_hist else []

    avg_in = "—"
    if checkin_minutes:
        m = round(sum(checkin_minutes) / len(checkin_minutes))
        avg_in = f"{m // 60:02d}:{m % 60:02d}"

    return {
        "start": start, "days": days, "daily": daily, "dow": dow,
        "hour_dist": hour_dist, "present_days": present_days,
        "leave_days": leave_days, "weekday_absent": weekday_absent,
        "weekdays": weekdays,
        "rate": round(100 * weekday_present / weekdays) if weekdays else 0,
        "avg_in": avg_in,
        "avg_hours": round(sum(worked) / len(worked), 1) if worked else 0,
        "total_hours": round(sum(worked) + weekend_hours, 1),
        "weekday_present": weekday_present,
        "weekend_present": weekend_present,
        "weekend_hours": round(weekend_hours, 1),
        "has_data": present_days > 0,
    }


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def seed_from_csv(app):
    """Load people.csv into the users table (skips already-present lab_ids)."""
    csv_path = os.path.join(app.root_path, "static", "people.csv")
    if not os.path.exists(csv_path):
        return
    added = 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            lab_id = (row.get("lab_id") or "").strip()
            name = (row.get("name") or "").strip()
            if not lab_id or not name:
                continue
            try:
                joined = _joined_on(row.get("joined"))
            except ValueError:
                joined = None
            existing = User.query.filter_by(lab_id=lab_id).first()
            if existing:
                # Backfill only — an admin-set joining date is never overwritten.
                if existing.joined_on is None and joined:
                    existing.joined_on = joined
                    added += 1
                continue
            user = User(
                lab_id=lab_id,
                name=name,
                designation=(row.get("designation") or "").strip(),
                funding=(row.get("funding") or "").strip(),
                gender=_gender(row.get("gender")),
                joined_on=joined,
            )
            db.session.add(user)
            added += 1
    if added:
        db.session.commit()
        app.logger.info("Seeded/backfilled %d members from people.csv", added)


def ensure_defaults(app):
    # Bootstrap admin account
    admin_id = app.config["DEFAULT_ADMIN_ID"]
    admin = User.query.filter_by(lab_id=admin_id).first()
    if admin is None:
        admin = User(lab_id=admin_id, name="Lab Admin",
                     designation="Administrator", funding="—", is_admin=True,
                     marks_attendance=False)
        admin.set_password(app.config["DEFAULT_ADMIN_PASSWORD"])
        db.session.add(admin)
        db.session.commit()
        app.logger.info("Created default admin '%s'", admin_id)

    # Geofence defaults
    if Setting.get("lab_lat") is None:
        Setting.set("lab_lat", Config.DEFAULT_LAB_LAT)
    if Setting.get("lab_lng") is None:
        Setting.set("lab_lng", Config.DEFAULT_LAB_LNG)
    if Setting.get("lab_radius") is None:
        Setting.set("lab_radius", Config.DEFAULT_LAB_RADIUS_M)
    if Setting.get("lab_name") is None:
        Setting.set("lab_name", "My Lab")
    if Setting.get("geofence_mode") is None:
        Setting.set("geofence_mode", Config.DEFAULT_GEOFENCE_MODE)

    # Leave policy + attendance window defaults
    for key, value in (("leave_rate_per_month", Config.DEFAULT_LEAVE_RATE),
                       ("menstrual_leave_rate", Config.DEFAULT_MENSTRUAL_LEAVE_RATE),
                       ("leave_accrual_start", Config.DEFAULT_LEAVE_START),
                       ("checkin_cutoff", Config.DEFAULT_CHECKIN_CUTOFF),
                       ("checkout_cutoff", Config.DEFAULT_CHECKOUT_CUTOFF)):
        if Setting.get(key) is None:
            Setting.set(key, value)


# --------------------------------------------------------------------------- #
# Raw DB editor (/dbmod)
# --------------------------------------------------------------------------- #
# A generic row editor over every model, deliberately kept out of the Admin UI:
# it bypasses all business rules (leave statuses, void logic, quota), so it has
# its own login (any active admin's Lab ID + password) and its own session key
# rather than riding on the normal Flask-Login session.
DBMOD_MODELS = (User, Attendance, LeaveRequest, Device, SecurityFlag, Setting)
DBMOD_SESSION_KEY = "dbmod_uid"


def dbmod_model(table):
    for model in DBMOD_MODELS:
        if model.__tablename__ == table:
            return model
    abort(404)


def dbmod_kind(col):
    """Map a column to the input widget / parser used for it."""
    try:
        py = col.type.python_type
    except NotImplementedError:
        return "text"
    if py is bool:
        return "bool"
    if py is int:
        return "int"
    if py is float:
        return "float"
    if py is datetime:
        return "datetime"
    if py is date:
        return "date"
    return "textarea" if isinstance(col.type, db.Text) else "text"


def dbmod_parse(col, raw):
    """Turn a submitted form string into a column value (raises ValueError)."""
    kind = dbmod_kind(col)
    raw = raw if raw is not None else ""
    if kind == "bool":
        return raw in ("1", "true", "on")
    if kind in ("int", "float", "date", "datetime") and not raw.strip():
        if col.nullable:
            return None
        raise ValueError(f"{col.name} cannot be empty")
    if kind == "int":
        return int(raw.strip())
    if kind == "float":
        return float(raw.strip())
    if kind == "date":
        return date.fromisoformat(raw.strip())
    if kind == "datetime":
        return datetime.fromisoformat(raw.strip())
    return raw


def dbmod_input_value(col, value):
    """Format a stored value for its <input>."""
    if value is None:
        return ""
    kind = dbmod_kind(col)
    if kind == "datetime":
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if kind == "date":
        return value.isoformat()
    if kind == "bool":
        return "1" if value else "0"
    return str(value)


DBMOD_PER_PAGE = (25, 50, 100, 250, 1000)
DBMOD_DATE_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(.*)$")


def dbmod_points_at_users(col):
    return any(fk.column.table.name == "users" for fk in col.foreign_keys)


def dbmod_match(col, text):
    """A WHERE condition for `text` typed into a search box, or None when it
    can't mean anything for this column. Understands the column's type, so a
    Lab ID or name finds a user_id, and 18/09/2026 finds a date."""
    text = text.strip()
    if not text:
        return None
    kind = dbmod_kind(col)
    if dbmod_points_at_users(col):
        if text.isdigit():
            return col == int(text)
        like = f"%{text}%"
        return col.in_(db.select(User.id).where(
            db.or_(User.lab_id.ilike(like), User.name.ilike(like))))
    if kind == "int":
        return col == int(text) if text.lstrip("-").isdigit() else None
    if kind == "float":
        try:
            return col == float(text)
        except ValueError:
            return None
    if kind == "bool":
        low = text.lower()
        if low in ("1", "true", "yes", "y", "t"):
            return col.is_(True)
        if low in ("0", "false", "no", "n", "f"):
            return col.is_(False)
        return None
    if kind in ("date", "datetime"):
        # SQLite stores these as ISO text, so a prefix/contains match on the
        # string works for "2026-09", "2026-09-18" or "2026-09-18 09:1".
        m = DBMOD_DATE_RE.match(text)
        if m:  # 18/09/2026 or 18-09-2026 → 2026-09-18
            d, mo, y, rest = m.groups()
            text = f"{y}-{int(mo):02d}-{int(d):02d}{rest}"
        text = text.replace("T", " ")
        if not re.fullmatch(r"[\d: .-]+", text):
            return None
        return db.cast(col, db.String).like(f"%{text}%")
    return col.ilike(f"%{text}%")


def dbmod_pk(model):
    return list(model.__table__.primary_key.columns)[0]


def dbmod_get(model, pk_raw):
    try:
        pk = dbmod_parse(dbmod_pk(model), pk_raw)
    except ValueError:
        abort(404)
    row = db.session.get(model, pk)
    if row is None:
        abort(404)
    return row


def dbmod_user():
    """The admin signed in to /dbmod, re-checked on every request."""
    uid = session.get(DBMOD_SESSION_KEY)
    user = db.session.get(User, uid) if uid else None
    if user is None or not user.is_admin or not user.is_active_member:
        session.pop(DBMOD_SESSION_KEY, None)
        return None
    return user


def dbmod_csrf():
    """Per-session token for /dbmod forms — the app has no CSRF layer, and
    this is the one page where a forged POST could rewrite anything."""
    if "dbmod_csrf" not in session:
        session["dbmod_csrf"] = secrets.token_urlsafe(32)
    return session["dbmod_csrf"]


def dbmod_check_csrf():
    sent = request.form.get("csrf", "")
    if not hmac.compare_digest(sent, dbmod_csrf()):
        abort(400)


def dbmod_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if dbmod_user() is None:
            return redirect(url_for("dbmod"))
        if request.method == "POST":
            dbmod_check_csrf()
        return f(*args, **kwargs)
    return wrapper


def dbmod_apply_form(model, row, creating):
    """Write submitted fields onto `row`; returns a list of error strings."""
    errors = []
    pk = dbmod_pk(model)
    for col in model.__table__.columns:
        name = col.name
        if col is pk and not creating:
            continue  # primary keys are fixed once the row exists
        if model is User and name == "password_hash":
            # Never edit the hash itself: type a new password, or clear it to
            # force the first-login "set password" flow.
            if request.form.get("null__password_hash"):
                row.password_hash = None
            elif request.form.get("password_hash"):
                row.set_password(request.form["password_hash"])
            continue
        if col.nullable and request.form.get(f"null__{name}"):
            setattr(row, name, None)
            continue
        raw = request.form.get(name)
        if creating and not (raw or "").strip() and dbmod_kind(col) != "bool":
            # Left blank on a new row: let the column default / autoincrement
            # fill it, and let the DB reject it if there is none.
            continue
        try:
            setattr(row, name, dbmod_parse(col, raw))
        except ValueError as e:
            errors.append(f"{name}: {e}")
    return errors


def dbmod_form(model, row, creating):
    """GET renders the row form; POST applies it and redirects on success."""
    table = model.__tablename__
    if request.method == "POST":
        errors = dbmod_apply_form(model, row, creating)
        if not errors:
            try:
                if creating:
                    db.session.add(row)
                db.session.commit()
            except Exception as e:  # IntegrityError, FlushError, ...
                db.session.rollback()
                errors = [str(getattr(e, "orig", None) or e)]
        if not errors:
            pk_val = getattr(row, dbmod_pk(model).name)
            current_app.logger.warning("dbmod: %s %s %s#%s",
                               dbmod_user().lab_id,
                               "created" if creating else "updated",
                               table, pk_val)
            flash(f"{table} #{pk_val} {'created' if creating else 'saved'}.",
                  "success")
            return redirect(url_for("dbmod_edit", table=table, pk=pk_val))
        db.session.rollback()
        for err in errors:
            flash(err, "danger")
        if not creating:
            row = dbmod_get(model, str(getattr(row, dbmod_pk(model).name)))
    return render_template("dbmod/row.html", table=table, model=model,
                           row=row, creating=creating,
                           cols=list(model.__table__.columns),
                           pk=dbmod_pk(model), kind=dbmod_kind,
                           value=dbmod_input_value,
                           form=request.form if request.method == "POST" else None,
                           tables=[m.__tablename__ for m in DBMOD_MODELS],
                           dbmod_admin=dbmod_user(), csrf=dbmod_csrf())


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def register_routes(app):

    @app.context_processor
    def inject_globals():
        pending = open_flags = 0
        if current_user.is_authenticated and current_user.is_admin:
            pending = LeaveRequest.query.filter_by(status="pending").count()
            open_flags = SecurityFlag.query.filter_by(resolved=False).count()
        return dict(lab_name=Setting.get("lab_name", "My Lab"),
                    pending_leaves=pending, open_flags=open_flags,
                    today=date.today(),
                    FLAG_LABELS={"shared_device": "Shared device",
                                 "device_limit": "Device limit reached",
                                 "off_network": "Off the lab network",
                                 "off_geofence": "Outside the geofence"})

    # ---------------- Auth ---------------- #
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            lab_id = request.form.get("lab_id", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter(
                db.func.lower(User.lab_id) == lab_id.lower()).first()
            if user is None:
                flash("No member found with that Lab ID.", "danger")
                return redirect(url_for("login"))
            if not user.is_active_member:
                flash("This account is deactivated. Contact the admin.", "danger")
                return redirect(url_for("login"))
            if not user.has_password:
                # First-time login → set a password
                flash("First login — please create a password.", "info")
                return redirect(url_for("set_password", lab_id=user.lab_id))
            if not user.check_password(password):
                flash("Incorrect password.", "danger")
                return redirect(url_for("login"))
            login_user(user)
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html")

    @app.route("/set-password", methods=["GET", "POST"])
    def set_password():
        lab_id = request.values.get("lab_id", "").strip()
        user = User.query.filter(
            db.func.lower(User.lab_id) == lab_id.lower()).first()
        if user is None:
            flash("Unknown Lab ID.", "danger")
            return redirect(url_for("login"))
        if user.has_password:
            flash("Password already set. Please log in.", "warning")
            return redirect(url_for("login"))
        if request.method == "POST":
            pw = request.form.get("password", "")
            pw2 = request.form.get("password2", "")
            if len(pw) < 6:
                flash("Password must be at least 6 characters.", "danger")
            elif pw != pw2:
                flash("Passwords do not match.", "danger")
            else:
                user.set_password(pw)
                db.session.commit()
                login_user(user)
                flash("Password set. Welcome to Atten!", "success")
                return redirect(url_for("dashboard"))
        return render_template("set_password.html", user=user)

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Logged out.", "info")
        return redirect(url_for("login"))

    # ---------------- Dashboard ---------------- #
    @app.route("/")
    @login_required
    def dashboard():
        # Admin-only accounts don't mark attendance — send them to the admin area.
        if not current_user.marks_attendance:
            return redirect(url_for("admin_dashboard"))
        today = date.today()
        att = Attendance.query.filter_by(
            user_id=current_user.id, day=today).first()
        recent = Attendance.query.filter_by(user_id=current_user.id) \
            .order_by(Attendance.day.desc()).limit(10).all()
        # monthly stats
        month_start = today.replace(day=1)
        present_days = Attendance.query.filter(
            Attendance.user_id == current_user.id,
            Attendance.day >= month_start,
            Attendance.is_voided == False,  # noqa: E712
            Attendance.check_in.isnot(None)).count()
        my_leaves = LeaveRequest.query.filter_by(user_id=current_user.id) \
            .order_by(LeaveRequest.created_at.desc()).limit(5).all()
        pending_leave_count = LeaveRequest.query.filter_by(
            user_id=current_user.id, status="pending").count()
        lat, lng, radius = lab_geofence()
        on_leave = is_on_approved_leave(current_user.id, today)
        return render_template(
            "dashboard.html", att=att, recent=recent,
            present_days=present_days, my_leaves=my_leaves,
            pending_leave_count=pending_leave_count,
            lab_lat=lat, lab_lng=lng, lab_radius=radius, on_leave=on_leave,
            quota=leave_summary(current_user.id), window=window_state(),
            geofence_mode=geofence_mode())

    # ---------------- Attendance API ---------------- #
    @app.route("/api/attendance", methods=["POST"])
    @login_required
    def api_attendance():
        data = request.get_json(silent=True) or {}
        action = data.get("action")  # 'in' or 'out'
        if action not in ("in", "out"):
            return jsonify(ok=False, error="Unknown action."), 400

        now = datetime.now()
        today = now.date()

        # 0. Left the lab — no more check-ins after the last day. Check-out is
        #    still allowed so a last-day check-in can be closed properly.
        if action == "in" and current_user.has_left(today):
            return jsonify(
                ok=False,
                error=f"You left the lab on {current_user.left_on.strftime('%d %b %Y')}, "
                      "so attendance is closed. Ask the admin if this is wrong."), 403

        # 1. Time window — cheapest check, clearest message.
        win = window_state(now)
        if action == "in" and not win["can_check_in"]:
            return jsonify(
                ok=False,
                error=f"Check-in closed at {win['checkin_cutoff'].strftime('%H:%M')}. "
                      "If you were in the lab today, ask the admin to add it manually."), 403
        if action == "out" and not win["can_check_out"]:
            return jsonify(
                ok=False,
                error=f"Check-out closed at {win['checkout_cutoff'].strftime('%H:%M')}."), 403

        # 2. Lab network — checked before GPS, because unlike GPS it can't be
        #    faked from the member's own browser.
        ip = client_ip()
        on_net, why = ip_on_lab_network(ip)
        mode = network_mode()
        if not on_net and mode != "off":
            problem = {
                "kind": "off_network", "device": None, "owner": None,
                "message": ("You must be on the lab network (Wi-Fi) to mark attendance. "
                            f"Your connection ({ip}) is {why}."),
                "detail": f"{current_user.name} ({current_user.lab_id}) tried to check "
                          f"{action} from {ip}, which is {why}.",
            }
            if mode == "enforce":
                raise_flag(current_user, problem, today)
                db.session.commit()
                notify_discord(notify.flag_payload(
                    current_user, "off_network", problem["detail"], today), "flags")
                return jsonify(ok=False, error=problem["message"]), 403
            # warn: let it through, but leave a trail for the admin
            raise_flag(current_user, problem, today)
            db.session.commit()
            notify_discord(notify.flag_payload(
                current_user, "off_network", problem["detail"], today), "flags")

        # 3. Geofence — browser-reported GPS, so a convenience check rather
        #    than proof. Modes mirror the network check: off skips it entirely
        #    (and the position may be missing), warn flags, enforce blocks.
        geo_mode = geofence_mode()
        lat = lng = dist = None
        try:
            lat = float(data.get("lat"))
            lng = float(data.get("lng"))
        except (TypeError, ValueError):
            if geo_mode == "enforce":
                return jsonify(
                    ok=False,
                    error="Location unavailable. Enable GPS and try again."), 400
            lat = lng = None

        if lat is not None and geo_mode != "off":
            lab_lat, lab_lng, radius = lab_geofence()
            dist = haversine_m(lat, lng, lab_lat, lab_lng)
            if dist > radius:
                problem = {
                    "kind": "off_geofence", "device": None, "owner": None,
                    "message": (f"You are {int(dist)} m from the lab. You must be "
                                f"within {int(radius)} m to mark attendance."),
                    "detail": f"{current_user.name} ({current_user.lab_id}) marked "
                              f"{action} from {int(dist)} m away "
                              f"(limit {int(radius)} m).",
                }
                if geo_mode == "enforce":
                    return jsonify(ok=False, error=problem["message"],
                                   distance=round(dist, 1)), 403
                # warn: let it through, but leave a trail for the admin
                raise_flag(current_user, problem, today)
                db.session.commit()
                notify_discord(notify.flag_payload(
                    current_user, "off_geofence", problem["detail"], today), "flags")
        elif lat is not None:
            # Off: still record how far away they were, for the audit trail.
            lab_lat, lab_lng, radius = lab_geofence()
            dist = haversine_m(lat, lng, lab_lat, lab_lng)

        # 4. Device — is this browser one of the member's own?
        token = request.cookies.get(Config.DEVICE_COOKIE)
        fingerprint = device_fingerprint(data.get("device"))
        ua = request.headers.get("User-Agent", "")
        device, problem = resolve_device(current_user, token, fingerprint, ua,
                                         register=(action == "in"))
        if problem:
            if problem["kind"] in ("shared_device", "device_limit"):
                raise_flag(current_user, problem, today)
                if problem["kind"] == "shared_device":
                    void_attendance(current_user.id, today,
                                    "Proxy attempt — device belongs to another member")
                db.session.commit()
                notify_discord(notify.flag_payload(
                    current_user, problem["kind"], problem["detail"], today), "flags")
            return jsonify(ok=False, error=problem["message"]), 403

        att = Attendance.query.filter_by(
            user_id=current_user.id, day=today).first()
        if att and att.is_voided:
            return jsonify(
                ok=False,
                error="Today's attendance was voided by the admin. Please contact them."), 403

        if action == "in":
            if att and att.check_in:
                return jsonify(ok=False, error="Already checked in today."), 400
            if att is None:
                att = Attendance(user_id=current_user.id, day=today)
                db.session.add(att)
            att.check_in = now
            att.in_lat, att.in_lng = lat, lng
            att.distance_m = round(dist, 1) if dist is not None else None
            att.status = "present"
            att.device_id = device.id if device else None
            att.in_ip = ip[:45]
            db.session.commit()
            notify_discord(notify.attendance_payload(
                "in", current_user, now,
                {"Distance": f"{int(dist)} m from lab" if dist is not None
                             else "Location check off"}))
            payload = jsonify(ok=True, message="Checked in",
                              check_in=now.strftime("%H:%M"),
                              distance=round(dist, 1) if dist is not None else None)
        else:
            if att is None or not att.check_in:
                return jsonify(ok=False, error="You have not checked in yet."), 400
            if att.check_out:
                return jsonify(ok=False, error="Already checked out today."), 400
            att.check_out = now
            att.out_lat, att.out_lng = lat, lng
            att.out_ip = ip[:45]
            db.session.commit()
            extra = {"In": att.check_in.strftime("%H:%M")}
            if att.worked_hours is not None:
                extra["Hours"] = f"{att.worked_hours} h"
            notify_discord(notify.attendance_payload("out", current_user, now, extra))
            payload = jsonify(ok=True, message="Checked out",
                              check_out=now.strftime("%H:%M"),
                              hours=att.worked_hours)

        # Remember the browser so the next check-in is recognised instantly.
        resp = make_response(payload)
        if device:
            resp.set_cookie(Config.DEVICE_COOKIE, device.token,
                            max_age=Config.DEVICE_COOKIE_DAYS * 86400,
                            httponly=True, samesite="Lax",
                            secure=request.is_secure)
        return resp

    # ---------------- Profile ---------------- #
    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.method == "POST":
            form = request.form
            current_user.name = form.get("name", current_user.name).strip() or current_user.name
            current_user.email = form.get("email", "").strip()
            current_user.phone = form.get("phone", "").strip()
            current_user.bio = form.get("bio", "").strip()

            err = save_photo(current_user, request.files.get("photo"))
            if err:
                flash(err, "danger")
                return redirect(url_for("profile"))

            # optional password change
            new_pw = form.get("new_password", "")
            if new_pw:
                if len(new_pw) < 6:
                    flash("New password must be at least 6 characters.", "danger")
                    return redirect(url_for("profile"))
                if new_pw != form.get("new_password2", ""):
                    flash("New passwords do not match.", "danger")
                    return redirect(url_for("profile"))
                current_user.set_password(new_pw)
                flash("Password updated.", "success")

            db.session.commit()
            flash("Profile saved.", "success")
            return redirect(url_for("profile"))
        return render_template("profile.html")

    @app.route("/profile/left-lab", methods=["POST"])
    @login_required
    def profile_left_lab():
        """A member records (or withdraws) their own last day in the lab.
        Once that day has passed only the admin can undo it, so a leaver
        can't quietly put themselves back on the roster."""
        if request.form.get("action") == "clear":
            if current_user.has_left():
                flash("Your last day has already passed — ask the admin to change it.",
                      "warning")
            else:
                current_user.left_on = None
                db.session.commit()
                flash("Marked as still in the lab.", "success")
            return redirect(url_for("profile"))
        if current_user.has_left():
            flash("Your last day has already passed — ask the admin to change it.",
                  "warning")
            return redirect(url_for("profile"))
        try:
            last = _left_on(request.form.get("left_on"))
        except ValueError:
            last = None
        if last is None:
            flash("Please pick your last day in the lab.", "danger")
        elif current_user.joined_on and last < current_user.joined_on:
            flash("Your last day can't be before you joined the lab.", "danger")
        elif not request.form.get("confirm"):
            flash("Tick the box to confirm you are leaving the lab.", "danger")
        else:
            current_user.left_on = last
            db.session.commit()
            flash(f"Recorded — your last day in the lab is "
                  f"{last.strftime('%d %b %Y')}. Attendance closes after that.", "info")
        return redirect(url_for("profile"))

    # ---------------- Leaves ---------------- #
    @app.route("/leaves", methods=["GET", "POST"])
    @login_required
    def leaves():
        if not current_user.marks_attendance:
            return redirect(url_for("admin_leaves"))
        if request.method == "POST":
            try:
                start = datetime.strptime(request.form["start_date"], "%Y-%m-%d").date()
                end = datetime.strptime(request.form["end_date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                flash("Please provide valid dates.", "danger")
                return redirect(url_for("leaves"))
            half = bool(request.form.get("is_half_day"))
            if half:
                end = start          # a half day is always one date
            if end < start:
                flash("End date cannot be before start date.", "danger")
                return redirect(url_for("leaves"))
            if current_user.left_on and end > current_user.left_on:
                flash(f"Leave can't run past your last day in the lab "
                      f"({current_user.left_on.strftime('%d %b %Y')}).", "danger")
                return redirect(url_for("leaves"))
            leave_type = request.form.get("leave_type", "casual")
            # The menstrual grant only exists for members it was granted to —
            # the form hides the option, so reaching here means it was forged.
            if leave_type == MENSTRUAL_LEAVE and not current_user.gets_menstrual_leave:
                flash("Menstrual leave is not available on your account.", "danger")
                return redirect(url_for("leaves"))
            summary = leave_summary(current_user.id)
            available = bucket_balance(summary, leave_type)
            lr = LeaveRequest(
                user_id=current_user.id,
                leave_type=leave_type,
                start_date=start, end_date=end,
                reason=request.form.get("reason", "").strip(),
                balance_at_request=available,
                is_half_day=half,
            )
            db.session.add(lr)
            db.session.commit()

            send_email(admin_recipients(),
                       mailer.leave_request_email(
                           current_user, lr, quota_check(lr),
                           Setting.get("lab_name", "the lab"),
                           site_link("admin_leaves")),
                       "leave_request")

            # Over-quota requests still go through — the admin decides.
            over = round(lr.days - available, 2)
            if over > 0:
                flash(f"Request submitted, but it exceeds your quota by {fmt_days(over)} "
                      f"day(s) — you have {fmt_days(available)} left. "
                      "The admin can still approve it.", "warning")
            else:
                flash("Leave request submitted.", "success")
            return redirect(url_for("leaves"))
        my = LeaveRequest.query.filter_by(user_id=current_user.id) \
            .order_by(LeaveRequest.created_at.desc()).all()
        return render_template("leaves.html", leaves=my,
                               quota=leave_summary(current_user.id))

    @app.route("/leaves/<int:leave_id>/cancel", methods=["POST"])
    @login_required
    def cancel_leave(leave_id):
        lr = db.session.get(LeaveRequest, leave_id)
        if lr is None or lr.user_id != current_user.id:
            abort(404)
        if lr.status == "pending":
            # Built before the delete — the row's attributes are gone after it.
            built = mailer.leave_withdrawn_email(
                current_user, lr, Setting.get("lab_name", "the lab"),
                site_link("admin_leaves"))
            db.session.delete(lr)
            db.session.commit()
            send_email(admin_recipients(), built, "leave_request")
            flash("Leave request withdrawn.", "info")
        else:
            flash("Only pending requests can be withdrawn.", "warning")
        return redirect(url_for("leaves"))

    # ---------------- Admin ---------------- #
    @app.route("/admin")
    @login_required
    @admin_required
    def admin_dashboard():
        today = date.today()
        total_members = User.query.filter(User.is_admin == False,  # noqa: E712
                                          still_in_lab(today)).count()
        present_today = Attendance.query.filter(
            Attendance.day == today,
            Attendance.is_voided == False,  # noqa: E712
            Attendance.check_in.isnot(None)).count()
        pending = LeaveRequest.query.filter_by(status="pending").count()
        on_leave_today = LeaveRequest.query.filter(
            LeaveRequest.status == "approved",
            LeaveRequest.start_date <= today,
            LeaveRequest.end_date >= today).count()
        recent_leaves = LeaveRequest.query.filter_by(status="pending") \
            .order_by(LeaveRequest.created_at.desc()).limit(6).all()
        todays = Attendance.query.filter_by(day=today) \
            .order_by(Attendance.check_in.desc()).all()
        return render_template(
            "admin/dashboard.html", total_members=total_members,
            present_today=present_today, pending=pending,
            on_leave_today=on_leave_today, recent_leaves=recent_leaves,
            todays=todays)

    @app.route("/admin/leaves")
    @login_required
    @admin_required
    def admin_leaves():
        status = request.args.get("status", "pending")
        q = LeaveRequest.query
        if status in ("pending", "approved", "rejected"):
            q = q.filter_by(status=status)
        items = q.order_by(LeaveRequest.created_at.desc()).all()
        quotas = {l.id: quota_check(l) for l in items}
        return render_template("admin/leaves.html", leaves=items, status=status,
                               quotas=quotas)

    @app.route("/admin/leaves/<int:leave_id>/<decision>", methods=["POST"])
    @login_required
    @admin_required
    def admin_review_leave(leave_id, decision):
        lr = db.session.get(LeaveRequest, leave_id)
        if lr is None:
            abort(404)
        if decision not in ("approve", "reject"):
            abort(400)
        impact = quota_check(lr)
        lr.status = "approved" if decision == "approve" else "rejected"
        lr.admin_comment = request.form.get("comment", "").strip()
        lr.reviewed_by = current_user.id
        lr.reviewed_at = datetime.utcnow()
        db.session.commit()

        # If approved, mark on_leave attendance for those days (no double count)
        if lr.status == "approved":
            # A half day still has the member in the lab for part of it, so it gets
            # its own status and never blocks or overwrites a real check-in.
            leave_status = "half_leave" if lr.is_half_day else "on_leave"
            d = lr.start_date
            while d <= lr.end_date:
                att = Attendance.query.filter_by(user_id=lr.user_id, day=d).first()
                if att is None:
                    att = Attendance(user_id=lr.user_id, day=d, status=leave_status)
                    db.session.add(att)
                elif not att.check_in and not att.is_voided:
                    att.status = leave_status
                d += timedelta(days=1)
            db.session.commit()

        # Tell the member — they aren't watching the admin queue, and an
        # approved day changes what they're expected to do tomorrow.
        send_email([lr.user.email],
                   mailer.leave_decision_email(
                       lr.user, lr, current_user.name,
                       Setting.get("lab_name", "the lab"),
                       balance=bucket_balance(leave_summary(lr.user_id),
                                              lr.leave_type),
                       link=site_link("leaves")),
                   "leave_decision")

        if lr.status == "approved" and impact["exceeds"]:
            flash(f"Leave approved — {lr.user.name} is now {fmt_days(impact['over_by'])} "
                  "day(s) over quota for this cycle.", "warning")
        else:
            flash(f"Leave {lr.status}.", "success")
        return redirect(request.referrer or url_for("admin_leaves"))

    @app.route("/admin/members", methods=["GET", "POST"])
    @login_required
    @admin_required
    def admin_members():
        if request.method == "POST":
            lab_id = request.form.get("lab_id", "").strip()
            name = request.form.get("name", "").strip()
            if not lab_id or not name:
                flash("Lab ID and name are required.", "danger")
            elif User.query.filter(db.func.lower(User.lab_id) == lab_id.lower()).first():
                flash("A member with that Lab ID already exists.", "danger")
            else:
                try:
                    joined = _joined_on(request.form.get("joined_on"))
                except ValueError:
                    joined = None
                u = User(lab_id=lab_id, name=name,
                         designation=request.form.get("designation", "").strip(),
                         funding=request.form.get("funding", "").strip(),
                         gender=_gender(request.form.get("gender")),
                         joined_on=joined)
                db.session.add(u)
                db.session.commit()
                flash(f"Added {name}.", "success")
            return redirect(url_for("admin_members"))
        # People who have left sink below everyone still in the lab.
        members = User.query.order_by(User.is_admin.desc(),
                                      User.left_on.isnot(None), User.name).all()
        flagged = {f.user_id for f in
                   SecurityFlag.query.filter_by(resolved=False).all()}
        return render_template("admin/members.html", members=members,
                               flagged=flagged)

    @app.route("/admin/members/<int:uid>")
    @login_required
    @admin_required
    def admin_member(uid):
        member = db.session.get(User, uid)
        if member is None:
            abort(404)
        try:
            days = int(request.args.get("days", 90))
        except ValueError:
            days = 90
        if days not in (30, 90, 180, 365):
            days = 90

        stats = member_stats(member, days)
        recent = Attendance.query.filter_by(user_id=uid) \
            .order_by(Attendance.day.desc()).limit(15).all()
        member_leaves = LeaveRequest.query.filter_by(user_id=uid) \
            .order_by(LeaveRequest.start_date.desc()).all()
        devices = Device.query.filter_by(user_id=uid) \
            .order_by(Device.is_revoked, Device.last_seen.desc()).all()
        flags = SecurityFlag.query.filter(
            (SecurityFlag.user_id == uid) | (SecurityFlag.owner_id == uid)) \
            .order_by(SecurityFlag.created_at.desc()).limit(10).all()

        return render_template(
            "admin/member.html", member=member, stats=stats, days=days,
            recent=recent, member_leaves=member_leaves, devices=devices,
            flags=flags, quota=leave_summary(uid),
            device_limit=Config.MAX_DEVICES_PER_USER)

    @app.route("/admin/members/<int:uid>/edit", methods=["POST"])
    @login_required
    @admin_required
    def admin_edit_member(uid):
        u = db.session.get(User, uid)
        if u is None:
            abort(404)
        back = request.referrer or url_for("admin_members")

        form = request.form
        new_id = form.get("lab_id", "").strip()
        name = form.get("name", "").strip()
        if not new_id or not name:
            flash("Lab ID and name are required.", "danger")
            return redirect(back)

        clash = User.query.filter(db.func.lower(User.lab_id) == new_id.lower(),
                                  User.id != u.id).first()
        if clash:
            flash(f"Lab ID “{new_id}” already belongs to {clash.name}.", "danger")
            return redirect(back)

        try:
            left_on = _left_on(form.get("left_on"))
            joined_on = _joined_on(form.get("joined_on"))
        except ValueError:
            flash("Joining date and last day in lab must be valid dates.", "danger")
            return redirect(back)
        if joined_on and left_on and left_on < joined_on:
            flash("Last day in lab can't be before the joining date.", "danger")
            return redirect(back)

        err = save_photo(u, request.files.get("photo"))
        if err:
            flash(err, "danger")
            return redirect(back)

        id_changed = new_id.lower() != u.lab_id.lower()
        u.lab_id = new_id
        u.name = name
        u.designation = form.get("designation", "").strip()
        u.funding = form.get("funding", "").strip()
        u.gender = _gender(form.get("gender"), u.gender)
        u.email = form.get("email", "").strip()
        u.phone = form.get("phone", "").strip()
        u.bio = form.get("bio", "").strip()
        u.left_on = left_on
        u.joined_on = joined_on
        db.session.commit()

        if id_changed:
            flash(f"Saved. {u.name} must now log in with Lab ID “{u.lab_id}”.", "warning")
        else:
            flash(f"Saved changes to {u.name}.", "success")
        return redirect(back)

    @app.route("/admin/members/<int:uid>/toggle-admin", methods=["POST"])
    @login_required
    @admin_required
    def admin_toggle_admin(uid):
        u = db.session.get(User, uid)
        if u is None:
            abort(404)
        if u.id == current_user.id:
            flash("You cannot change your own admin status.", "warning")
        else:
            u.is_admin = not u.is_admin
            db.session.commit()
            flash(f"{u.name} is {'now an admin' if u.is_admin else 'no longer an admin'}.", "success")
        return redirect(url_for("admin_members"))

    @app.route("/admin/members/<int:uid>/toggle-active", methods=["POST"])
    @login_required
    @admin_required
    def admin_toggle_active(uid):
        u = db.session.get(User, uid)
        if u is None:
            abort(404)
        if u.id == current_user.id:
            flash("You cannot deactivate yourself.", "warning")
        else:
            u.is_active_member = not u.is_active_member
            db.session.commit()
            flash(f"{u.name} {'activated' if u.is_active_member else 'deactivated'}.", "success")
        return redirect(url_for("admin_members"))

    @app.route("/admin/members/<int:uid>/toggle-attendance", methods=["POST"])
    @login_required
    @admin_required
    def admin_toggle_attendance(uid):
        u = db.session.get(User, uid)
        if u is None:
            abort(404)
        u.marks_attendance = not u.marks_attendance
        db.session.commit()
        flash(f"{u.name} {'now marks attendance' if u.marks_attendance else 'is now admin-only (no attendance)'}.",
              "success")
        return redirect(url_for("admin_members"))

    @app.route("/admin/members/<int:uid>/reset-password", methods=["POST"])
    @login_required
    @admin_required
    def admin_reset_password(uid):
        u = db.session.get(User, uid)
        if u is None:
            abort(404)
        u.password_hash = None
        db.session.commit()
        flash(f"Password for {u.name} reset. They will set a new one on next login.", "success")
        return redirect(url_for("admin_members"))

    @app.route("/admin/attendance")
    @login_required
    @admin_required
    def admin_attendance():
        day_str = request.args.get("date")
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date() if day_str else date.today()
        except ValueError:
            day = date.today()
        records = Attendance.query.filter_by(day=day) \
            .join(User).order_by(User.name).all()
        # members with no record today
        present_ids = {r.user_id for r in records}
        absentees = User.query.filter(
            User.is_admin == False,  # noqa: E712
            User.is_active_member == True,  # noqa: E712
            still_in_lab(day),
            ~User.id.in_(present_ids) if present_ids else True).all()
        return render_template("admin/attendance.html", records=records,
                               day=day, absentees=absentees)

    @app.route("/admin/attendance/<int:aid>/void", methods=["POST"])
    @login_required
    @admin_required
    def admin_void_attendance(aid):
        att = db.session.get(Attendance, aid)
        if att is None:
            abort(404)
        if att.is_voided:
            att.is_voided = False
            att.void_reason = None
            att.status = att.status if att.status in LEAVE_STATUSES else "present"
            flash("Attendance restored.", "success")
        else:
            att.is_voided = True
            att.void_reason = f"Voided by {current_user.name}"
            if att.status not in LEAVE_STATUSES:
                att.status = "voided"
            flash("Attendance voided — it no longer counts anywhere.", "warning")
        db.session.commit()
        return redirect(request.referrer or url_for("admin_attendance"))

    # ---------------- Security / anti-proxy ---------------- #
    @app.route("/admin/security")
    @login_required
    @admin_required
    def admin_security():
        show = request.args.get("show", "open")
        q = SecurityFlag.query
        if show == "open":
            q = q.filter_by(resolved=False)
        elif show == "resolved":
            q = q.filter_by(resolved=True)
        flags = q.order_by(SecurityFlag.created_at.desc()).limit(100).all()
        devices = Device.query.join(User).order_by(
            Device.is_revoked, User.name, Device.last_seen.desc()).all()
        return render_template("admin/security.html", flags=flags,
                               devices=devices, show=show,
                               device_limit=Config.MAX_DEVICES_PER_USER)

    @app.route("/admin/security/flags/<int:fid>/<action>", methods=["POST"])
    @login_required
    @admin_required
    def admin_resolve_flag(fid, action):
        flag = db.session.get(SecurityFlag, fid)
        if flag is None:
            abort(404)
        if action not in ("uphold", "dismiss"):
            abort(400)

        if action == "dismiss":
            # False alarm. Restore the day and stop the same fingerprint from
            # matching again — the real owner still has their device cookie.
            att = Attendance.query.filter_by(user_id=flag.user_id,
                                             day=flag.day).first()
            if att and att.is_voided:
                att.is_voided = False
                att.void_reason = None
                att.status = att.status if att.status in LEAVE_STATUSES else "present"
                if not att.check_in:
                    db.session.delete(att)
            if flag.device and flag.device.fingerprint:
                flag.device.fingerprint = None
            flag.resolution = "Dismissed — not a proxy"
            flash("Flag dismissed and the day restored.", "success")
        else:
            void_attendance(flag.user_id, flag.day,
                            f"Proxy confirmed by {current_user.name}")
            flag.resolution = "Upheld — attendance voided"
            flash("Flag upheld. That day's attendance stays voided.", "warning")

        flag.resolved = True
        flag.resolved_by = current_user.id
        flag.resolved_at = datetime.utcnow()
        db.session.commit()
        return redirect(request.referrer or url_for("admin_security"))

    @app.route("/admin/devices/<int:did>/toggle", methods=["POST"])
    @login_required
    @admin_required
    def admin_toggle_device(did):
        dev = db.session.get(Device, did)
        if dev is None:
            abort(404)
        dev.is_revoked = not dev.is_revoked
        db.session.commit()
        flash(f"Device {'removed — the slot is free again' if dev.is_revoked else 'allowed again'}.",
              "success")
        return redirect(request.referrer or url_for("admin_security"))

    @app.route("/admin/statistics")
    @login_required
    @admin_required
    def admin_statistics():
        try:
            days = int(request.args.get("days", 30))
        except ValueError:
            days = 30
        if days not in (30, 90, 180, 365):
            days = 30

        today = date.today()
        start = today - timedelta(days=days - 1)

        records = Attendance.query.filter(Attendance.day >= start,
                                          Attendance.day <= today).all()
        present = [r for r in records if r.counts_present]

        # --- 1. Daily attendance trend ---
        per_day = {}
        for r in present:
            per_day[r.day] = per_day.get(r.day, 0) + 1
        daily = []
        d = start
        while d <= today:
            daily.append({"label": d.strftime("%d %b"),
                          "value": per_day.get(d, 0),
                          "sub": d.strftime("%a")})
            d += timedelta(days=1)

        # --- 2. Check-in hour distribution ---
        hours = {}
        for r in present:
            hours[r.check_in.hour] = hours.get(r.check_in.hour, 0) + 1
        hour_dist = [{"label": f"{h:02d}", "value": hours.get(h, 0),
                      "sub": f"{h:02d}:00–{h:02d}:59"}
                     for h in range(6, 24)] if hours else []

        # --- 3. Day-of-week average ---
        dow_total, dow_count = {}, {}
        d = start
        while d <= today:
            w = d.weekday()
            dow_count[w] = dow_count.get(w, 0) + 1
            dow_total[w] = dow_total.get(w, 0) + per_day.get(d, 0)
            d += timedelta(days=1)
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow = [{"label": names[w],
                "value": round(dow_total.get(w, 0) / dow_count[w], 1) if dow_count.get(w) else 0,
                "sub": f"avg over {dow_count.get(w, 0)} {names[w]}s"}
               for w in range(7)]

        # --- 4. Attendance by designation (avg days present per member) ---
        users = User.query.filter_by(is_active_member=True).all()
        by_uid = {u.id: u for u in users}
        # Only people who were in the lab for some of the window form a group's
        # headcount (top members below still use by_uid, which keeps leavers).
        users = [u for u in users if not u.has_left(start)]

        def desig_key(u):
            """Group case-insensitively ('PhD ' == 'PhD') but keep the original
            spelling for display, so acronyms stay MS / PhD, not Ms / Phd."""
            raw = " ".join((u.designation or "").split()) or "Unspecified"
            return raw.lower(), raw

        desig_days, desig_people, desig_label = {}, {}, {}
        for u in users:
            if not u.marks_attendance:
                continue
            k, label = desig_key(u)
            desig_label.setdefault(k, label)
            desig_people.setdefault(k, set()).add(u.id)
            desig_days.setdefault(k, 0)
        for r in present:
            u = by_uid.get(r.user_id)
            if not u or not u.marks_attendance:
                continue
            k, _ = desig_key(u)
            if k in desig_days:
                desig_days[k] += 1
        by_designation = sorted(
            [{"label": desig_label[k],
              "value": round(desig_days.get(k, 0) / len(desig_people[k]), 1),
              "sub": f"{len(desig_people[k])} member(s) · {desig_days.get(k, 0)} check-ins"}
             for k in desig_people],
            key=lambda x: -x["value"])

        # --- 5. Top members by days present ---
        per_user = {}
        for r in present:
            per_user[r.user_id] = per_user.get(r.user_id, 0) + 1
        top = sorted(per_user.items(), key=lambda kv: -kv[1])[:10]
        top_members = [{"label": by_uid[uid].name if uid in by_uid else "—",
                        "value": n,
                        "sub": by_uid[uid].lab_id if uid in by_uid else ""}
                       for uid, n in top]

        # --- 6. Leave stats ---
        lv = LeaveRequest.query.filter(LeaveRequest.start_date >= start).all()
        leave_status = {"approved": 0, "pending": 0, "rejected": 0}
        leave_type = {}
        for l in lv:
            leave_status[l.status] = leave_status.get(l.status, 0) + 1
            leave_type[l.leave_type] = leave_type.get(l.leave_type, 0) + 1
        by_type = sorted([{"label": k.capitalize(), "value": v, "sub": f"{v} request(s)"}
                          for k, v in leave_type.items()], key=lambda x: -x["value"])

        # --- 7. Headline numbers ---
        total_checkins = len(present)
        avg_daily = round(total_checkins / days, 1) if days else 0
        peak = max(per_day.values()) if per_day else 0
        peak_day = max(per_day, key=per_day.get).strftime("%d %b") if per_day else "—"
        worked = [r.worked_hours for r in present if r.worked_hours is not None]
        avg_hours = round(sum(worked) / len(worked), 1) if worked else 0

        return render_template(
            "admin/statistics.html", days=days, start=start,
            daily=daily, hour_dist=hour_dist, dow=dow,
            by_designation=by_designation, top_members=top_members,
            leave_status=leave_status, by_type=by_type,
            total_checkins=total_checkins, avg_daily=avg_daily,
            peak=peak, peak_day=peak_day, avg_hours=avg_hours,
            has_data=bool(present))

    @app.route("/admin/settings", methods=["GET", "POST"])
    @login_required
    @admin_required
    def admin_settings():
        if request.method == "POST":
            section = request.form.get("section", "geofence")

            if section == "geofence":
                try:
                    lat = float(request.form["lab_lat"])
                    lng = float(request.form["lab_lng"])
                    radius = float(request.form["lab_radius"])
                except (KeyError, ValueError):
                    flash("Please enter valid numbers for location and radius.", "danger")
                    return redirect(url_for("admin_settings"))
                Setting.set("lab_lat", lat)
                Setting.set("lab_lng", lng)
                Setting.set("lab_radius", radius)
                Setting.set("lab_name",
                            request.form.get("lab_name", "My Lab").strip() or "My Lab")
                mode = request.form.get("geofence_mode", "enforce")
                Setting.set("geofence_mode",
                            mode if mode in ("off", "warn", "enforce") else "enforce")
                flash("Geofence settings saved."
                      + (" Location checking is OFF — anyone can mark attendance "
                         "from anywhere." if mode == "off" else ""),
                      "warning" if mode == "off" else "success")

            elif section == "policy":
                try:
                    rate = float(request.form["leave_rate"])
                    m_rate = float(request.form["menstrual_rate"])
                    start_raw = request.form["accrual_start"]
                    datetime.strptime(start_raw, "%Y-%m-%d")
                except (KeyError, ValueError):
                    flash("Please enter a valid monthly rate and start date.", "danger")
                    return redirect(url_for("admin_settings"))
                if rate < 0 or rate > 31 or m_rate < 0 or m_rate > 31:
                    flash("Monthly leave must be between 0 and 31 days.", "danger")
                    return redirect(url_for("admin_settings"))
                ci = _parse_time(request.form.get("checkin_cutoff"),
                                 Config.DEFAULT_CHECKIN_CUTOFF)
                co = _parse_time(request.form.get("checkout_cutoff"),
                                 Config.DEFAULT_CHECKOUT_CUTOFF)
                if co < ci:
                    flash("Check-out must close no earlier than check-in.", "danger")
                    return redirect(url_for("admin_settings"))
                Setting.set("leave_rate_per_month", rate)
                Setting.set("menstrual_leave_rate", m_rate)
                Setting.set("leave_accrual_start", start_raw)
                Setting.set("checkin_cutoff", ci.strftime("%H:%M"))
                Setting.set("checkout_cutoff", co.strftime("%H:%M"))
                flash("Leave and attendance rules saved.", "success")

            elif section == "network":
                raw = request.form.get("lab_networks", "").strip()
                bad = [t for t in re.split(r"[\s,;]+", raw) if t and not _valid_cidr(t)]
                if bad:
                    flash("Not a valid IP or range: " + ", ".join(bad[:3]), "danger")
                    return redirect(url_for("admin_settings"))
                mode = request.form.get("network_mode", "off")
                if mode not in ("off", "warn", "enforce"):
                    mode = "off"
                if mode != "off" and not raw:
                    flash("Add at least one network before turning the check on.", "danger")
                    return redirect(url_for("admin_settings"))
                Setting.set("lab_networks", raw)
                Setting.set("network_mode", mode)
                flash({"off": "Lab network check turned off.",
                       "warn": "Lab network check on — off-network attempts will be flagged.",
                       "enforce": "Lab network check enforced — off-network attempts are blocked."}[mode],
                      "success")

            elif section == "discord":
                url = request.form.get("discord_webhook", "").strip()
                problem = webhook_url_problem(url)
                if problem:
                    flash(problem, "danger")
                    return redirect(url_for("admin_settings"))
                Setting.set("discord_webhook", url)
                Setting.set("discord_attendance",
                            "1" if request.form.get("discord_attendance") else "0")
                Setting.set("discord_flags",
                            "1" if request.form.get("discord_flags") else "0")

                if request.form.get("test") and url:
                    sent, detail = notify.send(
                        (url),
                        notify.test_payload(Setting.get("lab_name", "the lab")),
                        blocking=True)
                    flash("Test message sent to Discord." if sent
                          else f"Discord rejected the test: {detail}",
                          "success" if sent else "danger")
                else:
                    flash("Discord settings saved." if url
                          else "Discord notifications turned off.", "success")

            elif section == "email":
                host = request.form.get("smtp_host", "").strip()
                from_addr = request.form.get("smtp_from", "").strip()
                try:
                    port = int(request.form.get("smtp_port") or Config.SMTP_PORT)
                except ValueError:
                    port = 0
                if not 1 <= port <= 65535:
                    flash("SMTP port must be a number between 1 and 65535.", "danger")
                    return redirect(url_for("admin_settings"))
                if host and not from_addr:
                    flash("Set the 'From' address — a mail server won't accept "
                          "a message without one.", "danger")
                    return redirect(url_for("admin_settings"))
                bad = [a for a in re.split(r"[\s,;]+",
                                           request.form.get("email_admin_recipients", ""))
                       if a and "@" not in a]
                if bad:
                    flash("Not a valid email address: " + ", ".join(bad[:3]), "danger")
                    return redirect(url_for("admin_settings"))

                security = request.form.get("smtp_security", "starttls")
                if security not in ("starttls", "ssl", "none"):
                    security = "starttls"
                Setting.set("smtp_host", host)
                Setting.set("smtp_port", port)
                Setting.set("smtp_security", security)
                Setting.set("smtp_username", request.form.get("smtp_username", "").strip())
                # Blank means "keep the stored password" — the form never renders
                # it back, so an admin editing the port can't wipe it by accident.
                new_pw = request.form.get("smtp_password", "")
                if new_pw:
                    Setting.set("smtp_password", new_pw)
                elif request.form.get("clear_password"):
                    Setting.set("smtp_password", "")
                Setting.set("smtp_from", from_addr)
                Setting.set("smtp_from_name",
                            request.form.get("smtp_from_name", "").strip() or "Atten")
                Setting.set("email_admin_recipients",
                            request.form.get("email_admin_recipients", "").strip())
                Setting.set("email_leave_request",
                            "1" if request.form.get("email_leave_request") else "0")
                Setting.set("email_leave_decision",
                            "1" if request.form.get("email_leave_decision") else "0")

                # Sent-folder copy. Every IMAP field is optional — blank means
                # "same as SMTP", which is the right answer for a single mailbox.
                save_sent = bool(request.form.get("email_save_sent"))
                try:
                    iport = int(request.form.get("imap_port") or Config.IMAP_PORT)
                except ValueError:
                    iport = 0
                if save_sent and not 1 <= iport <= 65535:
                    flash("IMAP port must be a number between 1 and 65535.", "danger")
                    return redirect(url_for("admin_settings"))
                isec = request.form.get("imap_security", "ssl")
                if isec not in ("starttls", "ssl", "none"):
                    isec = "ssl"
                Setting.set("imap_host", request.form.get("imap_host", "").strip())
                Setting.set("imap_port", iport or Config.IMAP_PORT)
                Setting.set("imap_security", isec)
                Setting.set("imap_username", request.form.get("imap_username", "").strip())
                new_ipw = request.form.get("imap_password", "")
                if new_ipw:
                    Setting.set("imap_password", new_ipw)
                elif request.form.get("clear_imap_password"):
                    Setting.set("imap_password", "")
                Setting.set("sent_folder", request.form.get("sent_folder", "").strip())
                Setting.set("email_save_sent", "1" if save_sent else "0")
                if save_sent and not imap_host(host):
                    flash("Set an IMAP host — there's no SMTP hostname to derive "
                          "one from, so the Sent copy has nowhere to go.", "danger")
                    return redirect(url_for("admin_settings"))

                cfg = smtp_config()
                if request.form.get("test") and cfg:
                    to = admin_recipients() or [current_user.email]
                    if not any(to):
                        flash("Saved, but there's nobody to send a test to — add an "
                              "email to your profile or to the recipient list.", "warning")
                        return redirect(url_for("admin_settings"))
                    sent, detail = mailer.send(
                        cfg, to, *mailer.test_email(Setting.get("lab_name", "the lab")),
                        blocking=True)
                    if not sent:
                        flash(f"Could not send the test: {detail}", "danger")
                    elif detail.startswith("sent, but"):
                        # Delivered, but not filed — worth saying out loud here,
                        # since a silent Sent folder is exactly what this setting
                        # is meant to fix.
                        flash(f"Test email sent to {', '.join(to)}, but the copy to "
                              f"the Sent folder failed: {detail.split('(', 1)[-1].rstrip(')')}",
                              "warning")
                    else:
                        flash(f"Test email sent to {', '.join(to)}"
                              + (f" and copied to {detail.split('copied to ', 1)[-1]}."
                                 if "copied to" in detail else "."), "success")
                else:
                    flash("Email settings saved." if cfg
                          else "Email notifications turned off.", "success")

            return redirect(url_for("admin_settings"))

        lat, lng, radius = lab_geofence()
        rate, accrual_start = leave_policy()
        cs, ce = leave_cycle()
        win = window_state()
        return render_template("admin/settings.html", lab_lat=lat, lab_lng=lng,
                               lab_radius=radius, leave_rate=rate,
                               geofence_mode=geofence_mode(),
                               menstrual_leave_rate=menstrual_rate(),
                               accrual_start=accrual_start, cycle_start=cs,
                               cycle_end=ce, window=win,
                               cycle_total=round(rate * _months_between(cs, ce), 2),
                               device_limit=Config.MAX_DEVICES_PER_USER,
                               discord_url=discord_webhook(),
                               discord_attendance=discord_enabled("attendance"),
                               discord_flags=discord_enabled("flags"),
                               smtp_host=Setting.get("smtp_host", "") or "",
                               smtp_port=Setting.get("smtp_port", Config.SMTP_PORT),
                               smtp_security=(Setting.get("smtp_security")
                                              or Config.SMTP_SECURITY),
                               smtp_username=Setting.get("smtp_username", "") or "",
                               smtp_has_password=bool(Setting.get("smtp_password")
                                                      or Config.SMTP_PASSWORD),
                               smtp_from=Setting.get("smtp_from", "") or "",
                               smtp_from_name=(Setting.get("smtp_from_name")
                                               or Config.SMTP_FROM_NAME),
                               email_leave_request=email_enabled("leave_request"),
                               email_leave_decision=email_enabled("leave_decision"),
                               email_admin_recipients=(
                                   Setting.get("email_admin_recipients", "") or ""),
                               email_admin_fallback=admin_recipients(),
                               email_save_sent=Setting.get("email_save_sent", "0") == "1",
                               imap_host=Setting.get("imap_host", "") or "",
                               imap_host_derived=imap_host(
                                   Setting.get("smtp_host") or Config.SMTP_HOST or ""),
                               imap_port=Setting.get("imap_port", Config.IMAP_PORT),
                               imap_security=(Setting.get("imap_security")
                                              or Config.IMAP_SECURITY),
                               imap_username=Setting.get("imap_username", "") or "",
                               imap_has_password=bool(Setting.get("imap_password")
                                                      or Config.IMAP_PASSWORD),
                               sent_folder=Setting.get("sent_folder", "") or "",
                               lab_networks_raw=Setting.get("lab_networks", "") or "",
                               network_mode=network_mode(),
                               my_ip=client_ip(),
                               trust_proxy=Config.TRUST_PROXY)

    # ---------------- DB editor (/dbmod) ---------------- #
    # Raw access to every table, outside the Admin UI on purpose — nothing here
    # goes through the business rules (LEAVE_STATUSES, counts_present, quota),
    # so it's a repair tool, not a workflow. Guarded by its own admin login.
    @app.route("/dbmod", methods=["GET", "POST"])
    def dbmod():
        user = dbmod_user()
        if user is None:
            if request.method == "POST":
                dbmod_check_csrf()
                lab_id = request.form.get("lab_id", "").strip()
                cand = User.query.filter(
                    db.func.lower(User.lab_id) == lab_id.lower()).first()
                if (cand and cand.is_admin and cand.is_active_member
                        and cand.check_password(request.form.get("password", ""))):
                    session[DBMOD_SESSION_KEY] = cand.id
                    app.logger.warning("dbmod: login by %s from %s",
                                       cand.lab_id, client_ip())
                    return redirect(url_for("dbmod"))
                app.logger.warning("dbmod: failed login for %r from %s",
                                   lab_id, client_ip())
                flash("Invalid admin credentials.", "danger")
                return redirect(url_for("dbmod"))
            return render_template("dbmod/login.html", csrf=dbmod_csrf())

        tables = [(m.__tablename__, db.session.query(m).count())
                  for m in DBMOD_MODELS]
        return render_template("dbmod/index.html", tables=tables,
                               dbmod_admin=user, csrf=dbmod_csrf())

    @app.route("/dbmod/logout", methods=["POST"])
    @dbmod_required
    def dbmod_logout():
        session.pop(DBMOD_SESSION_KEY, None)
        flash("Signed out of the DB editor.", "info")
        return redirect(url_for("dbmod"))

    @app.route("/dbmod/<table>")
    @dbmod_required
    def dbmod_table(table):
        model = dbmod_model(table)
        cols = list(model.__table__.columns)
        pk = dbmod_pk(model)
        query = db.session.query(model)

        # Global search box (any column may match) + one filter per column
        # (all must match). Every value goes through dbmod_match, which knows
        # the column's type.
        q = request.args.get("q", "").strip()
        if q:
            conds = [m for m in (dbmod_match(c, q) for c in cols) if m is not None]
            query = query.filter(db.or_(*conds)) if conds else query.filter(db.false())
        filters = {}
        for c in cols:
            val = request.args.get(f"f_{c.name}", "").strip()
            if val:
                filters[c.name] = val
                cond = dbmod_match(c, val)
                query = query.filter(cond if cond is not None else db.false())

        sort = request.args.get("sort", pk.name)
        sort_col = model.__table__.columns.get(sort, pk)
        desc = request.args.get("dir", "desc") == "desc"
        query = query.order_by(sort_col.desc() if desc else sort_col.asc())

        # Page size sticks for the session, so it doesn't reset on every click.
        per_page = request.args.get("per_page", type=int)
        if per_page in DBMOD_PER_PAGE:
            session["dbmod_per_page"] = per_page
        per_page = session.get("dbmod_per_page", 100)

        total = query.count()
        pages = max(1, -(-total // per_page))
        page = min(max(request.args.get("page", 1, type=int), 1), pages)
        rows = query.offset((page - 1) * per_page).limit(per_page).all()

        # Show "id · lab_id" for anything pointing at users, so user_id /
        # owner_id / reviewed_by columns are readable without a lookup.
        user_cols = [c.name for c in cols if dbmod_points_at_users(c)]
        ids = {getattr(r, c) for r in rows for c in user_cols} - {None}
        lab_ids = dict(db.session.query(User.id, User.lab_id)
                       .filter(User.id.in_(ids)).all()) if ids else {}

        # Everything that defines the current view, for building links that
        # change one thing (page, sort) and keep the rest.
        view_args = {"q": q or None, "sort": sort_col.name,
                     "dir": "desc" if desc else "asc",
                     **{f"f_{k}": v for k, v in filters.items()}}
        # Page links: first, last, and a window around the current page.
        shown = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
        page_links = []
        for n in shown:
            if page_links and n - page_links[-1] > 1:
                page_links.append(None)  # gap → "…"
            page_links.append(n)

        return render_template("dbmod/table.html", table=table, cols=cols,
                               pk=pk, rows=rows, total=total, page=page,
                               pages=pages, q=q, sort=sort_col.name, desc=desc,
                               filters=filters, view_args=view_args,
                               per_page=per_page, per_page_options=DBMOD_PER_PAGE,
                               page_links=page_links,
                               first_row=(page - 1) * per_page + 1 if rows else 0,
                               user_cols=user_cols, lab_ids=lab_ids,
                               tables=[m.__tablename__ for m in DBMOD_MODELS],
                               dbmod_admin=dbmod_user(), csrf=dbmod_csrf())

    @app.route("/dbmod/<table>/new", methods=["GET", "POST"])
    @dbmod_required
    def dbmod_new(table):
        model = dbmod_model(table)
        return dbmod_form(model, model(), creating=True)

    @app.route("/dbmod/<table>/<pk>/edit", methods=["GET", "POST"])
    @dbmod_required
    def dbmod_edit(table, pk):
        model = dbmod_model(table)
        return dbmod_form(model, dbmod_get(model, pk), creating=False)

    @app.route("/dbmod/<table>/<pk>/delete", methods=["POST"])
    @dbmod_required
    def dbmod_delete(table, pk):
        model = dbmod_model(table)
        row = dbmod_get(model, pk)
        try:
            # ORM delete, so User's cascades (attendance, leaves, devices) run.
            db.session.delete(row)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Could not delete: {getattr(e, 'orig', None) or e}", "danger")
            return redirect(url_for("dbmod_edit", table=table, pk=pk))
        app.logger.warning("dbmod: %s deleted %s#%s", dbmod_user().lab_id,
                           table, pk)
        flash(f"{table} #{pk} deleted.", "success")
        return redirect(url_for("dbmod_table", table=table))

    # ---------------- Errors ---------------- #
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403,
                               msg="You don't have access to this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404,
                               msg="Page not found."), 404


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))

    # Browsers only hand out GPS on a secure origin, and http://<LAN IP> isn't
    # one — so the dev server needs TLS to test check-in from a phone.
    # SSL_CERT/SSL_KEY use a real (self-signed) pair, SSL=1 falls back to
    # Werkzeug's throwaway cert. Production terminates TLS at nginx instead.
    cert, key = os.environ.get("SSL_CERT"), os.environ.get("SSL_KEY")
    if cert and key:
        ssl_context = (cert, key)
    elif os.environ.get("SSL") == "1":
        ssl_context = "adhoc"
    else:
        ssl_context = None
    app.run(host="0.0.0.0", port=port, debug=True, ssl_context=ssl_context)
