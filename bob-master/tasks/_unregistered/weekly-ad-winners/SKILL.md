---
name: weekly-ad-winners
description: Weekly Monday digest of top-performing Meta + TikTok creatives with scale/cut recommendations, joined to GHL leads.
---

You are generating the weekly ad-creative performance digest for Advanced Marketers (a marketing agency in Orange County, CA), AND refreshing the saved "winning-creatives" gallery artifact. Run these steps.

DATA SOURCES:
1. Supermetrics MCP (discover the connected Supermetrics tools if the prefix is unknown). Pull AD-LEVEL performance for the LAST 7 DAYS:
   - Facebook Ads (ds_id "FA"), account act_383628763095207. Fields: adgroup_name, adgroup_id, cost_usd, impressions, CPM, link_clicks, frequency, video_thruplay_watched_actions. date_range_type last_7_days. Use data_query then poll get_async_query_results until completed. NOTE: video asset URL fields cannot be queried together with ThruPlay — if you want watch links, run a SEPARATE query with adgroup_id + video_asset_url.
   - TikTok Ads (ds_id "TIK"), account 7631268331820761089. Fields: ad_name, ad_id, post_url, cost_usd, impressions, video_play_actions, video_watched_6s, conversions. date_range_type last_7_days.
2. Google Drive MCP: read the Google Sheet "GHL - Lead Update Meta Report" (fileId 17qrXY-1HKwj7A8elnJJX93amezXYbKY4jmuNIKIrzAY) with read_file_content. Count leads / won / lost per ad_id from the leads table.

ANALYSIS:
- Join ad performance to GHL outcomes on ad_id. Per creative compute: spend, CPM, ThruPlays (Meta) or 6-sec views + conversions (TikTok), frequency, leads, cost-per-lead.
- SCALE list: efficient creatives (low CPM, strong hook/ThruPlay or 6-sec hold, with leads/conversions) that deserve more budget.
- CUT/FATIGUE list: high spend + weak results, or frequency > 2.8.
- Best creative per platform, with watch link if a public URL exists (Meta video_asset_url path → prefix https://www.facebook.com ; TikTok post_url).

OUTPUTS (do BOTH):
A) A short prose digest for the founder: (1) headline scale/cut, (2) top creative per platform with metrics, (3) fatigue warnings, (4) brief week-over-week note. Tight and actionable, no long tables.
B) Rebuild the persistent artifact with id "winning-creatives" using update_artifact: a clean light-mode HTML gallery of the current top ~6 Meta and top ~5 TikTok videos as cards (rank, ad name, metric chips, and a Watch button via openLink where a public link exists; otherwise an "In Ads Manager" tag). Keep it self-contained. Write the HTML to a file first, then call update_artifact with id "winning-creatives".

If a data source fails, note it briefly and continue with what you have.