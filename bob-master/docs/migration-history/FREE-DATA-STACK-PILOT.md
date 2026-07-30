# Free Data Stack Pilot — Replacing Supermetrics with Official Ads MCPs

**Goal:** run client reporting through Google's and Meta's official (free) MCP servers in parallel with Supermetrics for 2 weeks. If the numbers match, downsize or cancel Supermetrics at renewal (~$15–25k/yr saved).

**What stays true either way:** reports, churn scans, and dashboards are all *reads* — fully covered by the free stack. The only write (negative-keyword submission) needs its own path later (small approval-gated script, or done in the Ads UI).

---

## ✅ STATUS UPDATE (July 19, verified live in Chris's accounts)

Steps 1 and 2 are ALREADY DONE — Chris had completed them on July 17:

- Developer token exists with **Basic Access approved** (Google Ads API Center, MCC 391-098-1944) — no waiting period.
- Cloud project **"Google Ads API"** (ID: `analog-daylight-502800-e6`, org advancedmarketers.co) with the Google Ads API enabled.
- OAuth desktop client **"am-audit"** created.
- Consent screen switched from External/Testing to **Internal** on July 19 (removes the 7-day token expiry that would have broken automated reports weekly).
- Note: an empty duplicate project `am-ads-reporting` was created July 19 and is unused — safe to shut down or ignore.

**Only remaining setup (on the Mac mini, migration day):** download the `am-audit` OAuth client JSON from the Credentials page, click "View token" in the Ads API Center for the developer token, and plug both into the MCP server install (Step 3 below). Steps 1–2 below are kept for reference only.

## Step 1 — Apply for the Google Ads developer token (DONE — for reference only)

Takes 10 minutes to apply; approval usually 1–3 business days. Free.

1. Sign in to your **manager account (MCC)** at ads.google.com — the same MCC that holds the ~250 client accounts. Must be done from the MCC, not a client account.
2. Go to **Tools & Settings (wrench icon) → Setup → API Center**. (If you don't see API Center, you're in a client account — switch to the manager account.)
3. Fill in the form: API contact email (use an email you check — Google sends compliance mail there), company name **Advanced Marketers**, company URL, accept the terms. You get a **test-access token instantly**.
4. On the same page, click **Apply for Basic access**. In the application, describe the use case as:
   > "Internal reporting tool for our agency's own managed client accounts (MCC). Read-only reporting: campaign performance, search terms, and Local Services leads, surfaced to our team via an internal AI assistant. No third-party access, no ad management by external parties."
5. Basic access = 15,000 API operations/day — far more than daily reporting across ~180 accounts needs.

**Note your Developer Token** (22 characters, shown in API Center) — it goes into the server config in Step 3.

## Step 2 — Google Cloud project + OAuth credentials (15 min, free)

1. Go to console.cloud.google.com (logged in as chris@advancedmarketers.co) → create project **am-ads-reporting**.
2. **APIs & Services → Library** → search "Google Ads API" → Enable.
3. **APIs & Services → OAuth consent screen** → if advancedmarketers.co is on Google Workspace choose **Internal** (much simpler); otherwise External + add yourself and report-runners as test users.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app** → name it "am-ads-mcp" → download the client secret JSON.

## Step 3 — Install Google's Ads MCP server on the Mac mini (after migration day)

Google's official server: https://github.com/googleads/google-ads-mcp (open source, read-only by design).

On the Mac mini, in Terminal — Claude can drive this step in a Cowork session:

1. Install: follow the repo README (Python-based; `pip install` or clone + run).
2. Configure with: the developer token (Step 1), OAuth client ID/secret (Step 2), and `login_customer_id` = your MCC ID (no dashes).
3. First run opens a browser for OAuth consent — approve with the Google login that has MCC access; it stores a refresh token.
4. Add it to the Claude desktop app on the mini as a local MCP server (stdio). Test with: *"List accessible Google Ads customers"* — you should see the client accounts.
5. **LSA check:** run a test GAQL query on `local_services_lead` for one LSA account to confirm the ~40 LSA accounts report through this path.

**Team-wide later (optional):** if the pilot succeeds and the whole team should query Google Ads directly, deploy the same server to Google Cloud Run (HTTP transport) and add it as a custom connector on the team org — one server, everyone inherits it, access still gated by Google OAuth.

## Step 4 — Connect Meta's hosted Ads MCP (5 min, no hosting)

1. Claude → Settings → Connectors → **Add custom connector** → URL: `https://mcp.facebook.com/ads`.
2. Authenticate with your **Meta Business** login. Permissions mirror your Business Manager roles — team members who connect it get only what their Meta role allows (this is the guardrail).
3. Test: *"List my Meta ad accounts"* — expect the ~62 accounts.
4. Note: currently beta; pricing under determination. Write actions create campaigns paused-by-default (safety net), but for this pilot we use reads only.

## Step 5 — Rebuild the report skill against the free stack (Claude does this)

In a Cowork session, ask Claude to:

1. Copy `am-client-report` to a new skill **am-client-report-v2** that pulls Google Ads + LSA via the Google Ads MCP and Meta via the hosted connector, keeping the identical PDF output format.
2. Same for the churn early-warning skill and the Winning Creatives dashboard data source.
3. Keep the originals untouched — they're the control group.

## Step 6 — Parallel run (2 weeks)

- [ ] Pick 3 representative clients (1 Google-heavy, 1 Meta-heavy, 1 LSA).
- [ ] Week 1: generate each client's report with BOTH skills. Compare spend, leads, CPL/CPQL — must match within rounding/attribution-window differences.
- [ ] Week 2: run the Monday churn scan on the new stack; refresh Winning Creatives from the new stack.
- [ ] Log any gaps (missing metric, LSA quirk, rate limit) — Claude patches the v2 skills.

## Step 7 — Decision & cutover

**If numbers match:** switch the plugin's default skills to v2, repoint dashboards, then at Supermetrics renewal either cancel, or keep a minimal plan only if something proved irreplaceable. Budget the negative-keyword write path: approval-gated script via the Google Ads API (Claude builds it; you approve each submission batch).

**If gaps exist:** keep Supermetrics for what gapped, negotiate seats/sources down to just that (the fallback still likely cuts the bill in half).

---

## Cost comparison

| | Supermetrics (10 users, quoted) | Free stack |
|---|---|---|
| Licensing | ~$15–25k/yr | $0 (Google) + $0-beta (Meta) |
| Setup | none | ~1 day spread over a week (token wait) |
| Maintenance | none | occasional server updates (Claude handles) |
| Write access (neg. keywords) | included | needs small custom script |
| Field normalization | included | Claude does it in-skill |

## Timeline

- **Today:** Step 1 (token application — starts the clock) + Step 2.
- **Tomorrow:** Mac mini migration as planned (Phases 1–6, unchanged, still on Supermetrics).
- **This week, when token approves:** Steps 3–5 on the mini.
- **Next 2 weeks:** Step 6 parallel run.
- **At Supermetrics renewal:** Step 7 decision.
