import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "atten.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Uploads
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB

    # Default geofence (placeholder — admin overrides in dashboard).
    # Set the real lab location from Admin -> Settings -> Geofence on first run.
    DEFAULT_LAB_LAT = 0.0
    DEFAULT_LAB_LNG = 0.0
    DEFAULT_LAB_RADIUS_M = 150  # meters
    # off / warn / enforce — how hard the GPS check bites. Same three modes as
    # the network check, so the admin can stand it down for a day (broken GPS,
    # no HTTPS on a test host) without editing code.
    DEFAULT_GEOFENCE_MODE = "enforce"

    # Default admin bootstrap credentials (change after first login)
    DEFAULT_ADMIN_ID = "labadmin"
    DEFAULT_ADMIN_PASSWORD = "admin@123"

    # Leave policy — days accrue every month and carry over within the cycle.
    # The launch year runs from DEFAULT_LEAVE_START to 31 Dec; every year after
    # that is a full Jan–Dec cycle (2.5 × 12 = 30 days).
    DEFAULT_LEAVE_RATE = 2.5          # days earned per month
    DEFAULT_LEAVE_START = "2026-07-01"

    # Menstrual leave — an extra monthly grant for female members only, on top
    # of DEFAULT_LEAVE_RATE, spendable *only* on leave of type "menstrual".
    DEFAULT_MENSTRUAL_LEAVE_RATE = 1.0

    # Attendance window (local time). Check-in closes in the evening so nobody
    # can back-fill a day they never showed up for; check-out stays open later.
    DEFAULT_CHECKIN_CUTOFF = "17:00"
    DEFAULT_CHECKOUT_CUTOFF = "23:59"

    # Discord notifications. The webhook URL is a secret: it lives in the
    # settings table (instance/ is gitignored) or this env var — never in source.
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

    # Email notifications for leave requests/decisions. Like the Discord webhook,
    # the password is a secret: it lives in the settings table (instance/ is
    # gitignored) or these env vars — never in source. Env is the fallback when
    # the admin hasn't filled the form in.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls")  # starttls/ssl/none
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")          # envelope + From address
    SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Atten")

    # Sent-folder copy over IMAP. SMTP delivers but files nothing, so without
    # this the sending account's Sent folder never shows what Atten sent.
    # Host blank = same provider as SMTP (smtp.x → imap.x); credentials blank =
    # the SMTP ones; folder blank = ask the server (RFC 6154 \Sent).
    IMAP_HOST = os.environ.get("IMAP_HOST", "")
    IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
    IMAP_SECURITY = os.environ.get("IMAP_SECURITY", "ssl")   # ssl/starttls/none
    IMAP_USERNAME = os.environ.get("IMAP_USERNAME", "")
    IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
    SENT_FOLDER = os.environ.get("SENT_FOLDER", "")

    # Set to 1 only when running behind a reverse proxy you control (nginx,
    # Caddy, Cloudflare). It makes the app trust X-Forwarded-For for the client
    # IP — which anyone can forge if there is no proxy actually stripping it.
    TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

    # Anti-proxy: how many devices one account may check in from.
    MAX_DEVICES_PER_USER = 3
    DEVICE_COOKIE = "atten_device"
    DEVICE_COOKIE_DAYS = 730
