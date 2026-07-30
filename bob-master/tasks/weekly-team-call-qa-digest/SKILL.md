---
name: weekly-team-call-qa-digest
description: Friday QA digest of the week's team and client Zoom calls (full transcripts) for upper management
---

You are preparing a weekly leadership QA digest for Chris Paniagua (chris@advancedmarketers.co), founder/owner of Advanced Marketers, a marketing agency in Orange County CA (timezone America/Los_Angeles). Team leads: Kyle (kyle@), Tim (tim@), Mak (makayla@), Jaime Falcon — Head of Operations (jaime@), Johnny (johnny@), Kate (kate@), Nathan (nathan@), all @advancedmarketers.co.

Objective: review the week's Zoom calls across the whole team using FULL TRANSCRIPTS pulled via the Zoom API, and surface what upper management should know — internal feedback and quality assurance, framed as coaching material.

== HOW TO GET TRANSCRIPTS (Zoom server-to-server API via bash) ==
Credentials are in zoom-credentials.txt in this task's own folder (same directory as this SKILL.md — readable via the task folder mount). NEVER print the CLIENT_SECRET or any token in output or reports.

1. Get an access token (expires after 1 hour; request a fresh one if a later call returns 401):
   POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=$ACCOUNT_ID with header "Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)".
2. For each team member email above (including chris@): GET https://api.zoom.us/v2/users/{email}/recordings?from=YYYY-MM-DD&to=YYYY-MM-DD&page_size=300 with "Authorization: Bearer $TOKEN" (from = previous Saturday, to = today; handle next_page_token; skip users that return errors).
3. For each meeting with a recording_files entry of file_type "TRANSCRIPT":
   - Double-URL-encode the meeting uuid, then GET https://api.zoom.us/v2/meetings/{double_encoded_uuid}/recordings?include_fields=download_access_token&ttl=3600
   - Download the transcript: GET {transcript file download_url}?access_token={download_access_token} — returns WEBVTT text with speaker names and timestamps.
   - Save VTT files to a working directory, one per meeting, named date_host_topic.
4. List meetings without transcripts by topic/host at the end of the digest.

== ANALYSIS ==
Read the transcripts (process in batches; delegate bulk reading to a subagent if volume is large) and produce a digest with these sections, citing meeting topic + date + speaker for every item:
1. CHURN RISK: frustrated clients, unresolved complaints, missed deliverables, payment problems, cancellation or pause talk, declining results
2. CLIENT COMMITMENTS: significant promises made to clients (deliverables, dates, pricing, refunds, scope changes)
3. INTERNAL TEAM MEETINGS: how the Monday "Team Leads Weekly" (host Kyle) and other internal meetings went — decisions made, KPIs reported concretely vs vaguely, whether prior week's action items were followed up, blockers raised
4. LEADERSHIP OBSERVATIONS: a short factual section on how team leads ran their calls this week, with particular attention to Jaime Falcon (HOO) — client handling, follow-through on commitments, people management. Behavior-based with short verbatim quotes; describe actions and words, never personality judgments.
5. PROCESS GAPS: problems repeating across calls (tracking, access, SOPs, scope creep, handoffs)
6. COACHING MOMENTS: notable wins or misses in call handling, with 1-2 line verbatim examples
7. WINS: closed deals, upsells, strong client praise

== OUTPUT ==
1. Create one ClickUp task in list "Chris - Action Items" (list_id 901417226802) named "Weekly QA digest — week of [date]" with the full digest as markdown_description, assignees ["me"], due the following Monday, priority normal. Check first that a digest task for this week doesn't already exist (clickup_filter_tasks); if it does, add the digest as a comment instead.
2. End the run with a concise report: top 3 things leadership should act on, the ClickUp link, number of calls reviewed, and calls skipped.

Constraints: be factual; quote accurately; focus on accounts, processes, and behaviors. Do not modify calendars, send messages, or create any other tasks. Never expose credentials. The team should be aware this QA program exists — note that reminder in the digest footer.