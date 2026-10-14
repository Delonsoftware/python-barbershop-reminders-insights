# Barbershop Reminder, Rebooking & Insights Automation

A Python tool that helps a barbershop **cut no-shows and fill empty chairs**. From a plain
appointment export it drafts three things:

1. **Next-day reminders** for every booked appointment (with a firmer "please confirm" version for repeat no-showers)
2. **Rebooking nudges** for clients who are overdue for their usual cut, in three tones (Due, Overdue, Lapsed)
3. **A weekly business report**: revenue, no-show rate and lost revenue, busiest hours (heatmap), services, barbers, 8-week trend

**Nothing is sent.** The tool writes drafts and import-ready CSVs, so a person reviews every message first.
Clients who opted out are never included.

All data in the sample is synthetic: fictional clients, a fictional shop, and phone numbers in the reserved 555-01xx range.

## Outputs

```
output/
  messages_2026-10-12.xlsx        reminders + rebooking drafts, colour-coded by tier
  reminders_2026-10-13.csv        import-ready reminder messages
  rebooking_2026-10-12.csv        import-ready rebooking messages
  weekly_report_2026-10-05.xlsx   the business report, with charts and a busiest-hours heatmap
```

Example drafts:

> Hi Chen! Reminder: Beard Trim with Dre tomorrow at 9:00 AM. Reply C to confirm. Need to change it? 416-555-0142 - Fade Factory
> Reply STOP to opt out.

> Hi Kwame, you're usually in every 4 wks and it's been 7. Want a chair with Jay? fadefactory.example/book - Fade Factory
> Reply STOP to opt out.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python generate_sample_appointments.py                 # creates data/appointments.csv (about 6 months of history)
python barber_assistant.py                             # reminders + rebooking + weekly report
python barber_assistant.py --only reminders --date 2026-10-13
python barber_assistant.py --only report --week-start 2026-10-05
python barber_assistant.py --record                    # also log who was nudged (14-day cooldown)
```

By default, reminders are drafted for the **next day that has bookings** (normally tomorrow),
and the report covers the **last complete Mon-Sun week**.

Use your own data: the CSV needs `client_id, client_name, appointment_datetime, service, price, status`.
Optional: `appointment_id, barber, phone, email, reminder_opt_in`. Status can be Completed, No-Show,
Cancelled or Booked (common spellings are recognized).

## How rebooking works

For each client the tool finds their **usual gap** between visits (the median of their last few gaps),
then compares it with the time since their last completed visit:

| Tier | When | Message |
|---|---|---|
| Due | 1.15x to 1.6x their usual gap | friendly nudge |
| Overdue | 1.6x to 3x | "you're usually in every N weeks" |
| Lapsed | 3x or more, or 120+ days | win-back message |

Skipped on purpose: clients who already have a booking, opted out, have no phone or email,
were messaged in the last 14 days (with `--record`), or haven't been in for 270+ days.
Everything is adjustable in `config.json`, including the message wording. Templates are
checked at startup so a typo in a `{placeholder}` fails with a clear message.

## How the report defines things

- **Revenue**: price of completed appointments
- **No-show rate**: no-shows / (completed + no-shows); cancellations are excluded because the chair can often be refilled
- **Lost revenue**: price of no-show appointments
- **Busiest hours**: completed + no-show appointments by weekday and start hour over the last 8 weeks
- **New client**: first completed visit in the data happened this week

## Input checks

Rows with a missing client, an invalid date, a bad price, an unknown status or a duplicate ID are
rejected and listed on the **Data Issues** sheet. Past appointments still marked "Booked" are flagged
for a status update and left out of revenue and no-show numbers.

## Notes and limitations

- Marketing texts need the client's consent (for example under CASL in Canada or the TCPA in the US).
  The tool respects an opt-in column, but making sure you actually have consent is up to the shop.
  This is not legal advice.
- A blank opt-in value follows `default_opt_in` in `config.json`; unrecognized text is treated as opted out.
- Service duration isn't in the data, so there is no utilization metric.
- Messages are only drafted. Connecting an SMS provider is a separate step.

## Tests

```bash
python test_barbershop.py
```

26 tests cover input validation, reminder wording and risk tiers, opt-out handling, the rebooking
tiers and skip rules, the contact-log cooldown, the weekly metrics and their definitions, and a full
end-to-end run (including that nobody who opted out or already has a booking is nudged).

## Tech

Python, pandas, openpyxl (native Excel charts and conditional formatting).
