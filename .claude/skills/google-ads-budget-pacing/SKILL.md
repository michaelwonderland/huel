---
name: google-ads-budget-pacing
description: Check whether Google Ads campaigns are about to hit, or have hit, Google's monthly spending limit (30.4 x average daily budget). Use when spend drops late in the month, when "budget lost impression share" appears on a campaign spending under its daily budget, when diagnosing a CPA/CAC spike that coincides with month end, or as a weekly pacing check.
---

# Google Ads monthly budget pacing check

## Why this matters
Google can spend up to **2x the daily budget** on any single day, but never more than the
**monthly charging limit** in a calendar month. A campaign that over-delivers early in the month
runs out of room late in the month and Google throttles it to whatever headroom is left. Traffic it
would have won then often leaks into overlapping campaigns, typically broad-match or tROAS ones with
spare budget, and shows up as a CPA spike elsewhere.

### The rules
- Monthly limit = **daily budget x 30.4**.
- When the budget changes mid-month, the limit resets to:
  **spend before the change + new daily budget x days remaining in the month (including the change day)**.
- Headroom per day = (limit - month-to-date spend) / days remaining.
- When headroom per day falls below the daily budget, Google slows delivery to match it.
- Small overshoots of the limit (a few hundred pounds) are normal; Google credits overdelivery afterwards.

### The signature in the data
A campaign shows **budget-lost impression share > 0 on days it spends well under its daily budget**,
usually in the last 3-7 days of the month, and recovers on the 1st with no change made.

## Data to pull (Google Ads search tool)
Use customer ID without dashes. If the account sits under an MCC, use the client ID.

1. **Daily spend and budget loss, month to date** (resource `campaign`):
   fields `campaign.name, segments.date, metrics.cost_micros, metrics.search_budget_lost_impression_share`,
   conditions `segments.date BETWEEN '<first of month>' AND '<yesterday>'`,
   `campaign.status = 'ENABLED'`.
2. **Current budgets** (resource `campaign`):
   `campaign.name, campaign_budget.amount_micros, campaign_budget.explicitly_shared, campaign_budget.reference_count`.
   - If the budget is shared, pace the **sum** of spend across every campaign using it.
   - `reference_count` also counts old experiment arms; check which referencing campaigns actually spend.
3. **Budget changes this month** (resource `change_event`, LIMIT <= 10000):
   `change_event.change_date_time, change_event.campaign, change_event.change_resource_type, change_event.user_email`,
   conditions `change_event.change_resource_type = 'CAMPAIGN_BUDGET'` and a date range within the last 30 days.
   - Try adding `change_event.old_resource, change_event.new_resource` to get the amounts.
     That field set sometimes fails through the connector.
   - If you can't get the amounts, infer them. The day before a change, spend falls or budget-lost IS appears.
     Solve for the limit that matches where spend flattened.
   - State clearly when an amount is inferred rather than read.
4. **Account-level caps** (resource `account_budget`). An account budget can also stop spend; check it is not the cause.

Large exports get saved to a file by the tool. Copy them to the scratchpad and parse with Python;
don't read them into context.

## Run the check
```
python3 .claude/skills/google-ads-budget-pacing/budget_pacing.py \
  --daily <campaign_daily.json> --month YYYY-MM \
  --budget "CAMPAIGN NAME=<current daily budget>" \
  [--change "CAMPAIGN NAME:YYYY-MM-DD=<new daily budget>"] \
  [--as-of YYYY-MM-DD]
```
- If a change happened after the 1st and you know the earlier budget, pass it as a change dated the 1st too.
- Use `--as-of` mid-month to project forward.
- For past months, set `--as-of` to the month end to confirm a throttle after the fact.

Output per campaign:
- daily cumulative spend, the limit in force, headroom per day, and budget-lost IS;
- flags `AHEAD OF PACE`, `THROTTLED` and `CONFIRMED`, the last when the signature above is present;
- month to date, run rate, projected month-end spend and the estimated date the limit is reached;
- the maximum spend per day that keeps the campaign unthrottled.

## Interpret and report
- **Confirmed throttle:** say which days, how much spend was lost (normal daily spend minus throttled spend)
  and roughly how many conversions (lost spend divided by that campaign's CPA).
- **Look for where the traffic went:** check whether overlapping campaigns grew on exactly the throttled days.
  Compare search terms by campaign before and during the throttled days.
- **Recommendation options:**
  - Set the daily budget to (intended monthly spend / 30.4), so the limit matches the intent.
  - Accept the month-end slowdown if the monthly cap is deliberate.
  - Lower the daily budget early in the month if front-loading is the problem.
- **Re-run weekly from the ~20th.** An `AHEAD OF PACE` flag gives about a week's notice.
- **Never change budgets yourself.** This skill is diagnostic; recommend, don't apply.

## Worked example (Huel UK, Sep 2026)
`UK_SEARCH_WEIGHT_LOSS`:
- Spent £5,283 on 1-3 Sep, then the budget was edited to £1,000 on 4 Sep.
- Limit = 5,283 + 1,000 x 27 = **£32,283**. Cumulative spend reached £32,289 on 29 Sep.
- 27-30 Sep: spend fell to about £500/day with 9-29% budget-lost IS. It recovered on 1 Oct.
- Run as of 20 Sep, the script projects the limit being reached around 28 Sep.
