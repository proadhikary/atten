from datetime import datetime, date

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    lab_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    designation = db.Column(db.String(120))
    funding = db.Column(db.String(120))

    email = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    bio = db.Column(db.Text)
    photo = db.Column(db.String(200))  # filename in static/uploads

    # Drives the menstrual-leave entitlement only. Everyone starts "male"; the
    # admin flips the ones who should get it, so nobody has to declare anything
    # to sign up.
    gender = db.Column(db.String(10), default="male", nullable=False)

    password_hash = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active_member = db.Column(db.Boolean, default=True, nullable=False)
    # False for admin-only/staff accounts: they don't check in or raise leave,
    # so the Home + Leaves tabs are hidden for them.
    marks_attendance = db.Column(db.Boolean, default=True, nullable=False)
    # Last day in the lab for someone who resigned / finished / moved on. Set
    # by the member (Profile) or the admin; NULL means still in the lab. Unlike
    # is_active_member it doesn't block login — they keep read access to their
    # history, but drop off the roster and can't check in after this date.
    left_on = db.Column(db.Date)
    # When they joined the lab (the 1st of the joining month, as the lab's
    # people page only gives a month). Leave accrues from this month rather than
    # from the policy start, and days before it are never counted as absences.
    # NULL = unknown, treated as "has always been here".
    joined_on = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendances = db.relationship("Attendance", backref="user", lazy=True,
                                  cascade="all, delete-orphan")
    leaves = db.relationship("LeaveRequest", backref="user", lazy=True,
                             foreign_keys="LeaveRequest.user_id",
                             cascade="all, delete-orphan")
    devices = db.relationship("Device", backref="user", lazy=True,
                              cascade="all, delete-orphan")

    # --- password helpers ---
    @property
    def has_password(self):
        return bool(self.password_hash)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def has_joined(self, on=None):
        return self.joined_on is None or (on or date.today()) >= self.joined_on

    def in_lab_on(self, day):
        """Between joining and last day — the days attendance is expected."""
        return self.has_joined(day) and not self.has_left(day)

    def has_left(self, on=None):
        """True once `on` (default today) is past the member's last day."""
        return self.left_on is not None and (on or date.today()) > self.left_on

    @property
    def gets_menstrual_leave(self):
        """Only female members accrue (and may request) menstrual leave."""
        return (self.gender or "male").lower() == "female"

    @property
    def initials(self):
        parts = [p for p in (self.name or "").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class Attendance(db.Model):
    __tablename__ = "attendance"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    day = db.Column(db.Date, default=date.today, nullable=False, index=True)

    check_in = db.Column(db.DateTime)
    check_out = db.Column(db.DateTime)

    in_lat = db.Column(db.Float)
    in_lng = db.Column(db.Float)
    out_lat = db.Column(db.Float)
    out_lng = db.Column(db.Float)
    distance_m = db.Column(db.Float)  # distance from lab at check-in
    in_ip = db.Column(db.String(45))   # source address, kept for spoof forensics
    out_ip = db.Column(db.String(45))

    status = db.Column(db.String(20), default="present")  # present / on_leave

    # Set when a proxy attempt is detected: the row is kept for the audit trail
    # but never counts as attendance anywhere.
    is_voided = db.Column(db.Boolean, default=False, nullable=False)
    void_reason = db.Column(db.String(255))

    # Which device the check-in came from (anti-proxy trail).
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"))

    __table_args__ = (
        db.UniqueConstraint("user_id", "day", name="uq_user_day"),
    )

    @property
    def counts_present(self):
        """A real, countable day in the lab."""
        return bool(self.check_in) and not self.is_voided

    @property
    def worked_hours(self):
        if self.check_in and self.check_out:
            delta = self.check_out - self.check_in
            return round(delta.total_seconds() / 3600, 2)
        return None


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    leave_type = db.Column(db.String(40), default="casual")  # casual/sick/vacation/duty/other
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text)

    status = db.Column(db.String(20), default="pending")  # pending/approved/rejected
    admin_comment = db.Column(db.Text)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    reviewed_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Snapshot of the member's remaining quota the moment they submitted, so the
    # warning the admin sees is the same one the member saw.
    balance_at_request = db.Column(db.Float)

    # Half day always covers a single date, and costs 0.5 against the quota.
    # Defaults to False, so every request that already exists stays a full day.
    is_half_day = db.Column(db.Boolean, default=False, nullable=False)

    @property
    def days(self):
        if self.is_half_day:
            return 0.5
        return (self.end_date - self.start_date).days + 1


class Device(db.Model):
    """A browser an account is allowed to mark attendance from.

    Browsers cannot read a MAC address, so each device is identified by a random
    token in a long-lived cookie, backed up by a fingerprint of stable browser
    traits in case the cookie is cleared.
    """
    __tablename__ = "devices"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    fingerprint = db.Column(db.String(64), index=True)
    label = db.Column(db.String(120))          # e.g. "Chrome on macOS"
    user_agent = db.Column(db.String(300))

    is_revoked = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)


class SecurityFlag(db.Model):
    """Raised when an account marks attendance from someone else's device."""
    __tablename__ = "security_flags"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    user = db.relationship("User", foreign_keys=[user_id])

    # The account the device is registered to (null for a device-limit flag).
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    owner = db.relationship("User", foreign_keys=[owner_id])

    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"))
    device = db.relationship("Device")

    day = db.Column(db.Date, default=date.today, nullable=False, index=True)
    kind = db.Column(db.String(40), default="shared_device")  # shared_device / device_limit
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    resolved = db.Column(db.Boolean, default=False, nullable=False)
    resolution = db.Column(db.String(255))
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    resolver = db.relationship("User", foreign_keys=[resolved_by])
    resolved_at = db.Column(db.DateTime)


class Setting(db.Model):
    """Simple key/value store for geofence + app config."""
    __tablename__ = "settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255))

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return row.value if row else default

    @staticmethod
    def get_float(key, default=None):
        val = Setting.get(key)
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def set(key, value):
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=str(value))
            db.session.add(row)
        else:
            row.value = str(value)
        db.session.commit()
