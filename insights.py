"""
Weekly business insights: revenue, no-shows, busiest hours, services, barbers and trend.

Definitions
-----------
* Revenue         = price of completed appointments (cancellations and no-shows earn nothing)
* No-show rate    = no-shows / (completed + no-shows); cancellations are left out because
                    the chair could usually be refilled
* Lost revenue    = price of no-show appointments
* Busiest hours   = completed + no-show appointments by weekday and start hour, last 8 weeks
* New client      = a client whose first completed visit (in the data) was this week
Weeks run Monday to Sunday.
"""

from datetime import timedelta

import pandas as pd

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def money(value: float, decimals: int = 0) -> str:
    return f"${value:,.{decimals}f}"


def pct_change(current, previous):
    if previous is None or pd.isna(previous) or previous == 0:
        return None
    return (current - previous) / previous


def pct_text(change) -> str:
    return "n/a" if change is None else f"{change * 100:+.1f}%"


def pick_week_start(df: pd.DataFrame, today, requested=None) -> pd.Timestamp:
    """Monday of the requested week, or of the last complete week before today."""
    if requested:
        day = pd.Timestamp(requested).normalize()
        return day - timedelta(days=day.weekday())
    this_monday = pd.Timestamp(today).normalize() - timedelta(days=pd.Timestamp(today).weekday())
    return this_monday - timedelta(days=7)


def slice_week(df: pd.DataFrame, start: pd.Timestamp) -> pd.DataFrame:
    return df[(df["day"] >= start) & (df["day"] < start + timedelta(days=7))]


def week_kpis(week: pd.DataFrame, first_visit: pd.Series, start: pd.Timestamp) -> dict:
    completed = week[week["status"] == "completed"]
    no_shows = week[week["status"] == "no_show"]
    attended_or_missed = len(completed) + len(no_shows)
    clients = set(completed["client_id"])
    new = {c for c in clients if start <= first_visit.get(c, pd.Timestamp.max) < start + timedelta(days=7)}
    revenue = float(completed["price"].sum())
    return {
        "completed": len(completed),
        "no_shows": len(no_shows),
        "cancelled": int((week["status"] == "cancelled").sum()),
        "no_show_rate": len(no_shows) / attended_or_missed if attended_or_missed else 0.0,
        "revenue": round(revenue, 2),
        "avg_ticket": revenue / len(completed) if len(completed) else 0.0,
        "lost_revenue": round(float(no_shows["price"].sum()), 2),
        "clients": len(clients), "new_clients": len(new), "returning_clients": len(clients) - len(new),
    }


def breakdown(cur: pd.DataFrame, prev: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    done, prev_done = cur[cur["status"] == "completed"], prev[prev["status"] == "completed"]
    grouped = done.groupby(col).agg(revenue=("price", "sum"), appointments=("price", "size"))
    grouped["avg_price"] = grouped["revenue"] / grouped["appointments"]
    total = grouped["revenue"].sum()
    grouped["share"] = grouped["revenue"] / total if total else 0.0
    grouped["prev_revenue"] = prev_done.groupby(col)["price"].sum()
    grouped["prev_revenue"] = grouped["prev_revenue"].fillna(0.0)
    grouped["change_pct"] = [pct_change(r, p) for r, p in zip(grouped["revenue"], grouped["prev_revenue"])]
    grouped = grouped.sort_values("revenue", ascending=False).reset_index().rename(columns={col: label})
    for col_name in ("revenue", "avg_price", "prev_revenue"):
        grouped[col_name] = grouped[col_name].round(2)
    return grouped[[label, "appointments", "revenue", "share", "avg_price", "prev_revenue", "change_pct"]]


def daily_table(week: pd.DataFrame, start: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for i in range(7):
        day = start + timedelta(days=i)
        d = week[week["day"] == day]
        rows.append({"Date": day.date(), "Day": day.strftime("%a"),
                     "Completed": int((d["status"] == "completed").sum()),
                     "No-Shows": int((d["status"] == "no_show").sum()),
                     "Cancelled": int((d["status"] == "cancelled").sum()),
                     "Revenue": round(float(d.loc[d["status"] == "completed", "price"].sum()), 2)})
    return pd.DataFrame(rows)


def hour_label(hour: int) -> str:
    return f"{(hour - 1) % 12 + 1}:00 {'AM' if hour < 12 else 'PM'}"


def busy_grid(df: pd.DataFrame, week_start: pd.Timestamp, cfg: dict, weeks: int = 8) -> pd.DataFrame:
    """Rows = start hour, columns = open days; values = appointments over the last `weeks` weeks."""
    window = df[(df["day"] >= week_start - timedelta(days=7 * (weeks - 1)))
                & (df["day"] < week_start + timedelta(days=7))
                & df["status"].isin(["completed", "no_show"])]
    hours = list(range(cfg["opening"]["open_hour"], cfg["opening"]["close_hour"]))
    days = cfg["opening"]["open_days"]
    grid = pd.DataFrame(0, index=hours, columns=days)
    for appt in window.itertuples(index=False):
        weekday = WEEKDAYS[appt.day.weekday()]
        if weekday in grid.columns and appt.hour in grid.index:
            grid.at[appt.hour, weekday] += 1
    grid.index = [hour_label(h) for h in hours]
    return grid


def trend_table(df: pd.DataFrame, week_start: pd.Timestamp, first_week: pd.Timestamp, weeks: int = 8) -> pd.DataFrame:
    rows = []
    for i in range(weeks - 1, -1, -1):
        start = week_start - timedelta(days=7 * i)
        if start < first_week:
            continue
        w = slice_week(df, start)
        done, missed = w[w["status"] == "completed"], w[w["status"] == "no_show"]
        total = len(done) + len(missed)
        rows.append({"Week Starting": start.strftime("%b %d"), "Revenue": round(float(done["price"].sum()), 2),
                     "Appointments": len(done), "No-Show Rate": len(missed) / total if total else 0.0})
    return pd.DataFrame(rows)


def no_show_list(week: pd.DataFrame) -> pd.DataFrame:
    missed = week[week["status"] == "no_show"].sort_values("start")
    return pd.DataFrame({"When": missed["start"].dt.strftime("%a %b %d, %I:%M %p"),
                         "Client": missed["client_name"], "Barber": missed["barber"].fillna(""),
                         "Service": missed["service"], "Lost Revenue": missed["price"].round(2)}).reset_index(drop=True)


def build_highlights(start, cur, prev, has_prev, services, barbers, grid, rebooking) -> list:
    lines = []
    change = pct_change(cur["revenue"], prev["revenue"]) if has_prev else None
    if change is None:
        lines.append(f"Revenue for the week was {money(cur['revenue'])} from {cur['completed']} completed appointments.")
    else:
        lines.append(f"Revenue for the week was {money(cur['revenue'])} from {cur['completed']} completed "
                     f"appointments, {'up' if change >= 0 else 'down'} {abs(change) * 100:.1f}% from last week "
                     f"({money(prev['revenue'])}).")

    rate_note = f"{cur['no_show_rate']:.1%}"
    if has_prev:
        rate_note += f" (last week {prev['no_show_rate']:.1%})"
    line = f"No-show rate was {rate_note}: {cur['no_shows']} no-shows cost about {money(cur['lost_revenue'])} in lost revenue."
    if cur["no_show_rate"] >= 0.10 or (has_prev and cur["no_show_rate"] - prev["no_show_rate"] >= 0.03):
        line += " Worth a look: consider confirmation texts or a deposit for repeat no-showers."
    lines.append(line)

    if len(services):
        top = services.iloc[0]
        lines.append(f"{top['Service']} was the top earner with {money(top['revenue'])} "
                     f"({top['share']:.0%} of revenue); average ticket was {money(cur['avg_ticket'], 2)}.")
    if len(barbers):
        top = barbers.iloc[0]
        lines.append(f"{top['Barber']} led the chairs with {money(top['revenue'])} from {top['appointments']} appointments.")

    flat = grid.stack()
    if flat.max() > 0:
        hour, day = flat.idxmax()
        quiet = grid.sum(axis=1).idxmin()
        lines.append(f"Busiest slot over the last 8 weeks: {day} at {hour}. "
                     f"The quietest hour overall is {quiet}, a good time for promotions.")
    lines.append(f"{cur['new_clients']} new and {cur['returning_clients']} returning clients came in.")

    if rebooking is not None:
        lines.append(f"Rebooking opportunity: {rebooking['count']} clients look due for a visit "
                     f"(about {money(rebooking['value'])} in typical spend). Their messages are drafted.")
    return lines


def weekly_insights(df: pd.DataFrame, week_start: pd.Timestamp, cfg: dict, rebooking_summary=None) -> dict:
    """Everything the weekly report needs."""
    week = slice_week(df, week_start)
    prev_week = slice_week(df, week_start - timedelta(days=7))
    resolved = df[df["status"].isin(["completed", "no_show", "cancelled"])]
    first_visit = df[df["status"] == "completed"].groupby("client_id")["day"].min()
    first_week = df["day"].min() - timedelta(days=df["day"].min().weekday())

    cur = week_kpis(week, first_visit, week_start)
    prev = week_kpis(prev_week, first_visit, week_start - timedelta(days=7))
    has_prev = not prev_week[prev_week["status"].isin(["completed", "no_show", "cancelled"])].empty
    services = breakdown(week, prev_week, "service", "Service")
    barbers = breakdown(week, prev_week, "barber", "Barber")
    grid = busy_grid(resolved, week_start, cfg)
    return {
        "start": week_start, "end": week_start + timedelta(days=6), "current": cur, "previous": prev,
        "has_prev": has_prev, "services": services, "barbers": barbers,
        "daily": daily_table(week, week_start), "grid": grid,
        "trend": trend_table(resolved, week_start, first_week), "no_shows": no_show_list(week),
        "highlights": build_highlights(week_start, cur, prev, has_prev, services, barbers, grid, rebooking_summary),
    }
