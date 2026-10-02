"""Month-to-date budget pacing and monthly spend-limit check for Google Ads campaigns.

Google charges at most 30.4 x the average daily budget per calendar month. When a
budget changes mid-month the limit resets to:
    limit = spend before the change + new_daily_budget * days remaining (incl. change day)
Google can spend up to 2x the daily budget on a single day, so campaigns that
regularly over-deliver hit the limit before month end and get throttled to
(limit - spend to date) / days left.

Input: JSON export {"result": [...]} with rows of
    campaign.name, segments.date, metrics.cost_micros,
    metrics.search_budget_lost_impression_share (optional)
Budgets come from --budget NAME=AMOUNT and optional --change NAME:YYYY-MM-DD=AMOUNT
(new daily amount from that date, read from change_event CAMPAIGN_BUDGET rows).
"""
import argparse
import calendar
import datetime as dt
import json
from collections import defaultdict


def parse_kv(items, sep):
    out = defaultdict(list)
    for item in items or []:
        key, val = item.rsplit("=", 1)
        if sep and sep in key:
            name, date = key.rsplit(sep, 1)
            out[name].append((dt.date.fromisoformat(date), float(val)))
        else:
            out[key].append((None, float(val)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True, help="campaign x day export")
    ap.add_argument("--month", required=True, help="YYYY-MM to evaluate")
    ap.add_argument("--budget", action="append", required=True, help="CAMPAIGN=current daily budget")
    ap.add_argument("--change", action="append", help="CAMPAIGN:YYYY-MM-DD=new daily budget from that date")
    ap.add_argument("--as-of", help="last complete date (default: last date in data)")
    args = ap.parse_args()

    y, m = map(int, args.month.split("-"))
    first = dt.date(y, m, 1)
    days_in_month = calendar.monthrange(y, m)[1]
    last = dt.date(y, m, days_in_month)
    current = {k: v[0][1] for k, v in parse_kv(args.budget, None).items()}
    changes = parse_kv(args.change, ":")

    rows = defaultdict(dict)
    for r in json.load(open(args.daily))["result"]:
        d = dt.date.fromisoformat(r["segments.date"])
        if first <= d <= last and r["campaign.name"] in current:
            rows[r["campaign.name"]][d] = (r["metrics.cost_micros"] / 1e6,
                                          r.get("metrics.search_budget_lost_impression_share") or 0)

    for name, by_day in rows.items():
        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else max(by_day)
        # Monthly limit at the start of the month uses the budget in force on day 1.
        chg = sorted(changes.get(name, []))
        start_budget = current[name]
        if chg:
            # The budget before the first change is unknown from the change list alone;
            # if the first change is after day 1 the caller should pass the old value as a change on day 1.
            start_budget = chg[0][1] if chg[0][0] <= first else start_budget
        limit = start_budget * 30.4
        cum = 0.0
        print(f"\n{name}  (month {args.month}, current daily budget £{current[name]:,.0f})")
        print(f"{'date':12}{'spend':>9}{'cum':>10}{'limit':>10}{'headroom/day':>14}{'budget lost':>13}  flag")
        for i in range(days_in_month):
            d = first + dt.timedelta(days=i)
            for cd, amt in chg:
                if cd == d and cd != first:
                    limit = cum + amt * (days_in_month - i)
            if d > as_of:
                break
            spend, lost = by_day.get(d, (0.0, 0.0))
            days_left = days_in_month - i
            headroom = (limit - cum) / days_left
            budget_today = next((a for cd, a in reversed(chg) if cd <= d), start_budget)
            flag = ""
            if headroom < 0.6 * budget_today:
                flag = "THROTTLED: headroom well below daily budget"
            elif headroom < 0.9 * budget_today:
                flag = "AHEAD OF PACE: limit will bind before month end"
            if lost > 0 and spend < 0.9 * budget_today:
                flag += " | CONFIRMED: budget-lost IS while spending under daily budget"
            cum += spend
            print(f"{d.isoformat():12}{spend:>9,.0f}{cum:>10,.0f}{limit:>10,.0f}{headroom:>14,.0f}{lost:>13.0%}  {flag}")
        days_left = (last - as_of).days
        run_rate = cum / ((as_of - first).days + 1)
        if days_left > 0:
            proj = cum + run_rate * days_left
            exhaust = None
            if run_rate > 0 and proj > limit:
                exhaust = as_of + dt.timedelta(days=int((limit - cum) / run_rate) + 1)
            print(f"  MTD £{cum:,.0f} of £{limit:,.0f} limit; run rate £{run_rate:,.0f}/day; "
                  f"projected £{proj:,.0f}" + (f"; limit reached ~{exhaust}" if exhaust else "; within limit"))
            print(f"  Max spend/day to stay unthrottled: £{(limit - cum) / days_left:,.0f}")
        else:
            print(f"  Month closed: spent £{cum:,.0f} vs limit £{limit:,.0f}")


if __name__ == "__main__":
    main()
