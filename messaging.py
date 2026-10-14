"""
Message drafting: next-day appointment reminders and rebooking nudges.

Nothing is sent. The tool drafts messages (and CSVs you can import into an SMS/email tool);
a person decides what goes out. Only clients who haven't opted out are included, and every
SMS ends with an opt-out line.

Reminders
---------
* One message per booked appointment on the target date.
* Clients with repeated recent no-shows get a firmer "please confirm" variant.

Rebooking
---------
* For each client, find their usual gap between visits (median of recent gaps).
* If they are well past it and haven't rebooked, draft a nudge. Tone depends on how late:
    Due      1.15x to 1.6x their usual gap     friendly nudge
    Overdue  1.6x to 3x                        "you're usually in every N weeks"
    Lapsed   3x or more, or 120+ days          win-back message
* Skips clients who already have a booking, opted out, have no phone/email, were messaged
  recently (contact log), or haven't visited in so long they're likely gone.
"""

import csv
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

TIER_ORDER = {"Due": 0, "Overdue": 1, "Lapsed": 2}
SAMPLE_VALUES = {"first_name": "Sam", "service": "Haircut", "barber": "Alex", "when": "tomorrow",
                 "time": "4:30 PM", "shop_name": "Shop", "shop_phone": "555", "booking_link": "x.example",
                 "weeks_since": 5, "interval_weeks": 3}


def render(template: str, **values) -> str:
    return template.format(**values)


def validate_templates(config: dict) -> None:
    """Fail early with a clear message if a template uses an unknown {placeholder}."""
    for name, template in config["templates"].items():
        try:
            render(template, **SAMPLE_VALUES)
        except (KeyError, IndexError, ValueError) as exc:
            raise SystemExit(f"Template '{name}' in config.json has a problem: {exc}")


def sms_segments(text: str, per_segment: int = 160) -> int:
    n = len(text)
    return 1 if n <= per_segment else math.ceil(n / (per_segment - 7))


def pick_channel(phone, email, opt_in: bool):
    """(channel, destination) or (None, reason it can't be messaged)."""
    if not opt_in:
        return None, "Opted out"
    if phone:
        return "SMS", phone
    if email:
        return "Email", email
    return None, "No phone or email"


def text_or_none(value):
    """A real, non-blank string, or None. (Missing pandas values are NaN, which is truthy.)"""
    return value if isinstance(value, str) and value.strip() else None


def clock(ts: pd.Timestamp) -> str:
    return ts.strftime("%I:%M %p").lstrip("0")


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------
def find_default_target(df: pd.DataFrame, today: date):
    """The next day after today that has booked appointments (normally tomorrow)."""
    upcoming = df[(df["status"] == "booked") & (df["day"] > pd.Timestamp(today))]
    return None if upcoming.empty else upcoming["day"].min().date()


def no_show_counts(df: pd.DataFrame, today: date, cfg: dict) -> dict:
    """Recent no-shows per client, looking at their last N attended-or-missed visits."""
    window = cfg["reminders"]["high_risk_window_visits"]
    past = df[df["status"].isin(["completed", "no_show"]) & (df["day"] < pd.Timestamp(today))]
    counts = {}
    for client, group in past.groupby("client_id"):
        recent = group.sort_values("start").tail(window)
        counts[client] = int((recent["status"] == "no_show").sum())
    return counts


def contact_info(df: pd.DataFrame) -> dict:
    """Latest phone/email per client; opted out if ANY record says so (the safe choice)."""
    info = {}
    for client, group in df.sort_values("start").groupby("client_id"):
        info[client] = {
            "phone": next((p for p in reversed(group["phone"].tolist()) if text_or_none(p)), None),
            "email": next((e for e in reversed(group["email"].tolist()) if text_or_none(e)), None),
            "opt_in": bool(group["opt_in"].all()),
        }
    return info


def build_reminders(df: pd.DataFrame, target: date, today: date, config: dict):
    """Returns (messages, skipped) for booked appointments on the target date."""
    templates, shop = config["templates"], config["shop"]
    per_segment = config["reminders"]["sms_chars_per_segment"]
    threshold = config["reminders"]["high_risk_no_shows"]
    risk_counts, contacts = no_show_counts(df, today, config), contact_info(df)

    if target == today + timedelta(days=1):
        when = "tomorrow"
    elif target == today:
        when = "today"
    else:
        when = f"on {pd.Timestamp(target):%A, %b %d}"

    day = df[(df["status"] == "booked") & (df["day"] == pd.Timestamp(target))].sort_values("start")
    messages, skipped = [], []
    for appt in day.itertuples(index=False):
        contact = contacts[appt.client_id]
        channel, destination = pick_channel(contact["phone"], contact["email"], contact["opt_in"])
        if channel is None:
            skipped.append({"client_name": appt.client_name, "start": appt.start, "reason": destination})
            continue
        high_risk = risk_counts.get(appt.client_id, 0) >= threshold
        template = templates["reminder_high_risk" if high_risk else "reminder"]
        text = render(template, first_name=appt.first_name, service=appt.service,
                      barber=text_or_none(appt.barber) or "your barber", when=when, time=clock(appt.start),
                      shop_name=shop.get("short_name", shop["name"]), shop_phone=shop["phone"], booking_link=shop["booking_link"])
        if channel == "SMS":
            text += templates["opt_out_text"]
        messages.append({
            "appointment_id": appt.appointment_id, "client_id": appt.client_id,
            "client_name": appt.client_name, "start": appt.start, "service": appt.service,
            "barber": text_or_none(appt.barber) or "", "channel": channel, "to": destination,
            "risk": "High" if high_risk else "Normal",
            "no_shows_recent": risk_counts.get(appt.client_id, 0),
            "message": text, "chars": len(text),
            "segments": sms_segments(text, per_segment) if channel == "SMS" else 1,
        })
    return messages, skipped


# --------------------------------------------------------------------------
# Rebooking
# --------------------------------------------------------------------------
def usual_interval(days_between: list, cfg: dict) -> int:
    """Median of the client's recent gaps between visits, kept within sane limits."""
    reb = cfg["rebooking"]
    if not days_between:
        return reb["default_interval_days"]
    median = float(pd.Series(days_between[-5:]).median())
    return int(round(min(max(median, reb["min_interval_days"]), reb["max_interval_days"])))


def client_profiles(df: pd.DataFrame, today: date, config: dict) -> list:
    contacts = contact_info(df)
    today_ts = pd.Timestamp(today)
    profiles = []
    for client, group in df.groupby("client_id"):
        done = group[(group["status"] == "completed") & (group["day"] <= today_ts)].sort_values("start")
        if done.empty:
            continue
        gaps = done["day"].diff().dropna().dt.days.tolist()
        barbers = done["barber"].dropna()
        profiles.append({
            "client_id": client, "client_name": done.iloc[-1]["client_name"],
            "first_name": done.iloc[-1]["first_name"],
            "visits": len(done), "last_visit": done.iloc[-1]["day"].date(),
            "interval_days": usual_interval(gaps, config),
            "usual_service": done["service"].mode().iloc[0],
            "usual_barber": barbers.mode().iloc[0] if not barbers.empty else "your barber",
            "avg_spend": round(float(done["price"].mean()), 2),
            "has_upcoming": bool(((group["status"] == "booked") & (group["day"] >= today_ts)).any()),
            **contacts[client],
        })
    return profiles


def load_contact_log(path: Path) -> dict:
    """client_id -> date of the most recent rebooking message that was recorded."""
    log = {}
    if path and path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                when = date.fromisoformat(row["date"])
                log[row["client_id"]] = max(when, log.get(row["client_id"], when))
    return log


def record_contacts(path: Path, messages: list, today: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["client_id", "date", "type"])
        for m in messages:
            writer.writerow([m["client_id"], today.isoformat(), "rebooking"])


def build_rebooking(df: pd.DataFrame, today: date, config: dict, contact_log: dict = None):
    """Returns (messages, skip_counts) for clients who look due for a visit."""
    reb, shop, templates = config["rebooking"], config["shop"], config["templates"]
    per_segment = config["reminders"]["sms_chars_per_segment"]
    contact_log = contact_log or {}
    skips = {}

    def skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    candidates = []
    for p in client_profiles(df, today, config):
        days_since = (today - p["last_visit"]).days
        ratio = days_since / p["interval_days"]
        if p["has_upcoming"]:
            skip("Already has a booking")
        elif not p["opt_in"]:
            skip("Opted out")
        elif not (p["phone"] or p["email"]):
            skip("No phone or email")
        elif days_since > reb["give_up_days"]:
            skip(f"Gone too long (over {reb['give_up_days']} days)")
        elif ratio < reb["due_ratio"]:
            skip("Not due yet")
        elif p["client_id"] in contact_log and (today - contact_log[p["client_id"]]).days < reb["cooldown_days"]:
            skip(f"Messaged in the last {reb['cooldown_days']} days")
        else:
            if ratio >= reb["lapsed_ratio"] or days_since >= reb["lapse_days"]:
                tier = "Lapsed"
            elif ratio >= reb["overdue_ratio"]:
                tier = "Overdue"
            else:
                tier = "Due"
            candidates.append({**p, "tier": tier, "days_since": days_since, "ratio": round(ratio, 2)})

    candidates.sort(key=lambda c: (TIER_ORDER[c["tier"]], -c["avg_spend"], c["client_id"]))
    limit = reb["max_messages_per_run"]
    if len(candidates) > limit:
        skips["Over the per-run limit (will appear next run)"] = len(candidates) - limit
        candidates = candidates[:limit]

    messages = []
    for c in candidates:
        channel, destination = pick_channel(c["phone"], c["email"], True)
        text = render(templates[f"rebook_{c['tier'].lower()}"], first_name=c["first_name"],
                      service=c["usual_service"], barber=c["usual_barber"],
                      weeks_since=max(1, round(c["days_since"] / 7)),
                      interval_weeks=max(1, round(c["interval_days"] / 7)),
                      shop_name=shop.get("short_name", shop["name"]), shop_phone=shop["phone"], booking_link=shop["booking_link"])
        if channel == "SMS":
            text += templates["opt_out_text"]
        messages.append({
            "tier": c["tier"], "client_id": c["client_id"], "client_name": c["client_name"],
            "channel": channel, "to": destination, "last_visit": c["last_visit"],
            "days_since": c["days_since"], "usual_interval_days": c["interval_days"],
            "times_vs_usual": c["ratio"], "visits": c["visits"], "usual_barber": c["usual_barber"],
            "usual_service": c["usual_service"], "avg_spend": c["avg_spend"],
            "message": text, "chars": len(text),
            "segments": sms_segments(text, per_segment) if channel == "SMS" else 1,
        })
    return messages, skips
