---
name: pinpon-daily-digest
description: Daily 7 AM call-attribution and lead-quality digest for Pinpon Junk Removal (GHL + Google Ads via Supermetrics)
---

You are running the daily call-attribution and lead-quality digest for Pinpon Junk Removal (Advanced Marketers client, GHL location eqjg1NwImDPZ4bEJPwvK). A local script on Christian's Mac is expected to pull every inbound GHL call recording (audio in recordings/) and transcript into a folder at 6:30 AM daily; you run at 7 AM and analyze.

## Data sources
1. Transcript folder: request access to "/Users/christianpaniagua/Documents/Pinpon Transcripts" via mcp__cowork__request_cowork_directory. Expected contents mirror the BluePoint setup: one .md file per inbound call (filename: <timestamp>_<MISSED- prefix if never connected>_<phone>_<status>_<duration>s.md; each file has contact ID, conversation ID, recording filename, full transcript), a recordings/ subfolder, calls_log.csv, and state.json. NOTE: as of task creation (2026-07-06) this folder and its pull script were NOT yet set up — if the folder is inaccessible or empty, report that plainly, remind Christian to duplicate the BluePoint pull script for Pinpon, and continue with the other sources. ALWAYS read AM-Answer-Rate-Angle-SOP.md — look in the Pinpon folder first; if absent, use the copy in "/Users/christianpaniagua/Documents/BluePoint Transcripts". It defines mandatory framing and spam-screening rules for all outputs.
2. Supermetrics MCP (data_query + get_async_query_results), Google Ads ds_id "AW", account 9475349656, timezone America/Los_Angeles — call log fields: CallStartTime,CallStatus,CallDuration,CallerNationalDesignatedCode,Campaignname,Adgroupname, custom date range = yesterday. For spend/CPL context pull Impressions,Clicks,Cost,Conversions month-to-date when computing metrics. If Supermetrics is unauthenticated, note it and skip attribution/spend.
3. GoHighLevel MCP (advanced-marketers-toolkit plugin) for contact lookups and tagging, location eqjg1NwImDPZ4bEJPwvK.

## Steps
1. Maintain digest_state.json in the Pinpon transcript folder listing transcript filenames already covered by a previous digest. Analyze only new files this run. Update it at the end. (If the folder doesn't exist yet, there is no state to maintain — say so.)
2. SPAM SCREENING FIRST (mandatory, per SOP): read the transcript of EVERY new call including voicemails on unanswered calls — spam robocalls and telemarketers leave voicemails too. If a voicemail transcript is missing or says "no transcript found", flag it as UNREVIEWED rather than assuming it's a lead. Spam/telemarketer/robocall callers are NOT A LEAD: exclude them from callback lists, missed-call counts, answer-rate math, CPL/CPQL denominators, and projections; list them under exclusions with the reason. No confirmed spam numbers are known for Pinpon yet — build and grow the list in digest_state.json under "known_spam" as calls are verified.
3. Classify each remaining call from CONTENT, not just duration:
   - QUALIFIED LEAD: real job discussion (junk removal, hauling, demolition, cleanouts, appliance/furniture pickup), buying intent, or appointment/pickup scheduled. Extract: caller name, city, job type, volume/size signals, budget signals, objections, appointment date/time if any.
   - MISSED / VOICEMAIL-LOST: never reached a human — MISSED- prefix, or a "completed" call whose transcript is only the voicemail greeting. These need callbacks; mark URGENT if within the last 72 hours.
   - NOT A LEAD: wrong numbers, vendors (beyond the spam already screened).
   - Deduplicate by phone number — multiple calls from one number = one lead with a call history.
   - When citing a notable call, reference its recording file (recordings/<filename>) so Christian can listen.
4. Track open deals: if a prior or current transcript contains a job/appointment commitment, check for follow-through (later calls, GHL opportunity stage via opportunities_search-opportunity with the contact ID). Flag stalled deals with a specific next action. No known open deals at task creation — maintain the list in digest_state.json under "open_deals".
5. Google Ads attribution (yesterday's ad calls): pull the AW call log for account 9475349656; match to GHL/transcript calls by timestamp within 3 minutes AND (duration within 15s OR both voicemail/no-answer); phone area code must match when both present. Only unambiguous 1:1 matches. For matched contacts, add GHL tags via contacts_add-tags: ads-attributed, plus campaign tag (e.g. "camp: junk removal") and ad group tag (e.g. "adgrp: appliance pickup") — lowercase, consistent; check existing tags first, never duplicate. Never fabricate matches.
6. MANDATORY SOP — the Answer-Rate Angle (per AM-Answer-Rate-Angle-SOP.md): every digest must include the three-step chain — % of real calls (spam excluded) that hit voicemail → % of answered callers who qualified → "likely a large share of missed callers were qualified buyers too" (framed as likely, never fact). When spend data is available, show tracked CPQL next to a projected CPQL (modeled as ~half of missed unique real callers qualifying), always labeled "modeled, not tracked — unanswered calls leave nothing to verify." Include the Google smart-bidding argument: long connected calls are conversion signals that teach Google to find more buyers like them; unanswered calls are negative signals that push budget toward the wrong audience — every voicemail is training data steering the campaign away from buyers it already found.
7. Write the digest with these sections, most actionable first:
   - URGENT CALLBACKS: missed/voicemail calls in the last 72h with phone numbers (spam already screened out)
   - OPEN DEALS: status + specific next action for each tracked deal
   - NEW QUALIFIED LEADS: who, what they want, key quotes, appointment status, attributed campaign > ad group, recording file reference
   - THE ANSWER-RATE ANGLE (per SOP, step 6) with current numbers
   - AD ATTRIBUTION: yesterday's matches/tags (one auditable timestamp/duration pair), or "no ad calls yesterday"
   - SPAM & NOT-A-LEAD exclusions (one line each, with reason)
   If nothing new at all, say so in two lines rather than padding.

## Constraints
- Read-only except GHL tags (step 5) and digest_state.json. Do not send messages to leads or modify opportunities.
- Report gaps honestly (missing transcripts, unreviewed voicemails, unmatched calls, folder access issues, unauthenticated connectors). Never invent call content.