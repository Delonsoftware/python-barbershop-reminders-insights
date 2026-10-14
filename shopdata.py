"""
Loading, cleaning and validating the appointments export.

Required columns: client_id, client_name, appointment_datetime, service, price, status
Optional columns: appointment_id, barber, phone, email, reminder_opt_in

Statuses are normalized to: completed, no_show, cancelled, booked.
A past appointment still marked "booked" becomes "unresolved" (it needs a status update)
and is excluded from revenue and no-show numbers.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REQUIRED_COLS = ["client_id", "client_name", "appointment_datetime", "service", "price", "status"]
OPTIONAL_COLS = ["appointment_id", "barber", "phone", "email", "reminder_opt_in"]

STATUS_MAP = {
    "completed": "completed", "complete": "completed", "done": "completed", "paid": "completed",
    "noshow": "no_show", "missed": "no_show",
    "cancelled": "cancelled", "canceled": "cancelled", "cancel": "cancelled",
    "booked": "booked", "scheduled": "booked", "confirmed": "booked",
    "upcoming": "booked", "pending": "booked",
}
YES = {"yes", "y", "true", "1", "optin", "optedin"}
NO = {"no", "n", "false", "0", "stop", "optout", "optedout"}
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Config file is not valid JSON ({path}): {exc}")


def clean_phone(value):
    """Any North American format -> (416) 555-0187, or None if it isn't a valid number."""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def clean_email(value):
    text = re.sub(r"\s+", "", str(value)).lower()
    return text if EMAIL_RE.match(text) and ".." not in text else None


def parse_opt_in(value, default: bool):
    """Returns (opted_in, recognized)."""
    key = re.sub(r"[^a-z0-9]", "", str(value).lower())
    if key == "":
        return default, True
    if key in YES:
        return True, True
    if key in NO:
        return False, True
    return False, False          # unrecognized text: be conservative


def load_appointments(path: Path, today: date, default_opt_in: bool = True):
    """Returns (clean appointments DataFrame, issues list)."""
    if not path.exists():
        sys.exit(f"Appointments file not found: {path}\n"
                 "Run 'python generate_sample_appointments.py' first to create sample data.")
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    raw.columns = [c.strip().lower().replace(" ", "_") for c in raw.columns]
    missing = [c for c in REQUIRED_COLS if c not in raw.columns]
    if missing:
        sys.exit(f"Appointments file is missing required columns: {', '.join(missing)}")
    for col in OPTIONAL_COLS:
        if col not in raw.columns:
            raw[col] = ""

    rows, issues, seen_ids = [], [], set()
    today_ts = pd.Timestamp(today)

    for idx, rec in enumerate(raw.to_dict("records")):
        row = idx + 2
        appt_id = rec["appointment_id"].strip() or f"row{row}"

        def issue(severity, text):
            issues.append({"row": row, "appointment_id": appt_id, "severity": severity, "issue": text})

        client_id, name = rec["client_id"].strip(), re.sub(r"\s+", " ", rec["client_name"]).strip()
        if not client_id or not name:
            issue("Rejected", "Missing client ID or client name")
            continue
        start = pd.to_datetime(rec["appointment_datetime"], errors="coerce", format="mixed")
        if pd.isna(start):
            issue("Rejected", f"Invalid date/time '{rec['appointment_datetime']}'")
            continue
        try:
            price = float(re.sub(r"[$,\s]", "", rec["price"]))
            if price < 0:
                raise ValueError
        except ValueError:
            issue("Rejected", f"Invalid price '{rec['price']}'")
            continue
        status = STATUS_MAP.get(re.sub(r"[^a-z]", "", rec["status"].lower()))
        if status is None:
            issue("Rejected", f"Unknown status '{rec['status']}'")
            continue
        if rec["appointment_id"].strip():
            if appt_id in seen_ids:
                issue("Rejected", "Duplicate appointment ID")
                continue
            seen_ids.add(appt_id)

        if status == "booked" and start.normalize() < today_ts:
            status = "unresolved"
            issue("Warning", "Past appointment is still marked Booked (update its status)")

        opt_in, recognized = parse_opt_in(rec["reminder_opt_in"], default_opt_in)
        if not recognized:
            issue("Warning", f"Unrecognized opt-in value '{rec['reminder_opt_in']}': treated as opted out")
        phone, email = clean_phone(rec["phone"]), clean_email(rec["email"])
        if rec["phone"].strip() and phone is None:
            issue("Warning", f"Invalid phone number '{rec['phone']}'")

        rows.append({
            "row": row, "appointment_id": appt_id, "client_id": client_id, "client_name": name,
            "first_name": name.split(" ")[0].title(), "phone": phone, "email": email,
            "opt_in": opt_in, "barber": rec["barber"].strip() or None,
            "service": re.sub(r"\s+", " ", rec["service"]).strip(), "price": price,
            "status": status, "start": start, "day": start.normalize(), "hour": start.hour,
        })

    df = pd.DataFrame(rows, columns=["row", "appointment_id", "client_id", "client_name", "first_name",
                                     "phone", "email", "opt_in", "barber", "service", "price",
                                     "status", "start", "day", "hour"])
    return df.sort_values("start").reset_index(drop=True), issues
