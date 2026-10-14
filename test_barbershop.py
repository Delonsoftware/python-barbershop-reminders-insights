"""
Tests for the barbershop reminder, rebooking and insights tool.

Run with either:
    python test_barbershop.py
    pytest -q          (if you have pytest installed)
"""

import csv
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
import pandas as pd

import barber_assistant as assistant
import generate_sample_appointments as gen
import insights
import messaging
import shopdata

CONFIG = shopdata.load_config(Path(__file__).parent / "config.json")
TODAY = date(2026, 10, 11)                      # a Sunday
FIELDS = ["appointment_id", "client_id", "client_name", "phone", "email", "reminder_opt_in",
          "barber", "service", "price", "appointment_datetime", "status"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def appt(client="C1", name="Sam Lee", when="2026-09-01 10:00", status="Completed", price=35,
         service="Haircut", barber="Marcus", phone="416-555-0101", email="", opt_in="yes", appt_id=None):
    return {"appointment_id": appt_id or "", "client_id": client, "client_name": name, "phone": phone,
            "email": email, "reminder_opt_in": opt_in, "barber": barber, "service": service,
            "price": price, "appointment_datetime": when, "status": status}


def load(rows, today=TODAY):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.csv"
        with open(path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return shopdata.load_appointments(path, today)


def regular(client="C1", name="Sam Lee", every=21, visits=4, last="2026-09-01", **kw):
    """A client who visits every `every` days, ending on `last`."""
    end = datetime.fromisoformat(last)
    return [appt(client, name, (end - timedelta(days=every * i)).strftime("%Y-%m-%d 10:00"), **kw)
            for i in range(visits - 1, -1, -1)]


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------
def test_status_phone_and_price_cleaning():
    df, issues = load([
        appt(status="Complete", phone="(416) 555-0187", price="$35.00", appt_id="A1"),
        appt(client="C2", name="No Show", status="no show", phone="416.555.0188", appt_id="A2"),
        appt(client="C3", name="Cancel", status="Canceled", phone="+1 416 555 0189", appt_id="A3"),
        appt(client="C4", name="Future", when="2026-10-14 15:00", status="Scheduled", appt_id="A4"),
    ])
    assert df["status"].tolist() == ["completed", "no_show", "cancelled", "booked"]
    assert df["phone"].tolist() == ["(416) 555-0187", "(416) 555-0188", "(416) 555-0189", "(416) 555-0101"]
    assert df.loc[0, "price"] == 35.0 and issues == []


def test_bad_rows_are_rejected_with_reasons():
    df, issues = load([
        appt(appt_id="A1"),
        appt(client="", appt_id="A2"),
        appt(when="not a date", appt_id="A3"),
        appt(price="-5", appt_id="A4"),
        appt(status="Rescheduled?", appt_id="A5"),
        appt(appt_id="A1"),
    ])
    assert len(df) == 1
    reasons = [i["issue"] for i in issues]
    assert "Missing client ID or client name" in reasons
    assert any("Invalid date" in r for r in reasons) and any("Invalid price" in r for r in reasons)
    assert any("Unknown status" in r for r in reasons) and "Duplicate appointment ID" in reasons


def test_past_booked_appointments_become_unresolved():
    df, issues = load([appt(when="2026-10-01 10:00", status="Booked", appt_id="A1")])
    assert df.loc[0, "status"] == "unresolved"
    assert issues[0]["severity"] == "Warning"


def test_opt_in_parsing():
    assert shopdata.parse_opt_in("yes", True) == (True, True)
    assert shopdata.parse_opt_in("No", True) == (False, True)
    assert shopdata.parse_opt_in("", True) == (True, True)       # blank follows the config default
    assert shopdata.parse_opt_in("", False) == (False, True)
    assert shopdata.parse_opt_in("maybe?", True) == (False, False)   # unrecognized -> opted out


def test_phone_and_email_validation():
    assert shopdata.clean_phone("1-416-555-0187") == "(416) 555-0187"
    assert shopdata.clean_phone("555-0187") is None
    assert shopdata.clean_phone("000-000-0000") is None
    assert shopdata.clean_email(" Sam@Example.COM ") == "sam@example.com"
    assert shopdata.clean_email("not-an-email") is None


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------
def booked(client="C9", name="Pat Kim", when="2026-10-12 16:30", **kw):
    return appt(client, name, when, "Booked", **kw)


def test_reminder_text_and_wording():
    df, _ = load([booked(service="Fade", barber="Dre")])
    msgs, skipped = messaging.build_reminders(df, date(2026, 10, 12), TODAY, CONFIG)
    assert len(msgs) == 1 and not skipped
    text = msgs[0]["message"]
    assert text.startswith("Hi Pat!") and "Fade with Dre tomorrow at 4:30 PM" in text
    assert text.endswith("Reply STOP to opt out.") and msgs[0]["channel"] == "SMS"
    assert msgs[0]["risk"] == "Normal" and msgs[0]["segments"] in (1, 2)


def test_reminder_for_a_later_day_says_the_weekday():
    df, _ = load([booked(when="2026-10-14 09:00")])
    msgs, _ = messaging.build_reminders(df, date(2026, 10, 14), TODAY, CONFIG)
    assert "on Wednesday, Oct 14" in msgs[0]["message"] and "9:00 AM" in msgs[0]["message"]


def test_repeat_no_shows_get_the_firmer_message():
    history = [appt("C9", "Pat Kim", f"2026-09-0{i} 10:00", "No-Show") for i in (1, 2)]
    history.append(appt("C9", "Pat Kim", "2026-09-10 10:00", "Completed"))
    df, _ = load(history + [booked()])
    msgs, _ = messaging.build_reminders(df, date(2026, 10, 12), TODAY, CONFIG)
    assert msgs[0]["risk"] == "High" and "keep your chair" in msgs[0]["message"]


def test_opted_out_and_unreachable_clients_are_not_messaged():
    df, _ = load([
        booked("C1", "Opted Out", opt_in="no"),
        booked("C2", "No Contact", phone="", email=""),
        booked("C3", "Email Only", phone="", email="e@example.com", when="2026-10-12 17:30"),
    ])
    msgs, skipped = messaging.build_reminders(df, date(2026, 10, 12), TODAY, CONFIG)
    assert [m["client_name"] for m in msgs] == ["Email Only"] and msgs[0]["channel"] == "Email"
    assert not msgs[0]["message"].endswith("opt out.")             # no SMS opt-out line on email
    assert {s["reason"] for s in skipped} == {"Opted out", "No phone or email"}


def test_any_opt_out_on_record_blocks_messages():
    history = [appt("C1", "Sam Lee", "2026-09-01 10:00", opt_in="no")]
    df, _ = load(history + [booked("C1", "Sam Lee", opt_in="yes")])
    msgs, skipped = messaging.build_reminders(df, date(2026, 10, 12), TODAY, CONFIG)
    assert not msgs and skipped[0]["reason"] == "Opted out"


def test_missing_barber_and_contact_values_are_handled():
    df, _ = load([booked(barber="", phone="", email="e@example.com")])
    msgs, _ = messaging.build_reminders(df, date(2026, 10, 12), TODAY, CONFIG)
    assert "with your barber" in msgs[0]["message"] and "nan" not in msgs[0]["message"].lower()
    assert msgs[0]["channel"] == "Email" and msgs[0]["to"] == "e@example.com"


def test_default_target_is_next_booked_day():
    df, _ = load([booked(when="2026-10-14 10:00"), booked("C2", "Other", "2026-10-16 10:00")])
    assert messaging.find_default_target(df, TODAY) == date(2026, 10, 14)


# --------------------------------------------------------------------------
# Rebooking
# --------------------------------------------------------------------------
def test_usual_interval_is_the_median_gap_within_limits():
    assert messaging.usual_interval([20, 22, 21], CONFIG) == 21
    assert messaging.usual_interval([2, 3], CONFIG) == CONFIG["rebooking"]["min_interval_days"]
    assert messaging.usual_interval([200, 300], CONFIG) == CONFIG["rebooking"]["max_interval_days"]
    assert messaging.usual_interval([], CONFIG) == CONFIG["rebooking"]["default_interval_days"]


def test_overdue_client_gets_a_nudge_with_their_own_numbers():
    # every 21 days, last visit 40 days before TODAY -> 1.9x their usual gap
    df, _ = load(regular(every=21, last="2026-09-01", service="Fade", barber="Jay"))
    msgs, skips = messaging.build_rebooking(df, TODAY, CONFIG)
    m = msgs[0]
    assert m["tier"] == "Overdue" and m["days_since"] == 40 and m["usual_interval_days"] == 21
    assert "every 3 wks" in m["message"] and "Jay" in m["message"] and m["message"].endswith("opt out.")


def test_tiers():
    cases = {"Due": "2026-09-14", "Overdue": "2026-09-01", "Lapsed": "2026-06-01"}   # 27, 40 and 132 days ago
    for tier, last in cases.items():
        df, _ = load(regular(every=21, last=last))
        msgs, _ = messaging.build_rebooking(df, TODAY, CONFIG)
        assert msgs[0]["tier"] == tier, (tier, msgs[0]["tier"])


def test_clients_who_are_not_due_are_left_alone():
    df, _ = load(regular(every=21, last="2026-10-01"))          # 10 days ago, usual gap 21
    msgs, skips = messaging.build_rebooking(df, TODAY, CONFIG)
    assert msgs == [] and skips == {"Not due yet": 1}


def test_skip_reasons():
    rows = (regular("C1", "Has Booking", last="2026-08-20") + [booked("C1", "Has Booking", "2026-10-15 10:00")]
            + regular("C2", "Opted Out", last="2026-08-20", opt_in="no")
            + regular("C3", "No Contact", last="2026-08-20", phone="", email="")
            + regular("C4", "Long Gone", last="2025-09-01"))
    df, _ = load(rows)
    msgs, skips = messaging.build_rebooking(df, TODAY, CONFIG)
    assert msgs == []
    assert skips["Already has a booking"] == 1 and skips["Opted out"] == 1
    assert skips["No phone or email"] == 1 and any(k.startswith("Gone too long") for k in skips)


def test_cooldown_and_contact_log():
    df, _ = load(regular(every=21, last="2026-09-01"))
    msgs, _ = messaging.build_rebooking(df, TODAY, CONFIG)
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "contact_log.csv"
        messaging.record_contacts(log_path, msgs, TODAY)
        log = messaging.load_contact_log(log_path)
        assert log == {"C1": TODAY}
        again, skips = messaging.build_rebooking(df, TODAY + timedelta(days=3), CONFIG, log)
        assert again == [] and any("Messaged in the last" in k for k in skips)
        later, _ = messaging.build_rebooking(df, TODAY + timedelta(days=15), CONFIG, log)
        assert len(later) == 1                                  # cooldown has passed


def test_per_run_limit_keeps_the_most_urgent_tier_first():
    cfg = {**CONFIG, "rebooking": {**CONFIG["rebooking"], "max_messages_per_run": 2}}
    rows = (regular("C1", "A Due", last="2026-09-14") + regular("C2", "B Lapsed", last="2026-06-01")
            + regular("C3", "C Overdue", last="2026-09-01") + regular("C4", "D Due", last="2026-09-14"))
    df, _ = load(rows)
    msgs, skips = messaging.build_rebooking(df, TODAY, cfg)
    assert len(msgs) == 2 and [m["tier"] for m in msgs] == ["Due", "Due"]
    assert skips["Over the per-run limit (will appear next run)"] == 2


def test_templates_are_validated():
    messaging.validate_templates(CONFIG)
    bad = {**CONFIG, "templates": {**CONFIG["templates"], "reminder": "Hi {nickname}!"}}
    try:
        messaging.validate_templates(bad)
    except SystemExit as exc:
        assert "reminder" in str(exc)
    else:
        raise AssertionError("an unknown placeholder should be rejected")


# --------------------------------------------------------------------------
# Insights
# --------------------------------------------------------------------------
WEEK = pd.Timestamp("2026-09-28")                       # Mon .. Sun 2026-10-04


def insight_rows():
    return [
        appt("C1", "A", "2026-09-29 10:00", "Completed", 35, "Haircut", "Marcus"),
        appt("C2", "B", "2026-09-29 11:00", "Completed", 50, "Haircut + Beard", "Jay"),
        appt("C3", "C", "2026-09-30 16:00", "No-Show", 40, "Fade", "Marcus"),
        appt("C4", "D", "2026-10-01 16:00", "Cancelled", 40, "Fade", "Marcus"),
        appt("C1", "A", "2026-10-03 17:00", "Completed", 40, "Fade", "Marcus"),
        appt("C1", "A", "2026-09-22 10:00", "Completed", 35, "Haircut", "Marcus"),   # last week
        appt("C2", "B", "2026-09-21 10:00", "Completed", 25, "Kids Cut", "Jay"),     # last week
    ]


def test_weekly_kpis_and_definitions():
    df, _ = load(insight_rows())
    ins = insights.weekly_insights(df, WEEK, CONFIG)
    cur, prev = ins["current"], ins["previous"]
    assert cur["revenue"] == 125.0 and cur["completed"] == 3        # 35 + 50 + 40
    assert cur["no_shows"] == 1 and cur["cancelled"] == 1
    assert abs(cur["no_show_rate"] - 1 / 4) < 1e-9                  # cancellations are excluded
    assert cur["lost_revenue"] == 40.0 and round(cur["avg_ticket"], 2) == 41.67
    assert prev["revenue"] == 60.0
    assert ins["has_prev"]


def test_new_vs_returning_clients():
    df, _ = load(insight_rows())
    cur = insights.weekly_insights(df, WEEK, CONFIG)["current"]
    # C1 and C2 visited before this week; nobody here has their first visit this week
    assert cur["new_clients"] == 0 and cur["returning_clients"] == 2
    df2, _ = load(insight_rows() + [appt("C9", "New Guy", "2026-10-02 12:00", "Completed", 35)])
    cur2 = insights.weekly_insights(df2, WEEK, CONFIG)["current"]
    assert cur2["new_clients"] == 1 and cur2["returning_clients"] == 2


def test_breakdowns_and_changes():
    df, _ = load(insight_rows())
    services = insights.weekly_insights(df, WEEK, CONFIG)["services"]
    assert services.iloc[0]["Service"] == "Haircut + Beard" and services.iloc[0]["revenue"] == 50.0
    assert abs(services["share"].sum() - 1.0) < 1e-9
    haircut = services[services["Service"] == "Haircut"].iloc[0]
    assert haircut["revenue"] == 35.0 and haircut["prev_revenue"] == 35.0 and haircut["change_pct"] == 0.0
    assert pd.isna(services[services["Service"] == "Fade"].iloc[0]["change_pct"])   # new this week


def test_busiest_hours_grid():
    df, _ = load(insight_rows())
    grid = insights.weekly_insights(df, WEEK, CONFIG)["grid"]
    assert list(grid.columns) == CONFIG["opening"]["open_days"]
    assert grid.loc["4:00 PM", "Wed"] == 1                           # the no-show counts (chair was booked)
    assert grid.loc["4:00 PM", "Thu"] == 0                           # the cancellation does not
    assert grid.loc["10:00 AM", "Tue"] == 2                          # Sep 22 and Sep 29 (last 8 weeks)
    assert grid.loc["5:00 PM", "Sat"] == 1


def test_week_selection():
    df, _ = load(insight_rows())
    assert insights.pick_week_start(df, TODAY) == pd.Timestamp("2026-10-05") - timedelta(days=7)
    assert insights.pick_week_start(df, TODAY, "2026-09-23") == pd.Timestamp("2026-09-21")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
def test_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csv_path = tmp / "appointments.csv"
        rows = gen.generate(clients=120, weeks=20, seed=2, today=TODAY)
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=gen.FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        result = assistant.run_assistant(csv_path, Path(__file__).parent / "config.json", tmp / "out", TODAY,
                                         record=True)
        reminders, rebooking = result["reminders"], result["rebooking"]
        assert reminders and rebooking

        # everything promised was written
        for path in result["files"]:
            assert path.exists(), path
        wb = openpyxl.load_workbook(result["report"])
        assert wb.sheetnames == ["Summary", "Daily", "By Service", "By Barber", "Busiest Hours",
                                 "No-Shows", "Weekly Trend", "Data Issues"]
        assert len(wb["Summary"]._charts) == 2
        assert wb["Data Issues"].max_row == 1 + len(result["issues"])       # the 5 flawed rows are listed

        # nobody who opted out, and nobody who already has a booking, is nudged
        df, _ = shopdata.load_appointments(csv_path, TODAY)
        opted_out = set(df.loc[~df["opt_in"], "client_id"])
        upcoming = set(df.loc[(df["status"] == "booked") & (df["day"] >= pd.Timestamp(TODAY)), "client_id"])
        assert not any(m["client_id"] in opted_out | upcoming for m in rebooking)
        assert not any(r["client_id"] in opted_out for r in reminders)

        # recording the nudges stops a second run from repeating them
        second = assistant.run_assistant(csv_path, Path(__file__).parent / "config.json", tmp / "out", TODAY)
        assert not ({m["client_id"] for m in second["rebooking"]} & {m["client_id"] for m in rebooking})


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    raise SystemExit(1 if failed else 0)
