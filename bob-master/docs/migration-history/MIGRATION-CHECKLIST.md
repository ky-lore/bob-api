# Mac Mini Migration Checklist — Advanced Marketers Automations

**Migration date:** Tuesday, July 21, 2026
**From:** Christian's MacBook Pro (personal account: chris@advancedmarketers.co)
**To:** Dedicated Mac mini (team account, owned by chris@advancedmarketers.co)

---

## What's already done (backed up July 19)

All backups are in the `migration-backup` package delivered in this Claude session:

| File | What it is |
|---|---|
| `dashboards/retention-mission-control.html` | Retention Mission Control (TV dashboard) |
| `dashboards/retention-command-center.html` | Retention Command Center |
| `dashboards/negative-keyword-review.html` | Negative Keyword Review tool |
| `dashboards/churn-early-warning.html` | Churn Early Warning dashboard |
| `dashboards/winning-creatives.html` | Winning Creatives gallery |
| `dashboards/ad-optimization-live.html` | Ad Optimization Live dashboard |
| `scheduled-tasks/daily-go-live-audit-PROMPT.md` | Full prompt + schedule for the Daily Go-Live Audit |
| `scheduled-tasks/weekly-churn-early-warning-PROMPT.md` | Full prompt + schedule for the Weekly Churn scan |
| `scheduled-tasks/*.json` | Raw config backups (belt-and-suspenders) |
| `mac-automation-audit.sh` | Audit script — run on the OLD MacBook (see Phase 1) |

**Key fact:** the two scheduled tasks run in Anthropic's cloud under Chris's *personal* account — they are NOT on the MacBook and will keep firing no matter what happens to either machine. Nothing breaks on migration day. Moving them to the team account is optional (Phase 4).

---

## Phase 1 — TODAY, on the old MacBook (10 min)

1. Run the audit script to catch anything outside Cowork:
   - Move `mac-automation-audit.sh` to Downloads, open Terminal, run:
     `bash ~/Downloads/mac-automation-audit.sh`
   - A report appears on the Desktop: `mac-automation-audit.txt`
   - Attach that report to the Claude session — Claude maps every item found (cron jobs, launch agents, n8n, Shortcuts, etc.) into this plan.
2. Note anything you know runs on this Mac that isn't Claude-related (e.g., a browser that must stay open, a local n8n, GoHighLevel desktop helpers).

## Phase 2 — Mac mini setup (15 min)

1. Complete macOS setup. Since it's a dedicated automation machine:
   - System Settings → Displays/Battery → prevent sleep ("Prevent automatic sleeping when display is off").
   - System Settings → General → Sharing → give it a clear name (e.g., "AM-Automation-Mini").
   - Enable automatic login if the machine is physically secure.
2. Install the **Claude desktop app** and sign in with the **team account**.
3. Set the Claude app to open at login (right-click dock icon → Options → Open at Login).
4. Install Chrome + the Claude in Chrome extension if any automations use browser control.
5. Install Slack, and any other apps the audit report says are needed.

## Phase 3 — Re-create the 6 dashboards on the Mac mini (20–30 min)

On the Mac mini, in the Claude desktop app (team account):

1. Start a new Cowork session.
2. Attach the 6 dashboard HTML files from the backup package.
3. Say: *"Create each of these attached HTML files as a Cowork artifact, keeping the same names and IDs: retention-mission-control, retention-command-center, negative-keyword-review, churn-early-warning, winning-creatives, ad-optimization-live."*
4. **Re-grant connectors on each dashboard.** Freshly created artifacts start with no data permissions. Open each one in the Cowork sidebar and grant what it asks for:
   - Retention Mission Control / Retention Command Center → **ClickUp** (Cancel & Save Pipeline)
   - Negative Keyword Review / Churn Early Warning / Winning Creatives → **Supermetrics** (Google Ads, Meta, TikTok)
   - Ad Optimization Live → **Google Sheets/Drive** (GHL "Lead Update Meta Report" sheet)
5. Open every dashboard and confirm live data loads.

> Prerequisite: the **team account** must have these connectors authorized: ClickUp, Supermetrics, Slack, Gmail, Google Calendar, Google Drive, Zoom. Do this once in the Claude app's connector settings before step 4. Also reinstall the **Advanced Marketers Toolkit plugin** (and the marketing plugin) under the team account so the skills (client reports, churn scan, negative keyword review) are available there.

## Phase 4 — Scheduled tasks (decide, then 10 min)

Current state: both tasks keep running under Chris's personal account regardless of hardware. Two options:

- **Option A — leave them (zero work).** They keep firing from the cloud; push notifications continue to Chris's devices. Fine if Chris stays the owner.
- **Option B — move to the team account (recommended for a $160M-exit-ready org — get automations out of the founder's personal account).** In a Cowork session signed into the *team* account:
  1. Open `daily-go-live-audit-PROMPT.md`, paste the prompt, and say: *"Create this as a scheduled task, weekdays at 7:00 AM Pacific (cron 0 14 * * 1-5 UTC), named Daily Go-Live Audit (Advanced Marketers)."*
  2. Same for `weekly-churn-early-warning-PROMPT.md`: Mondays 6:00 AM Pacific (cron 0 13 * * 1 UTC), push notifications ON.
  3. Fire each once manually to verify (ask Claude to run it now), check the Slack DM/digest arrives.
  4. Only after both verify: ask Claude (in the personal-account session) to delete the two originals — IDs are in the PROMPT.md files.

## Phase 5 — Everything the audit found

For each item in `mac-automation-audit.txt`, apply one of:
- **Native app** (Keyboard Maestro, Hazel, Shortcuts): reinstall on the mini, export/import its settings (each PROMPT varies — Claude will give exact steps per app once the report is in).
- **Launch agent / cron job**: copy the underlying script + the `.plist`/crontab line to the mini, adjust paths, reload with `launchctl load`.
- **Docker / n8n**: export workflows (n8n → Settings → Download backup), reinstall Docker on the mini, re-import, re-enter credentials.

## Phase 6 — Verification & cutover (Tuesday end of day)

- [ ] All 6 dashboards open on the Mac mini and show live data
- [ ] Team can access/see what they need (share dashboards as needed)
- [ ] Scheduled tasks fired at least once from their new home, digests landed in Slack
- [ ] Audit-report items each have a home on the mini
- [ ] On the old MacBook: quit/disable automations — **don't delete anything for 1–2 weeks** (rollback safety)
- [ ] Calendar reminder for ~Aug 4: if the mini has run clean for two weeks, wipe the old automations off the MacBook

## Phase 7 — Free data stack pilot (this week, after migration)

Goal: replace the Supermetrics contract (~$15–25k/yr at your scale) with the official, free Google Ads and Meta MCP servers. Full details in `FREE-DATA-STACK-PILOT.md`. Summary:

1. **Today (before migration):** apply for the Google Ads developer token in the MCC's API Center (1–3 day approval — longest lead item) and set up the Google Cloud OAuth credentials.
2. **After the mini is live:** install Google's official Ads MCP server on the Mac mini; add Meta's hosted connector (mcp.facebook.com/ads) to the team account.
3. **Claude rebuilds** the client-report and churn-scan skills as v2 copies running on the free stack; originals stay as the control.
4. **2-week parallel run** on 3 test clients — numbers must match Supermetrics.
5. **Decision at Supermetrics renewal:** cancel/downsize if the pilot passes. Reads (reports, dashboards, scans) are fully covered; only the negative-keyword write path needs a small approval-gated script.

---

## Gotchas worth knowing

- **Artifacts don't sync between machines.** The 6 dashboards exist only on the Mac they were created on — that's why we exported the HTML. Any future dashboard edits should happen on the mini.
- **Connector grants are per-artifact and reset on re-creation.** Expect every dashboard to ask for permissions again the first time it's opened on the mini.
- **The team account needs its own connector authorizations** (ClickUp, Supermetrics, Google, Slack, Zoom, Gmail). Budget 10 minutes of OAuth clicking.
- **Cron times in the backups are UTC.** 0 14 = 7:00 AM Pacific (PDT). If a task is re-created in winter, re-check the hour (PST is UTC-8).
- **GoHighLevel**: if the GHL connector gets added to the team account, the go-live audit prompt already knows how to use it (it checks for it conditionally).
