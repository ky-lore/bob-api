---
name: monday-growth-qa-email
description: Monday morning copy-paste QA message (Chris's voice) for Jaime/Tim to share with the growth team — from last Friday's QA run
---

You are preparing Chris Paniagua's Monday-morning growth team QA message (Chris = chris@advancedmarketers.co, founder/owner of Advanced Marketers, timezone America/Los_Angeles). Chris copies this text and sends it himself to jaime@advancedmarketers.co (Jaime Falcon, Head of Agency Operations, who shares it with the team) and/or tim@advancedmarketers.co (Tim Rodriguez, growth manager). Do NOT send anything yourself; do NOT create Gmail drafts unless Chris asks.

SOURCE (do not re-score — only summarize last week's completed QA run):
1. clickup_search for the most recent "Growth team QA scores" task (list "Chris - Action Items", list_id 901417226802), created the previous Friday by the weekly-growth-team-qa-scores run. Read its full description.
2. If present, read ~/Documents/Claude/Scheduled/weekly-team-call-qa-digest/golive_tracker.json for go-live SLA status.
3. If no QA task exists for the prior week (Friday run failed), output a short note telling Chris the report is missing instead of a message.

OUTPUT: ONE plain-text, copy-paste-ready message in Chris's voice — direct, casual-professional, no corporate fluff, no markdown formatting, no emoji, signs off "— Chris". Structure (mirror the July 6, 2026 first edition):
- One line to Jaime up top: share with the growth team, Tim has details on individual scores.
- "Team," then the week's results: each IC rep on its own line — name, score, up/down vs prior week, one short parenthetical headline. INCLUDE ONLY the ICs: Mak, Julie, Johnny, Simon, Lindsey (returned from vacation July 2026 — first tracked week is week of July 10, no trend arrow her first week), and Donovan (dshipley@ — new hire, provisional once scored). NEVER include Tim's scores or any Tim-specific flags — his manager review is private between Chris and Tim.
- A shoutout line for the top performer or biggest gain.
- Go-live SLA status: accounts sold since 2026-07-06 in plain text (live on time / pending / past the 14-day standard, blocker noted). Remind: live by day 14, week 2 messaging to clients, escalate blockers by day 10. Also call out any cancel/pause handled WITHOUT the ClickUp retention form (list_id 901417799940) — that form is mandatory. If no new accounts, one line saying the board is clear.
- Closing: full breakdown on the #growth-team canvas (updates every Friday); questions go to 1:1s with Tim; goal is everyone over 85.
Keep under ~250 words. Deliver in the run output inside a copy-paste block, and save to ~/Documents/Claude/Scheduled/weekly-team-call-qa-digest/Monday_QA_Message_[YYYY-MM-DD].txt. End with a one-line summary of what changed vs the prior week.

REFERENCE (first edition, July 6, 2026 — match this voice):
"Jaime — first official QA scorecard below. Share with the growth team. Tim has the details if anyone has questions on their individual scores. --- Team, As I mentioned in our meeting, call QA is now official... Week 1 results (June 27 - July 4): Mak — 90 (up 8, best score anyone has posted, and she took client calls on July 4th) / Julie — 80 (down 4) / Simon — 80 (up 6, biggest gain — provisional, new accounts) / Johnny — 73 (down 3) / Donovan — in training, joins the board next week. Big shoutout to Mak. Also starting today: go-live tracking... Goal is everyone over 85. — Chris"
Note: the intro paragraph explaining the QA program was for the first edition only — subsequent Mondays skip straight to results.