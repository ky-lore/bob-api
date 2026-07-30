---
name: weekly-hoo-brief
description: Friday weekly brief on Jaime Falcon (HOO): flags, follow-through, and notable calls this week
---

You are preparing a WEEKLY brief on Jaime Falcon, Head of Operations (jaime@advancedmarketers.co), for Chris Paniagua (chris@advancedmarketers.co), founder/owner of Advanced Marketers, Orange County CA (timezone America/Los_Angeles). Jaime leads the team day-to-day. This is the fast weekly pulse; a separate monthly task does the deep trend analysis. Purpose is evaluation AND coaching.

== DATA COLLECTION (Zoom server-to-server API via bash) ==
Credentials are in zoom-credentials.txt inside the weekly-team-call-qa-digest task folder at ~/Documents/Claude/Scheduled/weekly-team-call-qa-digest/ (request directory access if needed). NEVER print the CLIENT_SECRET or any token.
1. Token: POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=$ACCOUNT_ID with header "Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)". Refresh on 401.
2. GET https://api.zoom.us/v2/users/jaime@advancedmarketers.co/recordings?from=YYYY-MM-DD&to=YYYY-MM-DD&page_size=300 for the past 7 days. Also pull this week's "Team Leads Weekly" from kyle@advancedmarketers.co's recordings.
3. For each meeting with a TRANSCRIPT file: double-URL-encode the uuid, GET /v2/meetings/{uuid}/recordings?include_fields=download_access_token&ttl=3600, then download {transcript download_url}?access_token={download_access_token}.
4. Read all transcripts (delegate to a subagent if large).

== TRACKING CONTEXT ==
Baseline growth areas being watched (from June 1-10 review): scheduling/calendar friction; prep gaps under volume; rapport crowding out structured interviews; recorded-line discipline (profanity on recorded calls); deciding fundamentals live in front of vendors instead of pre-aligning. Search ClickUp (clickup_search) for the most recent "HOO weekly brief" and "HOO monthly brief" tasks and carry forward their open commitments list.

== WEEKLY BRIEF CONTENT (keep it tight — one page) ==
1. THIS WEEK'S CALLS: one-line list of his calls reviewed (topic, date, type).
2. FOLLOW-THROUGH: commitments from prior weeks' open list — kept, pending, or missed (missed = flag). New commitments made this week, added to the open list.
3. FLAGS: only if evidenced, with short verbatim quotes — broken commitments, disparaging talk about clients/leadership/company, legal or compliance exposure, client churn risk from his decisions, team friction caused or ignored, recurrence of baseline growth areas. If none: say "No flags this week."
4. NOTABLE: 1-3 moments worth Chris's attention, good or bad, with quotes.
5. COACHING NOTE: one specific, behavior-based suggestion Chris could pass to Jaime this week.

== OUTPUT ==
1. Create a ClickUp task in list "Chris - Action Items" (list_id 901417226802) named "HOO weekly brief — week of [date]", assignees ["me"], priority normal, due Monday, with the brief as markdown_description. If one already exists for this week, add a comment instead.
2. End the run with: flags (or "no flags"), follow-through score (X of Y commitments kept), and the ClickUp link.

Constraints: behavior-based and factual; describe what he said and did, never personality verdicts; cite meeting + date for every claim; note that transcripts miss Slack, in-person work, and off-Zoom 1:1s; room-mic speaker labels are unreliable — attribute by content. Never expose credentials. Do not message anyone or modify anything beyond the one ClickUp task.