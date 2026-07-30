---
taskId: weekly-churn-early-warning
schedule: "Mondays 6:00 AM (per BOB-OPERATIONS.md / REBUILD-PACK.md)"
execution: CLOUD — still running under Chris's PERSONAL Claude account, not yet moved to the team account (known drift, see project brief)
description: Monday 6am churn-risk scan of all Advanced Marketers client ad accounts via Supermetrics
source: pulled verbatim from ~/Documents/Claude/Scheduled/weekly-churn-early-warning/SKILL.md
---

You are running the weekly churn early-warning scan for Advanced Marketers (marketing agency, Orange CA, clients are service-industry contractors). Objective: score every active client ad account on churn risk and deliver a ranked summary Christian can bring to his Monday growth-team call.

## Data pull (Supermetrics MCP)

Use the Supermetrics connector (data_query + get_async_query_results, timezone America/Los_Angeles). Compare the last 14 complete days vs the prior 14 days (custom date ranges, ending yesterday).

1. Google Ads (ds_id "AW"): fields `Accountname_fromAW,Impressions,Clicks,Cost,Conversions`, settings {"exclude_invalid_accounts": true}, one query per period. Query these roster-matched, hand-audited account IDs (client roster 2026-07-02): 1031051775,1257416364,1282833637,1297430835,1312101984,1313981988,1358688007,1651214601,1676606148,1925489395,2271397182,2405047429,2439100413,2537755600,2840818097,2993364907,3211501986,3217451984,3362882249,3606379421,3732695598,4068957505,4363689052,4777558591,4781538868,5773415499,6014031582,6149092715,6295796429,6361937793,6535500805,7000074284,7145681413,7196209317,7240141945,7266435044,7314021821,7600055319,7768476970,8118243442,8144144464,8405520158,8617008899,8633821943,8784005764,8990563468,9152156731,9236832650,9336346574,9353838254,9368379590,9466593544,9475349656,9510063547,9581576521,9731233729,9736283774,9766370401,9824944905,9848833215,9862134305,9950847531
2. Meta (ds_id "FA"): fields `profile,impressions,cost,onsite_conversion.lead_grouped,offsite_conversions_fb_pixel_lead` (leads = onsite + website leads summed), one query per period. Roster-matched FA account IDs: act_1012388419936404,act_1062395335238827,act_1174830948187234,act_1190494109254907,act_1196693142000570,act_1210908176591694,act_1228410016165077,act_1239007188098378,act_1243630927237087,act_1266677795465665,act_1323695252832013,act_1349325277247354,act_1383764449294315,act_1393920718474059,act_1513942253501297,act_1640507733934930,act_1651044332144556,act_1663533661278319,act_1687621139661956,act_1687795268933772,act_1721016555979048,act_1952179088836493,act_2106855623455786,act_2231151677406389,act_2343610222790741,act_2374000689369411,act_26782908084669543,act_3036508903312321,act_399472862149970,act_758278763704880,act_758742387270645,act_769173841758686,act_778012001879509,act_829697866612924
3. Google Ads call log, current period only: fields `Accountname_fromAW,CallStatus,CallDuration` (max_rows 5000), same AW account IDs. Per account compute: total calls, missed count, short count (received under 60 seconds).

Refer to accounts by their roster client name where it differs from the ad account name (e.g. "Abs Plumbing" = Absolute Best Service Plumbing; "Rosewood" = Rosewood Landscape; "Ram dumpsters" = Ram Dump Truck; "Mad Differentials" = Mad Differential; "Reel Electric*" = The Reel Electric Company; "M14826 - Roof Solutions & Construction - GLS" = Roof Solutions; "(LSA) At Your Service Heating and Cooling" = At Your Service Heating and Air).

Known issue: if a query fails with a "prioritised accounts" error, the Supermetrics subscription is limiting account access — report which accounts you could not cover and tell Christian to fix it at https://hub.supermetrics.com/subscriptions/1816146 rather than silently reporting partial data as complete.

## Scoring (Advanced Marketers standard — do not change weights)

Per account (skip accounts with under $5 spend in both periods):
- Paused/stopped (prior spend >$100, current <$10): +45
- Spend down >40%: +30; down >20%: +20
- Leads down >50%: +30; down >25%: +20 (only when prior leads >= 5)
- CPL up >30% vs prior: +15 (only when prior leads >= 5)
- Missed-call rate >15% (min 5 calls): +10
- Short-call share >50% (min 5 calls): +10
Cap at 100. Tiers: 50+ = AT RISK, 25-49 = WATCH, else HEALTHY.

## Blind spots (no ad account connected — cannot be scored from ad data)

End the briefing with one line noting these roster clients are not covered: OG Plumbing, Lincoln Plumbing & Rooter, Apex Landscape & Construction, Pro Made Roofing, Golden Rule Construction and Remodeling, Leo's Cleaning Service LLC, Build By JM LLC, Drain Force Plumbing, Mariachi Corazon de Maria, alexandro hernandez, LaFara Luxury Properties, Cortex Plastering, NLN Consulting, Moreno Construction & Renovation, Crown Detail AZ, 5 Blox, Nations Instant Insurance Services, Amaral's Construction Corp, Saucedo Plumbing, M&M Cellars, Axxel Plumbing, Next Era Heating & Air Conditioning, Ground Up Plumbing 24-7, Forbes Landscape Services, Next Level Sheet Metal, Van's Carpet and Flooring, Bella Banana Pet Spa, Rising Green, Western Linq, Primal Windows, Shelby Plumbing, Redwood Coast Cleaning, Vizeon Construction, Unleashed Electric, Clog Monkey, Bodied by Jace.

## Output

Deliver a message summarizing: counts by tier, then every AT RISK and WATCH account with its platform (Google/Meta), score, and the specific signals that fired (e.g. "spend -34%, leads -41%, CPL +38%"), sorted by score descending. Note total tracked 14-day spend. Keep it tight — this is a Monday-morning briefing, not a report. Remind that the live dashboard artifact "churn-early-warning" shows the same data on demand.

Verify before sending: spot-check at least one flagged account's numbers against the raw query rows; never fabricate values for accounts whose data failed to load — list them as "not covered" instead.
