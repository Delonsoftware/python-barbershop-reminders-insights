#!/usr/bin/env python3
"""
Generate a synthetic barbershop appointment history (fully made up: no real people).

Simulates about six months of visits for ~200 clients across three barbers, ending today,
plus bookings for the coming week. Each client has their own rhythm (some every two weeks,
some every six), a favourite barber and service, and a personal no-show habit. A few
clients stop coming (so there are win-back cases), some opted out of messages, and a few
rows are deliberately flawed to show input validation.

Usage:
    python generate_sample_appointments.py                 # -> data/appointments.csv
    python generate_sample_appointments.py --clients 300 --weeks 40 --seed 3
    python generate_sample_appointments.py --today 2026-10-11
"""

import argparse
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

FIELDS = ["appointment_id", "client_id", "client_name", "phone", "email", "reminder_opt_in",
          "barber", "service", "price", "appointment_datetime", "status"]

BARBERS = ["Marcus", "Jay", "Dre"]
SERVICES = {                      # name: (price, popularity weight)
    "Haircut": (35, 30), "Fade": (40, 28), "Beard Trim": (20, 10), "Haircut + Beard": (50, 16),
    "Kids Cut": (25, 6), "Line Up": (15, 6), "Hot Towel Shave": (30, 4),
}
OPEN_DAYS = [1, 2, 3, 4, 5]       # Tue .. Sat (Monday = 0)
FIRST = ["James", "Mohamed", "Andre", "Kwame", "Tyrone", "Luis", "Daniel", "Omar", "Marcus", "Ethan",
         "Noah", "Samuel", "Victor", "Ibrahim", "Kevin", "Jamal", "Chen", "Ryan", "Lucas", "Isaiah",
         "Devon", "Malik", "Carlos", "Tariq", "Jordan", "Aiden", "Elijah", "Hassan", "Trevor", "Nate"]
LAST = ["Campbell", "Nguyen", "Patel", "Brown", "Singh", "Osei", "Thompson", "Khan", "Martin", "Taylor",
        "Ahmed", "Wilson", "Clarke", "Lopez", "Robinson", "Mensah", "Green", "Anderson", "Scott", "Baker",
        "Adams", "Hill", "Wright", "Evans", "Turner", "Lee", "Mitchell", "Roberts", "Phillips", "Bailey"]
# hour weights from 9am to 6pm start times
WEEKDAY_HOURS = [1, 1, 2, 2, 2, 3, 5, 6, 5, 3]
SATURDAY_HOURS = [3, 6, 7, 7, 6, 5, 4, 3, 2, 1]
STATUS_LABELS = {"completed": ["Completed", "Completed", "Completed", "Complete"],
                 "no_show": ["No-Show", "No Show", "no-show"],
                 "cancelled": ["Cancelled", "Canceled"], "booked": ["Booked", "Scheduled", "Confirmed"]}


def next_open_day(day: date) -> date:
    while day.weekday() not in OPEN_DAYS:
        day += timedelta(days=1)
    return day


def snap_to_open_day(rng, day: date) -> date:
    """If the shop is closed that day, move to the closest open day before or after (coin flip)."""
    if day.weekday() in OPEN_DAYS:
        return day
    earlier = day
    while earlier.weekday() not in OPEN_DAYS:
        earlier -= timedelta(days=1)
    return earlier if rng.random() < 0.5 else next_open_day(day)


def pick_start(rng, day: date, taken: set, barber: str):
    weights = SATURDAY_HOURS if day.weekday() == 5 else WEEKDAY_HOURS
    for _ in range(12):
        hour = rng.choices(range(9, 19), weights=weights)[0]
        start = datetime(day.year, day.month, day.day, hour, rng.choice([0, 30]))
        if (barber, start) not in taken:
            taken.add((barber, start))
            return start
    return None


def fmt_phone(rng, area, line):
    return rng.choice([f"{area}-555-{line}", f"({area}) 555-{line}", f"{area}.555.{line}",
                       f"{area}555{line}", f"+1 {area} 555 {line}"])


def generate(clients: int = 200, weeks: int = 26, seed: int = 4, today: date = None) -> list:
    rng = random.Random(seed)
    today = today or date.today()
    history_start = today - timedelta(weeks=weeks)
    booking_horizon = today + timedelta(days=7)
    taken, rows, used_names = set(), [], set()

    for number in range(1, clients + 1):
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used_names:
                used_names.add(name)
                break
        client_id = f"C{number:04d}"
        phone = fmt_phone(rng, rng.choice(["416", "647", "437"]), f"01{rng.randint(0, 99):02d}")
        email = f"{name.lower().replace(' ', '.')}@example.com" if rng.random() < 0.8 else ""
        if rng.random() < 0.15:
            phone = ""
        opt_in = rng.choices(["yes", "no", ""], weights=[90, 6, 4])[0]
        barber = rng.choice(BARBERS)
        service = rng.choices(list(SERVICES), weights=[w for _, w in SERVICES.values()])[0]
        interval = rng.randint(14, 42)
        no_show_rate = rng.choices([0.03, 0.12, 0.30], weights=[80, 15, 5])[0]
        churn_day = (history_start + timedelta(days=rng.randint(30, weeks * 7))
                     if rng.random() < 0.12 else None)

        # half the clients are long-standing; the rest first appear at some point in the history
        first_gap = rng.randint(0, interval) if rng.random() < 0.5 else rng.randint(0, max(interval, weeks * 7 - 14))
        visit = snap_to_open_day(rng, history_start + timedelta(days=first_gap))
        while visit <= booking_horizon:
            if churn_day and visit > churn_day:
                break
            who = barber if rng.random() < 0.8 else rng.choice(BARBERS)
            what = service if rng.random() < 0.8 else rng.choices(
                list(SERVICES), weights=[w for _, w in SERVICES.values()])[0]
            start = pick_start(rng, visit, taken, who)
            if start is not None:
                if visit >= today:
                    status = "booked" if rng.random() < 0.75 else None   # not everyone books ahead
                else:
                    roll = rng.random()
                    status = ("cancelled" if roll < 0.06 else "no_show" if roll < 0.06 + no_show_rate
                              else "completed")
                if status:
                    rows.append({
                        "client_id": client_id, "client_name": name, "phone": phone, "email": email,
                        "reminder_opt_in": opt_in, "barber": who, "service": what,
                        "price": SERVICES[what][0],
                        "appointment_datetime": start.strftime("%Y-%m-%d %H:%M"),
                        "status": rng.choice(STATUS_LABELS[status]),
                    })
            visit = snap_to_open_day(rng, visit + timedelta(days=interval + rng.randint(-3, 4)))

    rows.sort(key=lambda r: r["appointment_datetime"])
    for i, row in enumerate(rows, start=1):
        row["appointment_id"] = f"A{i:05d}"

    # deliberate problems, to demonstrate validation
    flawed = rng.sample(range(len(rows)), k=5)
    rows[flawed[0]]["appointment_datetime"] = "not a date"
    rows[flawed[1]]["price"] = "-20"
    rows[flawed[2]]["status"] = "Rescheduled?"
    rows[flawed[3]]["client_id"] = ""
    rows[flawed[4]]["appointment_id"] = rows[flawed[4] - 1]["appointment_id"]   # duplicate id
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a sample barbershop appointment history")
    parser.add_argument("--clients", type=int, default=200)
    parser.add_argument("--weeks", type=int, default=26, help="weeks of history (default 26)")
    parser.add_argument("--seed", type=int, default=4)
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="treat this date as today (default: the real date)")
    parser.add_argument("--output", type=Path, default=Path("data/appointments.csv"))
    args = parser.parse_args()

    rows = generate(args.clients, args.weeks, args.seed, args.today)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows):,} appointments to {args.output}")


if __name__ == "__main__":
    main()
