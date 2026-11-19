"""Discord notifications for attendance events.

Posts to an incoming webhook in a background thread: a slow or broken Discord
must never delay a check-in or fail one. Every error is swallowed and logged —
attendance is the source of truth, the notification is best-effort.
"""
import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT = 6                      # seconds
COLORS = {
    "in": 0x16A34A,              # green
    "out": 0x0284C7,             # blue
    "flag": 0xDC2626,            # red
    "test": 0x4F46E5,            # indigo
}



def _post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "Atten/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        log.warning("Discord webhook rejected the message: %s %s", e.code, body)
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:                      # network down, DNS, timeout…
        log.warning("Discord webhook failed: %s", e)
        return False, str(e)


def send(url, payload, blocking=False):
    """Fire a webhook. Returns (ok, detail) when blocking, else (True, 'queued')."""
    if not url:
        return False, "no webhook configured"
    if blocking:
        return _post(url, payload)
    threading.Thread(target=_post, args=(url, payload), daemon=True).start()
    return True, "queued"


def _embed(title, description, color, fields=None):
    return {
        "username": "Atten",
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields or [],
        }],
    }


def attendance_payload(kind, user, when, extra=None):
    """kind is 'in' or 'out'. `when` must be a datetime, not a struct_time."""
    title = "✅ Checked in" if kind == "in" else "👋 Checked out"
    fields = [{"name": "Time", "value": when.strftime("%H:%M"), "inline": True}]
    for name, value in (extra or {}).items():
        fields.append({"name": name, "value": str(value), "inline": True})
    who = f"**{user.name}** · `{user.lab_id}`"
    if user.designation:
        who += f"\n{user.designation}"
    return _embed(title, who, COLORS[kind], fields)


FLAG_TITLES = {
    "shared_device": "🚨 Proxy attempt blocked",
    "device_limit": "⚠️ Device limit reached",
    "off_network": "📡 Attendance from outside the lab network",
}


def flag_payload(user, kind, detail, day):
    title = FLAG_TITLES.get(kind, "⚠️ Security flag")
    return _embed(title, detail, COLORS["flag"],
                  [{"name": "Member", "value": f"{user.name} · `{user.lab_id}`",
                    "inline": True},
                   {"name": "Date", "value": day.strftime("%d %b %Y"),
                    "inline": True}])


def test_payload(lab_name):
    return _embed("🔔 Atten is connected",
                  f"Attendance notifications for **{lab_name}** are working.\n"
                  "You'll see a message here whenever someone checks in or out.",
                  COLORS["test"])
