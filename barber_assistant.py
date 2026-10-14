#!/usr/bin/env python3
"""
Barbershop Reminder, Rebooking & Insights Automation
====================================================

Reads a barbershop's appointment export and produces three things:

  1. Next-day REMINDER messages for every booked appointment (with a firmer version for
     clients who often no-show)
  2. REBOOKING nudges for clients who are overdue for their usual cut, in three tones
     (Due / Overdue / Lapsed)
  3. A WEEKLY REPORT: revenue, no-show rate, busiest hours, services, barbers, trend

Nothing is sent. The tool drafts messages and writes CSVs for an SMS/email tool, so a
person always reviews them first. Clients who opted out are never included.

Outputs (in output/):
    messages_<date>.xlsx          reminders + rebooking drafts, colour-coded
    reminders_<date>.csv          import-ready reminder messages
    rebooking_<date>.csv          import-ready rebooking messages
    weekly_report_<week>.xlsx     the business report with charts and a busiest-hours heatmap

Usage
-----
    python generate_sample_appointments.py            # make sample data first
    python barber_assistant.py                        # everything
    python barber_assistant.py --only reminders --date 2026-10-13
    python barber_assistant.py --only report --week-start 2026-10-05
    python barber_assistant.py --record               # also log who was nudged (14-day cooldown)
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import insights
import messaging
import reports
import shopdata

log = logging.getLogger("barber")


def run_assistant(input_path: Path, config_path: Path, output_dir: Path, today: date,
                  only: str = "all", target: date = None, week_start: str = None,
                  record: bool = False, contact_log_path: Path = None) -> dict:
    config = shopdata.load_config(config_path)
    messaging.validate_templates(config)
    df, issues = shopdata.load_appointments(input_path, today, config.get("default_opt_in", True))
    rejected = sum(1 for i in issues if i["severity"] == "Rejected")
    log.info("Loaded %d appointments (%d rows rejected, %d warnings)", len(df), rejected, len(issues) - rejected)
    if df.empty:
        sys.exit("No valid appointments to work with.")

    result = {"issues": issues, "files": []}
    contact_log_path = contact_log_path or output_dir / "contact_log.csv"

    # ---- rebooking (also feeds the weekly report's highlights) ----
    rebooking, skip_counts = [], {}
    if only in ("all", "rebooking", "report"):
        rebooking, skip_counts = messaging.build_rebooking(
            df, today, config, messaging.load_contact_log(contact_log_path))
        log.info("Rebooking: %d clients to nudge", len(rebooking))
    result["rebooking"], result["skip_counts"] = rebooking, skip_counts

    # ---- reminders + messages workbook ----
    if only in ("all", "reminders", "rebooking"):
        target = target or messaging.find_default_target(df, today)
        reminders, skipped = [], []
        if target is None:
            log.info("No booked appointments after today, so no reminders to draft")
        else:
            reminders, skipped = messaging.build_reminders(df, target, today, config)
            log.info("Reminders: %d drafted for %s (%d not messaged)", len(reminders), f"{target:%a %b %d}", len(skipped))
        result.update({"reminders": reminders, "skipped_reminders": skipped, "target": target})

        reminders_csv, rebooking_csv = reports.write_messages_csvs(
            output_dir, target or today, today, reminders, rebooking)
        workbook = output_dir / f"messages_{today:%Y-%m-%d}.xlsx"
        reports.write_messages_workbook(workbook, target, today, reminders, skipped,
                                        rebooking, skip_counts, config)
        result["files"] += [workbook, reminders_csv, rebooking_csv]

        if record and rebooking:
            messaging.record_contacts(contact_log_path, rebooking, today)
            log.info("Recorded %d rebooking contacts in %s", len(rebooking), contact_log_path)

    # ---- weekly report ----
    if only in ("all", "report"):
        start = insights.pick_week_start(df, today, week_start)
        summary = ({"count": len(rebooking), "value": sum(m["avg_spend"] for m in rebooking)}
                   if rebooking else None)
        ins = insights.weekly_insights(df, start, config, summary)
        report_path = output_dir / f"weekly_report_{start:%Y-%m-%d}.xlsx"
        reports.write_weekly_workbook(report_path, ins, issues, input_path.name, len(df))
        result.update({"insights": ins, "report": report_path})
        result["files"].append(report_path)
        log.info("Weekly report for %s to %s", f"{start:%b %d}", f"{ins['end']:%b %d}")
    return result


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file_handler = logging.FileHandler("barber_assistant.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(console)
    log.addHandler(file_handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Barbershop reminders, rebooking nudges and weekly insights")
    parser.add_argument("--input", type=Path, default=Path("data/appointments.csv"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--only", choices=["all", "reminders", "rebooking", "report"], default="all",
                        help="run just one part (default: all)")
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="treat this date as today (default: the real date)")
    parser.add_argument("--date", type=date.fromisoformat, default=None, dest="target",
                        help="date to draft reminders for (default: the next day with bookings)")
    parser.add_argument("--week-start", default=None,
                        help="a date inside the week to report on (default: the last complete week)")
    parser.add_argument("--record", action="store_true",
                        help="log the rebooking messages so nobody is nudged again within the cooldown")
    args = parser.parse_args()

    setup_logging()
    today = args.today or date.today()
    result = run_assistant(args.input, args.config, args.output_dir, today, args.only,
                           args.target, args.week_start, args.record)

    log.info("-" * 60)
    if "reminders" in result:
        high = sum(1 for r in result["reminders"] if r["risk"] == "High")
        log.info("Reminders : %d drafted (%d firmer 'please confirm' versions)", len(result["reminders"]), high)
    if result["rebooking"] and args.only != "report":
        tiers = {t: sum(1 for m in result["rebooking"] if m["tier"] == t) for t in ("Due", "Overdue", "Lapsed")}
        log.info("Rebooking : %d nudges (Due %d, Overdue %d, Lapsed %d)", len(result["rebooking"]),
                 tiers["Due"], tiers["Overdue"], tiers["Lapsed"])
    if "insights" in result:
        for line in result["insights"]["highlights"]:
            log.info("* %s", line)
    log.info("-" * 60)
    for path in result["files"]:
        log.info("Wrote %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
