"""Email notifications for leave requests.

Sends over SMTP in a background thread, for the same reason notify.py does: a
slow or misconfigured mail server must never delay — or fail — a leave request.
Every error is swallowed and logged. The DB is the source of truth, the mail is
best-effort.

SMTP only hands a message to the server — it does not file a copy anywhere, so
mail sent this way is invisible in the account's own Sent folder (Gmail is the
exception; it files SMTP-sent mail itself). When `save_sent` is on we append the
message over IMAP after delivery, which is how every mail client does it.

Stdlib only (smtplib + imaplib + email.message), so there is no new dependency
to install.
"""
import imaplib
import logging
import re
import smtplib
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from html import escape

log = logging.getLogger(__name__)

TIMEOUT = 12                     # seconds — SMTP handshakes are slower than HTTP

STATUS_WORDS = {
    "approved": "approved",
    "rejected": "rejected",
    "pending": "pending",
}
STATUS_COLORS = {
    "approved": "#16A34A",
    "rejected": "#DC2626",
    "pending": "#D97706",
}


def _connect(cfg):
    """Open an SMTP session per cfg['security']: 'ssl' | 'starttls' | 'none'."""
    host, port = cfg["host"], cfg["port"]
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        if cfg["security"] == "starttls":
            server.starttls()
            server.ehlo()
    if cfg.get("username"):
        server.login(cfg["username"], cfg.get("password") or "")
    return server


def _build(cfg, to, subject, text, html=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("from_name") or "Atten", cfg["from_addr"]))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _imap_connect(cfg):
    cls = imaplib.IMAP4_SSL if cfg.get("imap_security") == "ssl" else imaplib.IMAP4
    host, port = cfg["imap_host"], cfg["imap_port"]
    try:                                        # timeout= only exists from 3.9
        server = cls(host, port, timeout=TIMEOUT)
    except TypeError:
        server = cls(host, port)
    if cfg.get("imap_security") == "starttls":
        server.starttls()
    if cfg.get("imap_username"):
        server.login(cfg["imap_username"], cfg.get("imap_password") or "")
    return server


def _resolve_sent_folder(server, configured):
    """The Sent folder is named differently everywhere — 'Sent', 'Sent Items',
    '[Gmail]/Sent Mail'. When the admin hasn't named one, ask the server: IMAP
    special-use (RFC 6154) flags the right mailbox with \\Sent."""
    if configured:
        return configured
    try:
        typ, data = server.list()
        if typ == "OK":
            for raw in data or []:
                line = (raw[0] if isinstance(raw, tuple) else raw)
                line = line.decode("utf-8", "replace")
                if "\\Sent" in line:
                    quoted = re.findall(r'"([^"]*)"', line)
                    if quoted:
                        return quoted[-1]       # trailing quoted token is the name
                    return line.rsplit(" ", 1)[-1]
    except Exception as e:
        log.debug("Could not list IMAP folders: %s", e)
    return "Sent"


def _save_to_sent(cfg, msg):
    """Append an already-delivered message to the account's Sent folder."""
    if not cfg.get("imap_host"):
        return False, "no IMAP server configured"
    server = None
    try:
        server = _imap_connect(cfg)
        folder = _resolve_sent_folder(server, cfg.get("sent_folder"))
        typ, data = server.append(f'"{folder}"', "(\\Seen)",
                                  imaplib.Time2Internaldate(time.time()),
                                  msg.as_bytes())
        if typ != "OK":
            detail = (data[0].decode("utf-8", "replace")
                      if data and data[0] else "server said NO")
            return False, f"{folder}: {detail}"
        return True, folder
    except imaplib.IMAP4.error as e:
        return False, f"IMAP error: {e}"
    except Exception as e:                      # DNS, TLS, timeout, bad port…
        return False, str(e)
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass


def _deliver(cfg, to, subject, text, html):
    msg = _build(cfg, to, subject, text, html)
    try:
        with _connect(cfg) as server:
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        log.warning("SMTP rejected the login: %s", e)
        return False, "the server rejected the username or password"
    except smtplib.SMTPException as e:
        log.warning("SMTP error sending '%s': %s", subject, e)
        return False, str(e)
    except Exception as e:                      # DNS, TLS, timeout, bad port…
        log.warning("Could not send '%s': %s", subject, e)
        return False, str(e)

    # The mail is delivered from here on. A failed Sent copy is a filing
    # problem, never a delivery one, so it downgrades the detail — it never
    # turns this into a failure the caller would report as "not sent".
    detail = "sent"
    if cfg.get("save_sent"):
        ok, why = _save_to_sent(cfg, msg)
        if ok:
            detail = f"sent, copied to {why}"
        else:
            detail = f"sent, but the Sent copy failed ({why})"
            log.warning("Could not file '%s' in Sent: %s", subject, why)
    log.info("Sent '%s' to %s (%s)", subject, ", ".join(to), detail)
    return True, detail


def send(cfg, to, subject, text, html=None, blocking=False):
    """Send one message. Returns (ok, detail) when blocking, else (True, 'queued')."""
    if not cfg or not cfg.get("host") or not cfg.get("from_addr"):
        return False, "no mail server configured"
    to = [a for a in (to or []) if a]
    if not to:
        return False, "no recipient address"
    if blocking:
        return _deliver(cfg, to, subject, text, html)
    threading.Thread(target=_deliver, args=(cfg, to, subject, text, html),
                     daemon=True).start()
    return True, "queued"


# --------------------------------------------------------------------------- #
# Message bodies
#
# Every builder returns (subject, text, html). Plain text is not a fallback
# nobody reads — it is what most mail clients show in the notification preview,
# so it carries the same facts as the HTML.
# --------------------------------------------------------------------------- #
def _dates(lr_start, lr_end, half):
    if half:
        return f"{lr_start:%d %b %Y} (half day)"
    if lr_start == lr_end:
        return f"{lr_start:%d %b %Y}"
    return f"{lr_start:%d %b %Y} → {lr_end:%d %b %Y}"


def _dates_subject(lr_start, lr_end, half):
    """Same span, ASCII only: a non-ASCII subject becomes a =?utf-8?…?= blob that
    clients truncate badly in previews and users can't filter on."""
    return _dates(lr_start, lr_end, half).replace("→", "to")


# def _shell(heading, accent, rows, note=None, link=None, link_text="Open Atten"):
#     """One house style for every mail: a coloured heading, a facts table, a note.

#     Everything interpolated here — names, reasons, admin comments — is member
#     input, so it is escaped: an unescaped '<' would silently eat the rest of the
#     message.
#     """
#     heading = escape(heading)
#     rows = [(escape(str(k)), escape(str(v)).replace("\n", "<br>"))
#             for k, v in rows if v]
#     note = escape(note) if note else None
#     link = escape(link, quote=True) if link else None
#     cells = "".join(
#         f'<tr><td style="padding:6px 16px 6px 0;color:#64748b;font-size:13px;'
#         f'white-space:nowrap;vertical-align:top">{k}</td>'
#         f'<td style="padding:6px 0;color:#0f172a;font-size:14px">{v}</td></tr>'
#         for k, v in rows)
#     body = (
#         f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
#         f'background:#f1f5f9;padding:24px">'
#         f'<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;'
#         f'overflow:hidden;border:1px solid #e2e8f0">'
#         f'<div style="height:4px;background:{accent}"></div>'
#         f'<div style="padding:24px">'
#         f'<h1 style="margin:0 0 16px;font-size:18px;color:#0f172a">{heading}</h1>'
#         f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse">{cells}</table>'
#     )
#     if note:
#         body += (f'<p style="margin:18px 0 0;padding:12px 14px;background:#f8fafc;'
#                  f'border-left:3px solid {accent};color:#334155;font-size:13px;'
#                  f'line-height:1.5">{note}</p>')
#     if link:
#         body += (f'<p style="margin:22px 0 0"><a href="{link}" '
#                  f'style="display:inline-block;background:#0f172a;color:#fff;'
#                  f'text-decoration:none;padding:10px 18px;border-radius:8px;'
#                  f'font-size:14px">{link_text}</a></p>')
#     body += ('</div></div>'
#              '<p style="max-width:520px;margin:14px auto 0;color:#94a3b8;font-size:12px;'
#              'text-align:center">Sent by Atten — you are receiving this because '
#              'you use the lab attendance system.</p></div>')
#     return body


def _text(heading, rows=None, note=None, link=None):
    lines = [heading, "=" * len(heading), ""]
    if rows:
        lines += [f"{k}: {v}" for k, v in rows if v]
    if note:
        lines += ["", note]
    if link:
        lines += ["", link]
    return "\n".join(lines).strip() + "\n\n\n" + "— Atten attendance"


def leave_request_email(user, lr, quota, lab_name, link=None):
    """To the admins: somebody just raised a request. quota may be None."""
    heading = f"New leave request — {user.name}"
    subject = f"[{lab_name}] Leave request: {user.name} - {_dates_subject(lr.start_date, lr.end_date, lr.is_half_day)}"
    rows = [
        ("Member", f"{user.name} · {user.lab_id}"),
        ("Designation", user.designation),
        ("Type", (lr.leave_type or "casual").title()),
        ("Dates", _dates(lr.start_date, lr.end_date, lr.is_half_day)),
        ("Days", _days(lr.days)),
        ("Reason", lr.reason or "—"),
    ]
    note = None
    if quota and quota.get("exceeds"):
        before = quota.get("before", 0)
        if before < 0:
            note = (f"{user.name} is already {_days(-before)} day(s) over quota "
                    f"this cycle, before this request.")
        else:
            note = (f"This request is {_days(quota['over_by'])} day(s) more than "
                    f"{user.name} has left ({_days(before)} day(s) before it).")
        note += " Going over never blocks a request — approving it is your call."
    return (subject,
            _text(heading, rows, note, link and f"Approve/Reject at: {link}"))


def leave_decision_email(user, lr, reviewer_name, lab_name, balance=None, link=None):
    """To the member: an admin approved or rejected their request."""
    word = STATUS_WORDS.get(lr.status, lr.status)
    heading = f"Your leave request was {word}"
    subject = f"[{lab_name}] Leave {word}: {_dates_subject(lr.start_date, lr.end_date, lr.is_half_day)}"
    rows = [
        ("Type", (lr.leave_type or "casual").title()),
        ("Dates", _dates(lr.start_date, lr.end_date, lr.is_half_day)),
        ("Days", _days(lr.days)),
        ("Decision", word.title()),
        ("Reviewed by", reviewer_name),
    ]
    if balance is not None:
        rows.append(("Balance left", f"{_days(balance)} day(s) this cycle"))
    note = f"Admin's note: {lr.admin_comment}" if lr.admin_comment else None
    return (subject,
            _text(heading, rows, note, link and f"View your leaves: {link}"),
            )


def leave_withdrawn_email(user, lr, lab_name, link=None):
    """To the admins: a pending request they may have seen is gone."""
    heading = f"Leave request withdrawn — {user.name}"
    subject = f"[{lab_name}] Leave withdrawn: {user.name} - {_dates_subject(lr.start_date, lr.end_date, lr.is_half_day)}"
    rows = [
        ("Member", f"{user.name} · {user.lab_id}"),
        ("Type", (lr.leave_type or "casual").title()),
        ("Dates", _dates(lr.start_date, lr.end_date, lr.is_half_day)),
        ("Days", _days(lr.days)),
    ]
    note = "The member withdrew this request before it was reviewed — nothing to do."
    return (subject,
            _text(heading, rows, note, link and f"Open Atten: {link}"),
            )


def test_email(lab_name):
    heading = "Atten email notifications are working!"
    # rows = [("Lab", lab_name), ("Status", "Connected")]
    note = ("Hello,\n\nFrom now on you will get an email whenever someone from the lab raises a leave "
            "request, and members will get one when you approve or reject their request.")
    return (f"[{lab_name}] Attendance Emails",
            _text(heading, None, note),
            )


def _days(n):
    """Mirror of app.fmt_days — kept local so this module imports nothing of the app."""
    n = round(float(n or 0), 2)
    return str(int(n)) if n == int(n) else f"{n:g}"
