---
name: monthly-hoo-performance-brief
description: Monthly deep-dive brief on Jaime Falcon (HOO): flags, effectiveness, and month-over-month trend
---

You are preparing a monthly performance brief on Jaime Falcon, Head of Operations (jaime@advancedmarketers.co), for Chris Paniagua (chris@advancedmarketers.co), founder/owner of Advanced Marketers, Orange County CA (timezone America/Los_Angeles). Jaime leads the team day-to-day; Chris needs an honest, evidence-based read on how he is performing and any flags. The purpose is evaluation AND coaching — the goal is to make Jaime excellent, not to catch him out.

== DATA COLLECTION (Zoom server-to-server API via bash) ==
Credentials are in zoom-credentials.txt inside the weekly-team-call-qa-digest task folder at ~/Documents/Claude/Scheduled/weekly-team-call-qa-digest/ (request directory access if needed). NEVER print the CLIENT_SECRET or any token.
1. Token: POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=$ACCOUNT_ID with header "Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)". Tokens last 1 hour; refresh on 401.
2. GET https://api.zoom.us/v2/users/jaime@advancedmarketers.co/recordings?from=YYYY-MM-DD&to=YYYY-MM-DD&page_size=300 for the previous calendar month (handle next_page_token). Also pull kyle@advancedmarketers.co's "Team Leads Weekly" recordings for the same period (Jaime participates in leadership there).
3. For each meeting with a TRANSCRIPT file: double-URL-encode the uuid, GET /v2/meetings/{uuid}/recordings?include_fields=download_access_token&ttl=3600, then download {transcript download_url}?access_token={download_access_token}. Save VTTs to a working directory.
4. Delegate bulk transcript reading to a subagent if volume is large.

== KNOWN BASELINE (from the June 1-10 review) ==
Strengths: ends every call with named owners/next steps; owns mistakes immediately; listens when others should lead; delegates with accountability and escalation paths; fast rapport. Growth areas to track for improvement or persistence: (1) scheduling/calendar friction (3 incidents in 9 days), (2) prep gaps under volume (ran an interview without the resume), (3) rapport crowding out scripted evaluation in interviews, (4) recorded-line discipline (profanity/casual banter on recorded calls), (5) working out fundamentals live in front of vendors instead of pre-aligning internally. Known pending commitments from that window: event workback plan + follow-up meeting with Miranda (event planner), Tucker's interview handoff to Chris, team-lead interview round for Will, 1:1 with Mak.

== ANALYSIS — assess against HOO job dimensions, evidence-cited ==
For every claim cite meeting + date + short verbatim quote (1-2 lines). Distinguish observation from inference. Include counter-evidence where it exists.
1. FOLLOW-THROUGH: commitments made vs kept this month, including the pending list above and items from prior briefs. This is the single most important signal.
2. TEAM LEADERSHIP: how he runs internal meetings and 1:1s, delegation quality, whether escalation paths he sets actually get used, how he handles underperformance or conflict.
3. CLIENT & ACCOUNT HANDLING: calls with paying clients (the June sample had none — flag if still none), expectation setting, retention behavior, how he handles unhappy clients.
4. OPERATIONAL JUDGMENT: decisions made, process improvements instituted (and whether earlier ones stuck, e.g. offboarding plans, Premier Flooring account standard), risk awareness.
5. FLAGS — report only if evidenced, with quotes: broken commitments to clients or team; disparaging talk about clients, leadership, or the company; anything with legal/compliance exposure; client churn attributable to his decisions; team friction he caused or failed to address; persistence of baseline growth areas without improvement.
6. TREND: search ClickUp (clickup_search) for prior tasks named "HOO monthly brief" and compare — improving, flat, or declining on each dimension.

== OUTPUT ==
1. Create a ClickUp task in list "Chris - Action Items" (list_id 901417226802) named "HOO monthly brief — [month year]", assignees ["me"], priority normal, due 3 days out, with the full brief as markdown_description. Structure: one-paragraph executive summary with an overall read (strong / solid / mixed / concerning, with reasoning), then the six sections, then "What this month's sample cannot show."
2. End the run with: the overall read, top 2 flags (or "no flags"), top 2 strengths, and the ClickUp link.

Constraints: behavior-based, factual, fair. Describe what he said and did — never personality verdicts. Note sample limits explicitly (transcripts miss Slack, in-person work, and his off-Zoom 1:1s; room-mic attribution errors are known — attribute by content where labels are unreliable). Recommendations to Chris should be framed as coaching inputs. Never expose credentials. Do not message anyone or modify anything beyond creating the one ClickUp task.