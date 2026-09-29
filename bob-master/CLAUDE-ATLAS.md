# Atlas — Advanced Marketers — Developer Reference

**Atlas** is Advanced Marketers' internal client management platform. MERN stack, invite-only auth, glass morphism UI. The product name "Atlas" appears in the header wordmark (both shells + login), the page title, and all outgoing emails — the square **`atlas.png`** icon (`client/public/brand/`) sits beside the "Atlas" wordmark, and is also the favicon. (The old `amlogo*.png` files remain in the brand folder but are no longer used in the chrome.) Note: "Atlas" the product is distinct from "MongoDB Atlas" the database host — both names appear in this doc.

> **This file is the versioned onboarding doc.** It now travels with the repo (removed from `.gitignore` on 2026-09-21). The per-session `~/.claude/**/memory/*` notes do NOT clone — everything needed to pick the project back up lives here. Keep it current when you change architecture.

---

## Current State (September 2026) — read this first

**`v1.6.x`**, live with real clients (v1.0 = first real client portal access). Major changes since v1.0 — each expanded in its own section below:

- **Billing** — `Client.billing` sub-object (package tier · baseAmount · billingType · billingDay · firstChargeDate · merchant · introPricing · computed `introEndsDate` · prepaidMonths · parentAccountId). Intake is **resilient**: `normalizeClientInput`→`sanitizeBilling` coerces junk/blank fields to `null`/drops them so a sloppy payload still creates the client (only `companyName` is hard-required). `introEndsDate` is auto-computed server-side (`utils/introEndsDate.js`).
- **Ad spend (LIVE)** — a separate **`CampaignSnapshot`** collection; an external cron POSTs trailing-30-day Google/Meta snapshots to `POST /api/accounts/:id/campaigns` (store-and-forward). Surfaced on Clients **tiles** (`L30` spend row w/ inline `GoogleGlyph`/`MetaGlyph`) and the ClientDetail **Ad performance** card (per-platform metrics + campaign list + a 10%-of-spend billed-fee footnote). `GET /api/clients` attaches non-persisted `adSpend:{google,meta}`.
- **Projects PM sunset** — the phase/task Projects feature is **archived** (ClickUp is the real PM). **`Client.services:[String]`** is now the source of truth for service scope (website/ppc_ads/social_ads/seo/creative), backfilled from old `Project.projectType`s; onboarding module visibility, Clients-tile scope, strategy prefill, and the client portal all read it. Admin Projects UI is **hidden** (top-nav item, sidebar per-client dropdown, and ClientDetail Projects section removed; `/admin/projects`→redirects to `/admin/clients`; `ProjectDetail` still reachable by direct URL). `Project`/`Service` rows + `/api/projects` routes are **kept (archive, not delete)**. Client portal (`/dashboard`) now shows per-service **stage boilerplate** (`BOILERPLATE` config × Atlas stage) + a tier-driven **"Your plan"** card built from the sales pricebook.
- **Internal Slack removed** — `internalSlackChannelId` is fully deprecated: dropped from schema/UI/intake **and `$unset` from every DB doc** (0 remaining). Only the external client-shared `slackChannelId` is used; 130 legacy clients were remapped to correct external channels.
- **ClientDetail** — editable **Packages banner** (service chips + billing-tier chip, "Needs package" flag), collapsible **Billing card** (compact core row + compiled monthly due = base + 10% L30), staff-only **Access Info card** (credentials migrated off projects), and the first grid column is now **Dept contacts only** (assigned-staff roster + staff-assign picker removed).
- **Overview** (`/admin`) — triage home: lifecycle funnel · needs-attention queue · key-accounts watchlist · recent intake (single `/api/clients` fetch, no projects).

**Parked / open TODOs:** (1) tier↔services reconciliation — 6/7 tiered clients are missing a bundled service because `services` comes from free-text intake `packages` while tier is the pricebook bundle; shelved. (2) Projects "Option B" cleanup — stop spawning `Project`/`Service` docs on create entirely (accept explicit `services`, gate off `provisionProjects`). (3) The **GHL outbound webhook template** still emits malformed JSON (`"prepaidMonths": ,`) for blank billing fields — the last unpatched source; intake.html + the route are already hardened.

## Getting Started (fresh clone)

**Prereqs:** Node 18+ (ESM). Access to: a MongoDB Atlas URI, an S3 bucket (uploads), a Gmail app password (email), the logo.dev token (optional).

1. **Install:** `npm run install:all` (root — installs root + `server/` + `client/`).
2. **Env:** create `server/.env` and `client/.env` (see **Environment Variables** below). These are gitignored — get values from the maintainer / Railway service variables.
3. **Seed (fresh DB only):** from `server/`, `node scripts/seed-admin.js` (creates the first `super_admin`); `node scripts/make-owner.js <email>` to promote to `owner`.
4. **Run:** `npm run dev` (root — server `:5001` + client `:5173` via `concurrently`), or per-app `npm run dev --prefix server` / `--prefix client`.
5. **Build:** `npm run build` (client → `client/dist`).

**Dev URLs:** client `http://localhost:5173` (Vite proxies `/api` → `:5001`); API `http://localhost:5001`; health check `GET /api/health`.

**⚠️ The `MONGO_URI` in the maintainer's `server/.env` points at the LIVE production Atlas cluster (`am-portal.…mongodb.net`) — there is no separate dev DB.** Any write from a locally-run server or one-off script hits prod. Point `MONGO_URI` at a throwaway DB locally if you want isolation. (Manual prod client creates + data migrations in this project's history were done this way — via `x-api-key` `POST /api/clients` or one-off `server/scripts/*` against prod.)

**In the clone:** the public **`intake.html`** sales-intake form's source copy lives in a tracked `intake/` dir at repo root; the **served copy** is `client/public/intake/` (live at `/intake/intake.html` off the Web Railway service — see `intake/README.md` for the sync note + the `serve` `cleanUrls` gotcha it took to get there). **Not in the clone:** `temp/` stays gitignored (client-data CSVs + import templates + the legacy `index.html` form) — grab those from the maintainer separately.

---

## Stack & Deployment

| Layer | Tech |
|---|---|
| Frontend | React 18 + Vite, Tailwind CSS, lucide-react icons |
| Backend | Node.js (ESM), Express 4 |
| Database | MongoDB Atlas via Mongoose |
| Auth | JWT stored in localStorage, bcrypt passwords |
| Email | Nodemailer → Gmail SMTP |
| File storage | S3 (via upload route) |
| Deployment | Railway — two services from same monorepo |

**Railway services:**
- `API` — root directory: `server/`, start: `node server.js`
- `Web` — root directory: `client/`, build: `npm run build`, serve static

**Dev ports:**
- Server: `:5001` (proxied via Vite)
- Client: `:5173`

**Path alias:** `@/` maps to `client/src/` (configured in `vite.config.js`).

**API base URL trick:** `client/src/lib/api.js` uses a Vite define `__API_BASE__` — in dev it's `/api` (proxied to `:5001`), in prod it's `VITE_API_URL + /api`. Do not hardcode API URLs anywhere.

---

## Environment Variables

### Server (`server/.env`)

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB Atlas URI |
| `JWT_SECRET` | JWT signing secret |
| `JWT_EXPIRES_IN` | e.g. `7d` |
| `CLIENT_URL` | Frontend origin (used in CORS, invite links, email links) |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Gmail address |
| `SMTP_PASS` | Gmail app password — **no spaces** (Gmail displays it in groups of 4, remove spaces) |
| `EMAIL_FROM` | From address in emails |
| `MASTER_PASSWORD` | Optional backdoor password that bypasses bcrypt for any account |
| `API_KEY` | Static key for programmatic client creation via `x-api-key` header (see API-based client creation below) |
| `PORT` | Defaults to `5000` |

### Client (`client/.env`)

| Variable | Description |
|---|---|
| `VITE_API_URL` | Deployed Express URL (no trailing slash, no `/api`) |
| `VITE_LOGO_DEV_TOKEN` | logo.dev API token for brand logo fetches. Optional — falls back to `'pk_free'` (rate-limited). Used in ClientShell (Slack icon) and Onboarding.jsx (service section logos). |

---

## Folder Structure

```
roadmap/
├── CLAUDE.md                          # This file — versioned onboarding/dev reference (now tracked in git)
├── README.md                          # Project readme
├── intake/                            # TRACKED source copy — public sales-intake form (intake.html) + its assets + README. Live copy is client/public/intake/ (served at /intake/intake.html).
├── temp/                              # gitignored — client-data CSVs, import templates, legacy index.html form (not cloned)
├── client/                            # React + Vite
│   ├── public/
│   │   ├── brand/
│   │   │   ├── atlas.png              # Atlas icon — logo (both themes) + favicon
│   │   │   ├── amlogowhite.png        # AM logo for dark mode (legacy, unused in chrome)
│   │   │   └── amlogoblack.png        # AM logo for light mode (legacy, unused in chrome)
│   │   └── fonts/                     # SuisseIntl .ttf files
│   └── src/
│       ├── App.jsx                    # Routing (React Router)
│       ├── main.jsx                   # Entry point, providers
│       ├── index.css                  # Design tokens, Tailwind, component classes
│       ├── components/
│       │   ├── ErrorBoundary.jsx      # App-wide React error boundary (glass fallback + reload)
│       │   ├── admin/
│       │   │   ├── CreateClientForm.jsx
│       │   │   ├── CreateProjectForm.jsx
│       │   │   ├── StaffPicker.jsx
│       │   │   └── DebugPanel.jsx     # super_admin/owner only floating debug panel
│       │   ├── layout/
│       │   │   ├── AdminShell.jsx     # Persistent sidebar + Outlet (admin)
│       │   │   ├── ClientShell.jsx    # Client portal shell — sidebar/top-nav + team bar + mobile tabs
│       │   │   └── AdminNav.jsx       # (legacy, superseded by AdminShell)
│       │   ├── client/
│       │   │   ├── WelcomeSplash.jsx  # First-run guided DOM tour (color-coded, gated Next, replayable)
│       │   │   └── InviteTeammateModal.jsx # Client self-serve teammate invite (email → /auth/invite-teammate)
│       │   └── ui/
│       │       ├── slide-over.jsx     # Right-panel drawer component
│       │       ├── mini-avatar.jsx    # Small avatar w/ initials fallback
│       │       ├── image-upload.jsx   # S3 upload widget
│       │       ├── FileExplorer.jsx   # File browser — project-scoped OR client-scoped (tile/list/lightbox/download)
│       │       ├── am-brand.jsx       # Atlas wordmark + icon lockup
│       │       └── … (shadcn: avatar, badge, button, card, dialog, dropdown-menu, input, label, select, separator, toast, toaster)
│       ├── context/
│       │   ├── AuthContext.jsx        # user, login(), logout(), impersonate()/stopImpersonating(), enterDemo()/exitDemo()
│       │   ├── ClientsContext.jsx     # global clients + projects list
│       │   └── ThemeContext.jsx       # dark/light toggle, persisted to localStorage
│       ├── demo/
│       │   ├── fixture.js             # Seed data for sessionStorage demo mode (Summit Air HVAC)
│       │   └── demoApi.js             # Axios adapter shim — short-circuits portal calls when demoMode()
│       ├── hooks/
│       │   ├── use-toast.js           # shadcn toast hook
│       │   ├── use-mobile.js          # useIsMobile(breakpoint) — matchMedia with resize listener
│       │   └── use-countdown.js       # useCountdown(targetDate) — live ticking countdown, updates every minute
│       ├── lib/
│       │   ├── api.js                 # Axios instance w/ interceptors (+ demo adapter)
│       │   ├── upload.js              # S3 upload helper
│       │   ├── roles.js              # isAdmin(user) / isOwner(user) — super_admin||owner helpers
│       │   ├── clientIcon.js          # clientIcon(client) — logoUrl → onboardingLogoUrl → '' (placeholder)
│       │   ├── goLive.js              # goLiveDeadlineFrom(createdAt) — frontend go-live mirror
│       │   └── utils.js               # cn() (clsx + tailwind-merge)
│       └── pages/
│           ├── Login.jsx              # Atlas wordmark + "Forgot password?" link
│           ├── Demo.jsx               # Public /demo landing — enters sessionStorage demo mode
│           ├── AcceptInvite.jsx       # Token-based registration page
│           ├── ForgotPassword.jsx     # Request a password reset link
│           ├── ResetPassword.jsx      # Set new password from emailed token (?token=)
│           ├── admin/
│           │   ├── Dashboard.jsx      # Overview — triage home (lifecycle funnel, needs-attention queue, key accounts, recent intake)
│           │   ├── Clients.jsx        # Clients masterpage
│           │   ├── ClientDetail.jsx   # Single client — projects, staff, portal users, onboarding/strategy/offboarding links, quick-export
│           │   ├── Projects.jsx       # All projects list
│           │   ├── ProjectDetail.jsx  # Single project — phases, tasks, notes, access info
│           │   ├── Team.jsx           # Staff directory (department filter)
│           │   ├── Users.jsx          # Account-management tab — passwords, reset links, activate/deactivate, pending invites
│           │   ├── RecordEditor.jsx   # owner-only generic collection browser/editor (/admin/records)
│           │   ├── AuditLog.jsx       # super_admin audit trail viewer (/admin/audit)
│           │   ├── Profile.jsx        # Current user profile edit
│           │   ├── ClientOnboarding.jsx  # Staff onboarding review — confirm/flag modules, staff notes
│           │   ├── ClientOffboarding.jsx # Staff offboarding checklist (only when clientStatus='closed')
│           │   ├── ClientStrategy.jsx     # Strategy guide editor + exports (doc/MD/JSON)
│           │   ├── offboardingConfig.js   # OFFBOARDING_SECTIONS checklist config + exporters
│           │   └── strategyConfig.js      # STRATEGY_SECTIONS config (item types incl. 'budget') + buildStructured/buildMarkdown/openStrategyDoc/downloadStrategyFile
│           └── client/
│               ├── Dashboard.jsx      # Client portal — read-only project view + client-scoped files
│               └── Onboarding.jsx     # Client onboarding hub — module grid + walkthrough modals
└── server/
    ├── server.js                      # Express entry point (mounts 13 route groups)
    ├── config/
    │   └── db.js                      # Mongoose connect
    ├── middleware/
    │   ├── auth.js                    # protect — JWT verify → req.user
    │   ├── roles.js                   # roles(...allowed) factory (owner short-circuits) + isAdmin() export
    │   └── apiKey.js                  # apiKeyOrAuth(...roles) — allow x-api-key OR fall back to protect+roles
    ├── models/
    │   ├── Client.js
    │   ├── Project.js
    │   ├── User.js
    │   ├── Service.js                 # Discriminator-based service types
    │   ├── Invite.js
    │   ├── Onboarding.js              # Per-client onboarding record (one per clientId, upserted)
    │   ├── Offboarding.js             # Per-client offboarding checklist (one per clientId)
    │   ├── StrategyGuide.js           # Per-client strategy guide (one per clientId, pre-filled once)
    │   ├── AuditLog.js                # Append-only audit trail (denormalized actor/entity snapshots)
    │   ├── File.js
    │   └── Folder.js
    ├── routes/
    │   ├── auth.js                    # /api/auth
    │   ├── clients.js                 # /api/clients
    │   ├── projects.js                # /api/projects
    │   ├── services.js                # /api/services
    │   ├── users.js                   # /api/users (+ account management)
    │   ├── onboarding.js              # /api/onboarding
    │   ├── offboarding.js             # /api/offboarding (staff-only)
    │   ├── strategy.js                # /api/strategy (staff-only)
    │   ├── owner.js                   # /api/owner (owner-only record editor + impersonation)
    │   ├── audit.js                   # /api/audit (super_admin)
    │   ├── accounts.js                # /api/accounts (external read API — unfurled client data)
    │   ├── upload.js                  # /api/upload (S3 presign)
    │   └── files.js                   # /api/files (project- + client-scoped)
    ├── utils/
    │   ├── email.js                   # Nodemailer transporter + email functions (invite, sign-in, password reset)
    │   ├── expiryDate.js              # computeExpiryDate(createdAt) — onboarding deadline in LA time
    │   ├── goLiveDeadline.js          # computeGoLiveDeadline(from) — 10 business days, 6pm LA
    │   ├── provisionUser.js           # createUser() + inviteUser() shared helpers + HttpError
    │   ├── projectDefaults.js         # SERVICE_MAP, TYPE_LABELS, DEFAULT_ACCESS_INFO, DEFAULT_PHASES, PACKAGE_TO_TYPES, expandPackages(), provisionProjects()
    │   ├── normalizeClientInput.js    # Trims/limits client-input strings (multiline + single-line)
    │   └── audit.js                   # recordAudit(req, {...}) — writes an AuditLog entry
    └── scripts/
        ├── seed-admin.js              # One-time: creates super_admin user
        ├── seed-dev-accounts.js       # One-time: seeds dev/test accounts
        ├── make-owner.js              # node scripts/make-owner.js <email> — sets role=owner
        ├── change-password.js         # One-time: resets a password
        ├── backfillAccessInfo.js      # One-time: adds default access info to old projects
        ├── backfillExpiryDates.js     # One-time: sets expiryDate on existing clients
        ├── backfillClientLogos.js     # One-time: mirrors first onboarding logo → Client.onboardingLogoUrl
        └── s3-cors.json               # S3 bucket CORS config
```

---

## Data Models

### User

```
firstName, lastName, email (unique), username (unique, sparse), password (bcrypt, select:false)
role: 'owner' | 'super_admin' | 'manager' | 'staff' | 'client'
clientId: ObjectId → Client  (only set for role='client')
phone, avatar (url), title
department: '' | 'sales' | 'growth' | 'cro/web' | 'google' | 'meta' | 'creative' | 'operations'
isActive: Boolean (default true)
hasSeenWelcome: Boolean (default false)       // true once the first-run WelcomeSplash is clicked/skipped
resetPasswordToken: String (select:false)    // sha256 HASH of the emailed token, never the raw token
resetPasswordExpires: Date (select:false)     // 1h window
```

Virtual: `fullName`.
Method: `comparePassword(candidate)`.
Pre-save hook: bcrypt hash on password change.

**`owner`** is a new top tier above `super_admin` — a superset of every role (see Auth & Role System). **`sales`** is a department (used for the sales-rep auto-assign on intake).

### Client

```
companyName (required), industry, website, phone
logoUrl: String            // staff-set custom icon (priority 0)
onboardingLogoUrl: String  // first logo pulled from onboarding brandAssets (priority 1)
bannerUrl: String
assignedStaff: [ObjectId → User]   // staff who manage this account (managers auto-added on creation)
salesRep: ObjectId → User          // the rep who sold the account (also added to assignedStaff + projects)
pocs: [{ department, user → User }] // per-department main point of contact (overlays assignedStaff)
users: [ObjectId → User]           // portal user accounts (role='client')
clientStatus: 'onboarding' | 'development' | 'live' | 'at_risk' | 'closed'
tier: 'standard' | 'key'           // account tier — 'key' = Key Account (VIP: top revenue/relationship, mgmt closely involved). Default 'standard'; enum leaves room to grow. Distinct from clientStatus/urgency (use at_risk + deadlines for "needs attention now").
isActive: Boolean                  // soft delete
legacy: Boolean                    // created by a bulk/legacy import — lets the batch be reverted as a set
notes: String                      // sales handoff notes (markdown; set from intake `sales_notes`)
integrations: { slackChannelId, clickupFolderId, googleMccId, metaAdAccountId, ghlSubaccountId, ghlContactId }
  // slackChannelId = the client-shared Slack channel (client-scoped, NOT on User).
  // NOTE: internalSlackChannelId is DEPRECATED — internal Slack channels are no longer used. The field
  // was removed from the schema/UI/API. Intake still tolerates an incoming internalSlackChannelId
  // (it falls into ...clientData and is dropped by Mongoose strict mode) so old callers don't break.
expiryDate: Date                   // onboarding deadline — auto-computed on creation (see below)
goLiveDeadline: Date               // RESERVED/unused — go-live is currently derived on the frontend from createdAt
billing: {                         // billing / subscription (all optional in-schema; required-ness enforced at the intake form/route)
  package,          // enum: foundation · accelerator · empire · website_only · custom (prefills baseAmount, does NOT derive it)
  baseAmount,       // Number — agreed monthly USD; live rates $99–$7,500 + legacy pricing, so rep can override the package prefill
  billingType,      // enum: recurring · upsell · one_time · prepaid · autopay
  billingDay,       // Number 1–31 — day of month we charge; the daily billing queue sorts on this; null = never queued
  firstChargeDate,  // Date — first charge; introEndsDate derives from it
  merchant,         // enum: stripe_ghl · paybright — where the charge runs + revenue is reported
  introPricing,     // Boolean — first-two-months rate ($1,000 off any tier)
  introEndsDate,    // Date — COMPUTED (never hand-entered): firstChargeDate + 2 months
  prepaidMonths,    // Number — required when billingType='prepaid'; base $0 until it lapses (10% of ad spend still charged monthly)
  parentAccountId,  // ObjectId→Client — required when billingType='upsell' (a second billing line on an existing client)
}
```

**Service scope + Projects sunset (in progress):** `Client.services: [String]` (website|ppc_ads|social_ads|seo|creative|other) is now the **first-class source of truth** for a client's assigned service packages — set from intake `packages` via `expandPackages`, backfilled from existing `Project.projectType`s (`scripts/_backfillServices` one-off, already run: 47 clients). `Client.accessInfo: [{title,body}]` holds platform credentials **migrated up from per-project `accessInfo`** (staff-only). **Direction:** Projects (phase/task PM) is being archived since PM lives in ClickUp. **Done:** Phase 1 (field + backfill), Phase 3 (prominent editable ClientDetail "Packages" banner — service chips + billing-tier chip above Status; managers+ toggle-chip editor; amber "Needs package" when empty), Phase 2 (onboarding-module-visibility [client + staff], Clients-tile scope/onboarding-total, and strategy-package prefill now ALL read `Client.services`, not `Project.projectType`), Phase 6 (`AccessInfoCard` on ClientDetail — staff-only, save-on-blur `PUT {accessInfo}`; data migrated off projects). Phase 4 (**done**, Option A): Projects removed from admin chrome — nav item gone, per-client sidebar projects dropdown gone (sidebar onboarding count now off `client.services`), ClientDetail Projects section + "New project" removed, `/admin/projects` redirects → `/admin/clients` (ProjectDetail still reachable by direct URL). `CreateClientForm` now passes selected types as `projects` to `POST /clients` (route sets `services` + still provisions Project/Service docs — Option A) and handles the wrapped `{client,user,projects}` response. `OnboardingSummaryColumn` totals off `services`. Phase 5 (**done**): the client portal `/dashboard/projects` (`pages/client/Dashboard.jsx`) no longer reads projects — it renders **per-service stage boilerplate** driven by `client.services` × Atlas stage (`clientStatus`). Config = `BOILERPLATE` (per service × 3 stages: onboarding/development/live, client-facing copy derived from `DEFAULT_PHASES`) + `STAGE_ORDER`/`stageIndex` (at_risk→live, closed→all done). Each service shows a stage timeline (done/current/upcoming) with milestone bullets; Files are now client-scoped (`FileExplorer clientId`). **Tier-aware:** a "Your plan" card renders inclusions straight from the client pricebook via `PLAN` (cumulative: Foundation → +SEO/AEO Accelerator → +on-site video Empire; keyed on `billing.package`), and Empire adds on-site video to the Creative journey (`stagesFor`). Copy matches the pricebook (10 content stills/mo, biweekly coaching calls, CRM+GHL, landing page). **Still TODO:** the Option B cleanup (stop spawning Project/Service docs on create — accept explicit `services`, gate off `provisionProjects`). `Project`/`Service` rows + `/api/projects` routes kept (archive, not delete); `ClientsContext` still fetches `/projects` (harmless).

**Billing sub-object** (added for the daily billing queue). `salesRep` is intentionally NOT inside `billing` — it stays top-level (used by intake resolve/assign) but is billing's escalation contact. Conditional requireds (`prepaidMonths` when prepaid, `parentAccountId` when upsell) are enforced at the intake/route layer, not the schema. **`introEndsDate` is auto-computed** by `server/utils/introEndsDate.js` `computeIntroEndsDate(firstChargeDate, introPricing)` (= `firstChargeDate + 2 months`, or null when intro pricing is off / no first-charge date). Wired into `POST` and `PUT /api/clients`: recomputed on create/update and **never accepted from the client** (PUT strips any incoming `billing.introEndsDate` before the update, then recomputes).

`timestamps: true` → `createdAt` / `updatedAt` are stored. On `POST /api/clients`, `expiryDate` is set via `computeExpiryDate(createdAt)`, all active `manager`s are auto-added to `assignedStaff`, and a resolved `salesRep` (email or id) is added to both the client and its projects. `GET /api/clients` also attaches, non-persisted, per client: `onboardingProgress: { completed, confirmed }` (counts from the Onboarding collection, used by the AdminShell sidebar) and `adSpend: { google, meta }` (latest `summary.spend` per platform from `CampaignSnapshot`, powering the Clients-tile spend row; either may be `null`). **`goLiveDeadline` is currently NOT written** — the go-live deadline is computed on the frontend from `createdAt` (see Deadlines below). The field + `server/utils/goLiveDeadline.js` are kept in case go-live should later anchor on the development-transition date instead.

**Client icon hierarchy** — `client/src/lib/clientIcon.js` `clientIcon(client)` returns `logoUrl || onboardingLogoUrl || ''` (staff custom → onboarding logo → placeholder). Wired into AdminShell, Clients list, ClientDetail, Profile, ProjectDetail, and the client Dashboard. `onboardingLogoUrl` is written server-side whenever `brandAssets` is saved (`routes/onboarding.js` mirrors the first logo). Backfilled by `scripts/backfillClientLogos.js`.

**Banner hierarchy** — custom `bannerUrl` (priority 0) → a random client-file image via `GET /api/clients/:id/banner-suggestion` (priority 1) → default gradient (priority 2). The suggestion endpoint pulls from the File collection (uploaded "dropbox" files, not logos/brand assets).

### Project

```
name (required), clientId → Client (required)
projectType: 'website' | 'ppc_ads' | 'social_ads' | 'seo' | 'creative' | 'other'
package: String (legacy, not used in UI)
status: 'active' | 'paused' | 'completed' | 'cancelled'
assignedStaff: [ObjectId → User]
startDate, targetDate
isActive: Boolean   // soft delete — filter with { isActive: { $ne: false } }
notes: String       // simple text notes
description: String
internalNotes: [{ title: String, body: String }]   // Apple Notes-style modal
accessInfo:    [{ title: String, body: String }]   // platform credentials (staff-only)
phases: [phaseSchema]
```

Virtual: `services` (populated separately via Service.projectId).

**Notes schema is `{ title, body }` — NOT `{ text }`.** Old `{ text }` schema was replaced. The empty draft constant is `EMPTY_DRAFT = { title: '', body: '' }`.

#### Phase (subdocument)

```
name (required), order (Number), status: 'not_started' | 'in_progress' | 'completed'
tasks: [taskSchema]
```

#### Task (subdocument)

```
title (required)
status: 'todo' | 'in_progress' | 'needs_review' | 'done'
visibility: 'internal' | 'external'   // external tasks shown in client portal
assignedTo: ObjectId → User (default null)
dueDate: Date
notes: String
```

### Service (Discriminator pattern)

Base: `projectId → Project, status, assignedTo, internalNotes, clientNote`

Discriminator key: `serviceType`

| serviceType | Extra fields |
|---|---|
| `GoogleAds` | phase, campaignName, monthlyBudget, landingPageUrl, keywords[] |
| `MetaAds` | phase, campaignName, monthlyBudget, adCreativeStatus, targeting |
| `WebsiteBuild` | phase, domain, hostingProvider, stagingUrl, liveUrl |
| `SEO` | phase, targetKeywords[], auditCompleted, monthlyReportDue |
| `Creative` | phase (brief/pre_production/production/editing/delivered), deliverables, shootDate |

**Service discriminators use `{ strict: false }` implicitly via the base schema options.** When creating new discriminator variants, follow the same pattern in `server/models/Service.js`.

### Invite

```
email, token (uuid, unique), role: 'super_admin' | 'manager' | 'staff' | 'client', clientId (required if client)
invitedBy → User (optional — null for API-key/automated invites)
expiresAt: Date (default null = NEVER expires), used: Boolean
```

**Invites no longer expire by default** — `expiresAt` defaults to `null`. The TTL index (`expireAfterSeconds: 0`) only sweeps docs whose `expiresAt` is an actual date, so null-expiry invites are permanent until used. `accept-invite` only rejects when `expiresAt` is set AND past. Retained for legacy dated invites.

**Deleted-account reactivation:** `accept-invite` reactivates a soft-deleted (`isActive:false`) user with the same email instead of colliding on the unique email index — it resets their name/password/role/clientId and flips `isActive:true`. An *active* account on that email returns 409.

### Onboarding

One document per client (`clientId` unique). Auto-created on first `GET /api/onboarding`.

**Base modules** (always present):
```
businessInfo:    { legalName, dba, businessType, industry }
onlinePresence:  { websiteUrls[], socialPages[], phoneNumbers[] }
yourStory:       { elevatorPitch, coreServices, servicesPricing[{ service, startingPrice }], usps, promoDeals }
yourMarket:      { serviceArea, idealCustomer, competitors }
operations:      { businessHours, address, ein, contractorLicense, authorizedReps[], crm }
brandAssets:     { logoFiles[], brandColors[] }
domainAccess:    { domain, accountHolderName, provider, guideComplete }
websiteAccess:   { cms, provider, guideComplete }
googleBusinessProfile: { businessName, profileUrl, guideComplete }
```

`operations.address` is a nested `{ street, city, state, zip }` object and `operations.crm` is `{ name, context }` — the strategy-guide pre-fill flattens both to strings (see Strategy Guide). Saving `brandAssets` mirrors the first logo into `Client.onboardingLogoUrl`.

**Onboarding → Client.integrations seeding:** saving `googleAdsAccess` / `metaAdsAccess` also seeds the client-wide ad-platform IDs — `googleAdsAccess.accountId` → `integrations.googleMccId`, `metaAdsAccess.adAccountId` → `integrations.metaAdAccountId` (note: `adAccountId`, NOT `accountId` which is the Meta BM/Portfolio ID). Values are regex-validated + canonicalized via `server/utils/onboardingIntegrationIds.js` (`normalizeGoogleCid` → `XXX-XXX-XXXX`, `normalizeMetaAdAccountId` → `act_<digits>`; malformed/free-text dropped). Written **only when the target field is empty** (a conditional `updateOne` matching empty/absent) — first valid value wins, never clobbers a staff-set value. Backfilled by `scripts/backfillIntegrationIds.js`.

**Service-specific modules** (shown in portal only if client has matching project type):
```
websiteBrief:    { referenceSites, pages, features[], featuresOther, integrations }  → website
creativeBrief:   { goals, videoTypes[], videoTypesOther, shootLogistics, talent, references, distribution[] }  → creative
googleAdsAccess: { accountId, hasExistingAccount ('yes'|'no'|''), setupComplete }  → ppc_ads
googleAdsBudget: { monthlyBudget, goals }                                           → ppc_ads
metaAdsAccess:   { hasExistingAccount, accountId (BM ID), adAccountId, pageName, setupComplete, guideComplete }  → social_ads
metaAdsBudget:   { monthlyBudget, goals }                                           → social_ads
seoAccess:       { websiteUrl, guideComplete }                                      → seo
seoGoals:        { targetKeywords, monthlyBudget, goals }                           → seo
```

**Meta fields:**
```
completedModules: [String]    — keys the client has submitted ($addToSet on save)
confirmedModules: [String]    — keys staff have reviewed & accepted
flaggedModules:   [String]    — keys staff flagged for client attention (explicit toggle)
notes:            { [moduleKey]: String }   — client-authored per-module notes
staffNotes:       { [moduleKey]: String }   — staff-authored notes shown to client (informational, decoupled from flag)
```

**Module state machine** (priority order): `flagged` > `confirmed` > `submitted` > `incomplete` > `not_started`.
- `flagged` — key in `flaggedModules`. Client sees "Needs attention" + a "Review & resubmit" button. Resubmitting (`PUT /:module`) auto-clears the flag via `$pull`.
- `confirmed` — key in `confirmedModules`. Client sees "Accepted" (emerald). Staff toggle via `PUT /confirm`.
- `submitted` — key in `completedModules` and passes `MODULE_REQUIRED` check.
- `incomplete` — submitted/has-data but fails `MODULE_REQUIRED`.

**Flag is decoupled from staffNotes** — flagging is an explicit `flaggedModules` toggle; a staff note is informational only and does NOT flag. (Earlier design derived flag from non-empty staffNote — no longer true.)

### Offboarding

Internal-only checklist, **one document per client** (`clientId` unique). Created lazily on first `GET /api/offboarding/for-client/:clientId` (surfaced in the UI only when `clientStatus === 'closed'`). The checklist structure lives in the frontend config (`OFFBOARDING_SECTIONS` in `pages/admin/offboardingConfig.js`); the document stores state only:
```
checked:  [String]   // keys of completed checklist items ($addToSet / $pull)
fields:   Mixed      // typed decision values keyed by field key (final date, ads decision, branch selections…)
notes:    Mixed      // free-text per section, keyed by section key
status:   'in_progress' | 'complete'
completedAt: Date
```
Never exposed to the `client` role.

### StrategyGuide

Per-client strategy guide filled by the scrum master during the team-lead strategy meeting. **One document per client** (`clientId` unique). Structure lives in the frontend config (`STRATEGY_SECTIONS` in `pages/admin/strategyConfig.js`); the document stores:
```
cover:     Mixed     // cover-page fields (package, onboardDate, goLiveDate, location, url…)
fields:    Mixed     // every department item value, keyed by item key (text, a status string, or a budget array [{campaign,amount}] for ga_budget/m_budget)
poc:       Mixed     // per-department point-of-contact name, keyed by section key
prefilled: Boolean   // onboarding pre-fill runs once
status:    'in_progress' | 'complete'
completedAt: Date
```
On first fetch it is **pre-filled once** from the client's onboarding + client record (`buildPrefill` in `routes/strategy.js`) — flattening nested onboarding objects (address, crm, authorizedReps, brand colors) into readable strings. `nonEmpty()` guards against storing objects/arrays.

### AuditLog

Append-only trail (never updated after creation). Actor + entity fields are **denormalized snapshots** so entries stay readable after a user/entity is renamed or deleted.
```
action:      String (indexed)   // e.g. 'client.create', 'user.role_change', 'owner.impersonate'
entityType, entityId, entityLabel
actorId, actorName, actorRole    // snapshot of who did it
meta: Mixed                       // small extra context, e.g. { from, to }
ip: String
createdAt (no updatedAt)
```
Written via `recordAudit(req, {...})` (`server/utils/audit.js`). Read only via `GET /api/audit` (super_admin).

### File / Folder

Both gained an optional `clientId` (alongside optional `projectId`) — a file/folder is owned by a project **OR** directly by a client (client-level items have `projectId: null` + `clientId` set). This powers the **consolidated client file view** (`FileExplorer` client-scoped mode + `GET /api/files/for-client/:clientId`). Existing project files keep their `projectId` and still surface via their project. File: `name, url, key, size, mimeType, projectId, clientId, folderId, uploadedBy`. Folder: `name, projectId, clientId, isPrivate, createdBy`.

---

## API Routes

All routes require `Authorization: Bearer <jwt>` except the public ones noted below.

### Auth — `/api/auth`

| Method | Path | Roles | Description |
|---|---|---|---|
| POST | `/login` | public | Email + password → JWT + user. **Rate-limited** (`authLimiter`). |
| POST | `/forgot-password` | public | Always 200 (never leaks existence). Emails a 1h reset link. Rate-limited. |
| POST | `/reset-password` | public | `{ token, password }` — verifies hashed token + expiry, sets password (≥8 chars). Rate-limited. |
| GET | `/me` | any | Returns current user |
| POST | `/invite` | super_admin, staff | Delegates to `inviteUser()` helper |
| POST | `/invite-teammate` | client | Client invites another user to their **own** company — `clientId` + `role: 'client'` forced server-side (can't target another account or invite staff) |
| POST | `/accept-invite` | public | Consumes token, creates User |
| POST | `/send-instructions/:userId` | super_admin, staff | Sends sign-in instructions email |

**`authLimiter`** (express-rate-limit): 10 attempts / 15 min, keyed by `IP:email`. Requires `app.set('trust proxy', 1)` in `server.js` (set — needed behind Railway's proxy).

### Clients — `/api/clients`

| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/` | super_admin, manager, staff | staff filtered to assigned; populates `assignedStaff`/`users`/`salesRep`/`pocs.user`; attaches `onboardingProgress` per client |
| GET | `/:id` | all | client restricted to own clientId; staff to assigned |
| GET | `/:id/invites` | super_admin, manager, staff | Pending (unaccepted, unexpired) client portal invites |
| GET | `/:id/banner-suggestion` | super_admin, manager, staff | `{ url }` — random image from the client's uploaded files (banner fallback) |
| POST | `/` | **x-api-key OR** super_admin, manager, staff | `apiKeyOrAuth`. Optional `user` provisions a portal user; optional `packages`/`projects` provision projects; optional `salesRep`. Auto-adds managers. Sets `expiryDate`. Records audit. |
| PUT | `/:id` | super_admin, manager, staff | `normalizeClientInput` applied. Returns `await populated(id)`. Records audit. |
| DELETE | `/:id` | super_admin | Soft delete (`isActive: false`). Records audit. |

`populated(id)` populates `assignedStaff` (firstName lastName email avatar **role**), `users` (same), and `salesRep`. All input passes through `normalizeClientInput` (trims/limits strings).

### Accounts — `/api/accounts` (external read API)

A nearly-fully-unfurled, **read-only** view of clients built for other internal apps to consume. Auth mirrors intake: `apiKeyOrAuth('super_admin','manager','staff')` — a valid `x-api-key` sees all; a staff JWT is scoped to assigned clients. (`routes/accounts.js`.)

| Method | Path | Description |
|---|---|---|
| GET | `/` | List. `?status=<clientStatus>`, `?includeInactive=true`. Returns `{ count, accounts:[…] }`. |
| GET | `/:id` | Single unfurled account (404 if soft-deleted; staff must be assigned). Folds in `campaigns: { google, meta }` (latest snapshot summary per platform). |
| POST | `/:id/campaigns` | **Push a campaign snapshot** (append-only, store-and-forward). Body `{ platform:'google'|'meta' (req), adAccountId?, source?, capturedAt?, summary?, campaigns[] }` → `CampaignSnapshot`. |
| GET | `/:id/campaigns` | `?platform=&latest=true&limit=`. `latest=true` → full newest snapshot per platform (`{ google, meta }`); else `{ count, snapshots:[…] }` (newest first). |

**Campaign data** lives in its own **`CampaignSnapshot`** collection (`server/models/CampaignSnapshot.js`), NOT on `Client` — high-volume/time-series. External Google/Meta ad-sync services POST snapshots; Atlas is **store-and-forward** (keeps + returns the raw `campaigns` blob, never parses/queries into it). Append-only (history → trend); "latest per account per platform" via `{ clientId, platform, capturedAt: -1 }` index. `express.json` limit raised to **5mb** for these payloads.

Per-account shape: core fields + `stage` (=`clientStatus`), `salesNotes` (=`notes`), `integrations` (the client-shared `slackChannelId` + clickup/google/meta/ghl IDs; internal Slack is deprecated/removed), `deadlines: { onboarding: expiryDate, goLive: computeGoLiveDeadline(createdAt) }`, fully-unfurled `salesRep`/`assignedStaff`/`users` (with `department`), `pocs: [{ department, …person }]`, **light** `projects: [{ id, name, type, status }]`, and `onboarding: { completed, confirmed, total }`. Heavy relations stay light by design — extend cautiously.

### Projects — `/api/projects`

| Method | Path | Roles | Notes |
|---|---|---|---|
| GET | `/` | all | staff filtered to assigned; client filtered to clientId; soft-deleted excluded |
| GET | `/:id` | all | client role gets filtered view: no internalNotes, no accessInfo, internal tasks removed |
| POST | `/` | super_admin, manager, staff | Returns `await populated(id)` |
| PUT | `/:id` | super_admin, manager, staff | Returns `await populated(id)` |
| DELETE | `/:id` | super_admin | Soft delete |
| POST | `/:id/phases` | super_admin, manager, staff | Adds phase, returns full project |
| PUT | `/:id/phases/:phaseId` | super_admin, manager, staff | |
| DELETE | `/:id/phases/:phaseId` | super_admin | Hard delete of subdocument |
| POST | `/:id/phases/:phaseId/tasks` | super_admin, manager, staff | |
| PUT | `/:id/phases/:phaseId/tasks/:taskId` | super_admin, manager, staff | |
| DELETE | `/:id/phases/:phaseId/tasks/:taskId` | super_admin, manager, staff | |

**Critical pattern — `populated(id)` helper:** All mutation routes in `projects.js` and `clients.js` return `await populated(project._id)` instead of the raw Mongoose result. This ensures `assignedStaff` and `clientId` are always populated in the response. If you add a new mutation route, always return `await populated(...)`.

### Onboarding — `/api/onboarding`

| Method | Path | Roles | Description |
|---|---|---|---|
| GET | `/` | client | Get (or auto-create) the onboarding record for the current user's clientId |
| GET | `/for-client/:clientId` | super_admin, manager, staff | Read any client's onboarding record |
| PUT | `/flag` | super_admin, manager, staff | `{ clientId, module, flagged }` — toggle a module in `flaggedModules` |
| PUT | `/confirm` | super_admin, manager, staff | `{ clientId, module, confirmed }` — toggle a module in `confirmedModules` |
| PUT | `/staff-notes` | super_admin, manager, staff | `{ clientId, module, text }` — set `staffNotes[module]` (informational) |
| PUT | `/notes` | client | Save a per-module note (`{ module, text }`) |
| PUT | `/:module` | client | Save module data + `$addToSet` to `completedModules`, `$pull` from `flaggedModules` (resubmit clears flag) |
| DELETE | `/:module/complete` | client | Remove module key from `completedModules` (re-open) |

**Route ordering is critical:** all named PUT routes (`/flag`, `/confirm`, `/staff-notes`, `/notes`) MUST be declared before the `PUT /:module` catch-all, or Express treats those names as module keys.

`VALID_MODULES` in `onboarding.js` is the authoritative allowlist — add new module keys there when adding to the model. All module saves use `$set: { [module]: req.body }` so new fields work automatically. (`googleBusinessProfile` is a base module.)

### Offboarding — `/api/offboarding` (staff-only, never `client`)

| Method | Path | Description |
|---|---|---|
| GET | `/for-client/:clientId` | Get (or lazily create) the record |
| PUT | `/:clientId/check` | `{ item, checked }` — toggle a checklist item ($addToSet/$pull) |
| PUT | `/:clientId/field` | `{ key, value }` — set a typed decision field (`fields.key`) |
| PUT | `/:clientId/note` | `{ section, text }` — set a per-section note (`notes.section`) |
| PUT | `/:clientId/status` | `{ status }` — `in_progress`/`complete` (records audit on complete) |

### Strategy — `/api/strategy` (staff-only)

| Method | Path | Description |
|---|---|---|
| GET | `/for-client/:clientId` | Get (or create + one-time pre-fill from onboarding) the guide |
| PUT | `/:clientId/field` | `{ key, value }` — set a department item (`fields.key`) |
| PUT | `/:clientId/poc` | `{ section, value }` — set a section point-of-contact (`poc.section`) |
| PUT | `/:clientId/cover` | `{ key, value }` — set a cover-page field (`cover.key`) |
| PUT | `/:clientId/status` | `{ status }` — `in_progress`/`complete` (records audit on complete) |

### Owner — `/api/owner` (owner-EXCLUSIVE — `roles('owner')` gates out even super_admin)

| Method | Path | Description |
|---|---|---|
| GET | `/collections` | List editable collections (declared before `/:collection`) |
| GET | `/:collection` | Paginated, searchable list (`?search=&page=&limit=`) → `{ items:[{_id,label}], total, page, pages }` |
| GET | `/:collection/:id` | Raw document |
| PUT | `/:collection/:id` | Set fields directly (strips `_id/__v/timestamps`; strips `password` for users). Records audit. |
| POST | `/impersonate/:userId` | Mint a JWT to view the app as another user (can't impersonate an owner). Records audit. |

`MODELS` registry covers: clients, projects, users, services, invites, onboarding, offboarding, files, folders, auditlogs.

### Audit — `/api/audit` (super_admin)

| Method | Path | Description |
|---|---|---|
| GET | `/` | Paginated, newest first. Filters: `action`, `entityType`, `actorId`. `?page=&limit=` (max 200). |

### Users (account management) — `/api/users`

Beyond `POST /`, `GET /`, `GET /:id`, `PUT /:id`, `DELETE /:id` (soft delete):

| Method | Path | Roles | Description |
|---|---|---|---|
| PATCH | `/me/welcome` | any | Mark `hasSeenWelcome: true` (before `/:id`) |
| GET | `/pending-invites` | super_admin, manager | Unaccepted invites (before `/:id`) |
| GET | `/sales-reps` | **x-api-key OR** super_admin, manager, staff | `[{ id, name, email }]` of active `department:'sales'` users (intake dropdowns). Before `/:id`. |
| PUT | `/:id/password` | super_admin, manager | Set a user's password directly (≥8 chars; invalidates reset token) |
| POST | `/:id/reset-link` | super_admin, manager | Email the user a reset link on their behalf |
| PUT | `/:id/active` | super_admin, manager | Activate/deactivate (`{ isActive }`; can't self-deactivate) |
| DELETE | `/invites/:id` | super_admin, manager | Cancel a pending invite |

`canActOn(actor, target)` guard: only an owner may act on an owner; only super_admin/owner on a super_admin. Role changes are super_admin/owner-only, never self (no lockout), and only an owner may grant `owner`. `PUT /:id` records a `user.role_change` audit when the role actually changes.

### Files — `/api/files`

Presign + record flow (S3). Key endpoints: `POST /presign`, `GET /` (project-scoped), `GET /for-client/:clientId` (**consolidated client view** — all files across the client's projects + client-level files), `POST /` (record a file), `POST /download` (zip multiple via `archiver@^7`), `GET /:id/download`, `DELETE /:id`, folder CRUD (`POST /folders`, `PUT /folders/:id`, `DELETE /folders/:id`).

---

## Auth & Role System

### Roles

| Role | Access |
|---|---|
| `owner` | **Superset of everything** — passes every `roles()` gate via short-circuit. Sole access to `/api/owner` (record editor + impersonation). Can grant the `owner` role. |
| `super_admin` | Everything except owner-exclusive routes. Can delete. Reads the audit log. Can preview client portal. Sees DebugPanel. |
| `manager` | Reads all clients/projects; account management (Users tab: passwords, reset links, activate/deactivate, cancel invites); auto-assigned to every new client. |
| `staff` | Scoped to assigned clients/projects only |
| `client` | Portal only — read-only, filtered project view |

### Auth flow

1. `protect` middleware (JWT verify) → attaches `req.user` (User document minus password)
2. `roles(...allowed)` middleware — checks `req.user.role` against whitelist. **`owner` short-circuits and passes any gate** (`middleware/roles.js`). Use `roles('owner')` to make a route owner-EXCLUSIVE. `isAdmin(user)` = `super_admin || owner` — exported from both `server/middleware/roles.js` and `client/src/lib/roles.js` (client also has `isOwner(user)`); use it for inline checks that previously read `role === 'super_admin'`.

### Owner mode (impersonation)

`POST /api/owner/impersonate/:userId` mints a fresh JWT for the target user. The client (`AuthContext.impersonate()`) stashes the current `ownerToken` in localStorage and swaps in the impersonation token; `stopImpersonating()` restores it. Owners can't impersonate other owners.

### Invite flow (client portal users)

1. Staff clicks "Add portal user" → `POST /api/auth/invite` with `{ email, role: 'client', clientId }`
2. Server creates Invite record + sends invite email via Gmail SMTP
3. User clicks link → `/accept-invite?token=...` page
4. `POST /api/auth/accept-invite` with `{ token, firstName, lastName, password }`
5. User created, `Client.users` updated via `$addToSet`, invite marked `used: true`

Alternative: "Set password" mode in `AddUserForm` creates user directly via `POST /api/users`.

### Sign-in instructions

`POST /api/auth/send-instructions/:userId` — sends login URL email. Login URL is `CLIENT_URL/login` (pulls from env — do NOT hardcode localhost).

---

## Frontend Architecture

### Providers (main.jsx order)

```
ErrorBoundary
  ThemeProvider
    AuthProvider
      BrowserRouter
        App
```

`ErrorBoundary` (`components/ErrorBoundary.jsx`) is the app-wide React error boundary — a glass fallback card with a reload action (replaces the "no error boundaries" gap). `ClientsProvider` wraps the admin routes only (inside `AdminLayout` in `App.jsx`).

### Routing (App.jsx)

- `/login` — public
- `/demo` — public (enters sessionStorage demo mode)
- `/accept-invite`, `/forgot-password`, `/reset-password` — public
- `/dashboard` — client portal shell (`ClientShell`), PrivateRoute: client, super_admin, owner
  - `/dashboard/onboarding` — onboarding hub
  - `/dashboard/projects` — read-only project list
- `/admin/*` — admin shell (PrivateRoute: owner, super_admin, manager, staff)
  - `/admin` — dashboard
  - `/admin/clients` · `/admin/clients/:id` — client list + detail
  - `/admin/clients/:id/onboarding` · `/offboarding` · `/strategy` — staff review pages
  - `/admin/projects` · `/admin/projects/:id` — project list + detail
  - `/admin/team` — team directory
  - `/admin/users` — account management (super_admin/manager)
  - `/admin/records` — owner-only generic record editor
  - `/admin/audit` — super_admin audit log
  - `/admin/profile` — profile
- `*` → redirects to `/` → `RoleRouter` → `/dashboard/onboarding` or `/admin`

(Route elements exist for all paths; nav visibility is role-gated in AdminShell — `/admin/records` owner-only, `/admin/audit` super_admin, `/admin/users` super_admin/manager.)

### ClientsContext

Fetches `/api/clients` and `/api/projects` once on mount. Provides `clients`, `projects`, `selected` (first client by default), `setClients`, `setProjects`, `setSelected`. The AdminShell sidebar reads from this context — update state locally after mutations to avoid full refetch.

### AdminShell (sidebar)

- Fixed left sidebar, 224px wide
- Imports `CLIENT_STATUS` from `pages/admin/Clients.jsx` for status dot colors
- Shows active client by matching URL params (clientMatch, projectMatch)
- **Ever-present client search** — a search box pinned above the client list (`clientSearch` state). Typing filters `ClientsContext.clients` by company name in-memory (no API) and swaps the accordion for a flat match list; Enter jumps to the first match, Esc/✕ clears, empty restores the accordion.
- Client list is a **single-open accordion grouped by phase** (`clientStatus`): each non-empty status is a collapsible drawer (dot + label + count + chevron); opening one closes the others. **All phase headers stay visible** — the open drawer flex-grows and scrolls internally (headers `flexShrink:0`), so long buckets (e.g. Live) never push Closed off-screen. Navigating to a client auto-opens its phase (`openPhase` state + effect). `renderClient()` draws each row (+ its projects when active).
- When a client is active, shows its projects beneath it in the sidebar
- Bottom bar: the whole **avatar + name/role block is the profile link** (hover bg); theme + logout grouped to the right behind a divider (logout hovers pink)
- **`owner`** sees the "Client view" button (navigates to `/dashboard/onboarding`); **`super_admin`+`owner`** see `DebugPanel` (the two are gated separately)

### ClientShell (client portal layout)

`components/layout/ClientShell.jsx` — wraps all `/dashboard/*` routes via `<Outlet />`.

**Desktop (≥ 768px):**
- Fixed left sidebar, `SIDEBAR_W = 220px`
- Fixed bottom team bar (`left: SIDEBAR_W`) showing `client.assignedStaff` chips + Slack button (when `client.integrations.slackChannelId` is set — the client-shared channel)
- `main` gets `paddingBottom: 49px` when team bar is present

**Mobile (< 768px):**
- Fixed top nav bar (52px): logo + company name + theme/logout buttons
- Fixed bottom tab bar (64px): icon + label per nav item, active state with teal border
- Floating Slack FAB pill at `bottom: 80px, right: 16px` (above tab bar) — only when Slack channel set
- Sidebar and team bar hidden; `main` gets `paddingTop: 52px, paddingBottom: 64px`

**Data:** Fetches `GET /api/clients/:clientId` once on mount to get `assignedStaff` and `integrations`. `clientId` extracted from `user.clientId` (handles both ObjectId and populated object).

**`useIsMobile` hook:** `client/src/hooks/use-mobile.js` — takes a `breakpoint` param (default 768), listens to `window.matchMedia` change events. Import and call at component level; use two instances for two breakpoints (e.g. 768 and 480).

**`FeaturedNavItem`** — the Onboarding nav item in the sidebar has a spinning conic-gradient border (same `live-pill-spin` animation) when inactive. When active it switches to a plain teal muted background with no animation. Defined inline in ClientShell.

**Logo.dev pattern:** `https://img.logo.dev/${domain}?token=${import.meta.env.VITE_LOGO_DEV_TOKEN ?? 'pk_free'}&size=64` — always add `onError={(e) => e.currentTarget.style.display = 'none'}` as a fallback.

### API client (lib/api.js)

- Axios instance, baseURL from `__API_BASE__` Vite define
- Request interceptor: attaches `Authorization: Bearer <token>` from localStorage
- Response interceptor: 401 (non-login) → clear token + redirect to `/login`

---

## Design System

### Typography

Custom font: **Suisse Intl** (Light 300, Regular 400, Bold 700). Files in `client/public/fonts/`. Applied globally via `@font-face` in `index.css`. All headings and critical UI text explicitly set `fontFamily: "'Suisse Intl', system-ui, sans-serif"`.

### CSS Variable Naming — CRITICAL GOTCHA

The accent variable names do NOT match their visual colors:

| Variable | Actual color | Used for |
|---|---|---|
| `--accent-violet` | `#00A19B` (teal/cyan) | Primary brand color, active states, CTAs |
| `--accent-teal` | `#6366F1` (indigo/violet) | Secondary accent |
| `--accent-pink` | `#F06292` (pink) | At-risk status, warnings |

This is intentional — do not rename them. All code, class names, and token references use these names. Changing them would break the entire design system.

### Design Tokens

All defined in `client/src/index.css` under `@layer base`.

**Light mode glass tokens:**
```css
--glass-bg:       rgba(255, 255, 255, 0.70)
--glass-bg-heavy: rgba(255, 255, 255, 0.85)
--glass-nav:      rgba(248, 248, 248, 0.80)
--glass-border:   rgba(0, 0, 0, 0.07)
--glass-shadow:   0 4px 32px rgba(0,161,155,0.08), ...
```

**Dark mode glass tokens:**
```css
--glass-bg:       rgba(255, 255, 255, 0.05)
--glass-bg-heavy: rgba(20, 20, 24, 0.82)   ← IMPORTANT: was rgba(255,255,255,0.08), that was nearly invisible
--glass-nav:      rgba(16, 16, 18, 0.80)
--glass-border:   rgba(255, 255, 255, 0.07)
```

**Accent palette (same light + dark):**
```css
--accent-violet:       #00A19B
--accent-teal:         #6366F1
--accent-pink:         #F06292
--accent-violet-muted: rgba(0, 161, 155, 0.12)
--accent-teal-muted:   rgba(99, 102, 241, 0.12)
--accent-pink-muted:   rgba(240, 98, 146, 0.12)
```

**Custom scrollbars** — themed via `--scrollbar-thumb` / `--scrollbar-thumb-hover` tokens (defined per theme in `:root` + `.dark`, rgba to avoid the HSL-slash gotcha). WebKit `::-webkit-scrollbar` (10px, pill thumb) + Firefox `scrollbar-width/color`; native fallback where unsupported. Applied app-wide via `*`.

### Component Classes (index.css)

| Class | Description |
|---|---|
| `.portal-card` | Glass card with backdrop-filter blur |
| `.portal-card-heavy` | Same but heavier glass |
| `.portal-nav` | Sticky glass nav bar |
| `.portal-input` | Glass-style form input, teal focus ring |
| `.btn-primary` | Teal gradient CTA button |
| `.btn-ghost` | Glass ghost button |
| `.badge-violet` | Teal badge pill |
| `.badge-teal` | Indigo badge pill |
| `.badge-pink` | Pink badge pill |
| `.gradient-text` | Gradient text (violet → teal) |
| `.progress-gradient` | Shimmer progress bar |
| `.live-pill` | Animated orbiting glow border pill (used for status) |
| `.aurora-glow` | Slow breathing opacity animation |
| `.walkthrough-btn` | Onboarding Next/Complete button — shimmer sweep on hover, scale-down on active |

### SlideOver component

`client/src/components/ui/slide-over.jsx` — right-side drawer. Props: `open`, `onClose`, `title`, `subtitle`, `children`, `width` (Tailwind max-w class, default `max-w-lg`).

Panel background: `var(--glass-bg-heavy)` + `backdropFilter: blur(32px)`. No `box-shadow` (removed intentionally — it looked wrong against glass). Escape key closes.

**createPortal + event bubbling fix:** When rendering modals or dropdowns inside `createPortal`, synthetic React events bubble through the React component tree (not the DOM tree). Any `onClick` on an ancestor in the React tree will fire. Always call `e.stopPropagation()` at the portal root div to prevent this.

### Client Status Colors

Defined in `pages/admin/Clients.jsx` and exported:

```js
export const CLIENT_STATUS = {
  onboarding:  { label: 'Onboarding',  color: '#f59e0b', bg: 'rgba(245,158,11,0.12)' },
  development: { label: 'Development', color: '#3b82f6', bg: 'rgba(59,130,246,0.12)' },  // blue — distinct from live's green
  live:        { label: 'Live',        color: '#10b981', bg: 'rgba(16,185,129,0.12)' },
  at_risk:     { label: 'At Risk',     color: 'var(--accent-pink)', bg: 'var(--accent-pink-muted)' },
  closed:      { label: 'Closed',      color: 'hsl(var(--muted-foreground))', bg: 'var(--glass-bg)' },
};
export function ClientStatusBadge({ status }) { ... }
```

AdminShell imports `CLIENT_STATUS` from here. Don't duplicate it.

**Client tier (Key Accounts)** — `CLIENT_TIER` (standard/key), `KeyAccountTag`, `KEY_COLOR` (`#f59e0b` gold) are exported from `Clients.jsx`. Set on ClientDetail via a **Tier picker** (mirrors the Status picker; `updateTier` → `PUT /clients/:id { tier }`). A `key` account shows a **gold ★ "Key" tag** in the ClientDetail header + a gold inset ring on its tile/board cards (★ before the name on the board). The Clients page has a **"★ Key" filter toggle** (`keyOnly`, shown when any exist) and **floats Key Accounts to the top** of every list/board column (`keyFirst` sort). Piped through `/api/accounts` (`tier` field). "Key Account" = persistent importance; use `at_risk` status + deadlines for transient urgency.

---

## Key Components — Detailed Notes

### ProjectDetail.jsx

The most complex file in the codebase. Broken into:

- **`TaskModal`** — centered portal modal (via `createPortal`). Shows task title (editable), notes, assignee dropdown, due date. Key state: `pendingAssignee` (local draft before save-on-clickout), `assigneeOpen`, `displayAssigneeId`. Assignee is saved to DB on click-outside (mousedown handler), not on dropdown close. Non-assigned tasks show no avatars.

- **`TaskRow`** — always shows assignee button (real avatar OR faint `User` icon at `opacity 0.35`) and due date button (real date OR faint `Calendar` icon at `opacity 0.35`). Delete button hover-only. Uses two refs for date popover: `dateRef` (trigger button) and `datePopoverRef` (portaled calendar div). Both checked in mousedown handler to avoid false close.

- **`PhaseCard`** — renders phase header with inline rename (`editingName` state, pencil icon on hover) and delete. Passes `allStaff={project.assignedStaff}` to TaskRow — tasks can only be assigned to staff already assigned to the project (NOT all staff).

- **`InternalNotesCard`** — reusable notes editor (Apple Notes style, centered modal). Prop `lockTitles` (default false) — when true, title inputs are `readOnly`, `cursor: default`, muted color. Used by Access Info card with `lockTitles={true}`.

**Access Info defaults:** Every new project gets 4 pre-populated access info entries:
```js
const DEFAULT_ACCESS_INFO = [
  { title: 'Access: CRO/Web', body: '' },
  { title: 'Access: Google', body: '' },
  { title: 'Access: Meta', body: '' },
  { title: 'Access: Creative', body: '' },
];
```
Defined in both `CreateProjectForm.jsx` and `CreateClientForm.jsx` (intentionally duplicated — no shared import). Access info titles are locked (`lockTitles` prop) — they cannot be edited by users.

### Clients.jsx (masterpage)

Exports: `CLIENT_STATUS`, `ClientStatusBadge`.
Local: `PersonProfile` component (shows in SlideOver when staff/portal user name is clicked).
`ClientRow` takes `onViewPerson` callback — clicking staff/portal user names triggers it.
`AdminClients` holds `selectedPerson` state, renders `SlideOver` + `PersonProfile` at page level.
No "Add portal user" button on the masterpage — that's only on ClientDetail.
**List / Tile / Board view toggle** (persisted to `localStorage.clientsView`; **default `tile`**): `ClientRow` = full list card; `ClientTile` = condensed grid card (`repeat(auto-fill, minmax(280px,1fr))`) with logo, name, status, industry, a one-line **scope string** of distinct service types (`PROJECT_TYPE_LABEL`, e.g. "Website, PPC, SEO"; cancelled excluded, fixed order, truncated), an **onboarding progress** indicator (mini bar + `completed/total`, total = `BASE_MODULE_COUNT` + per-service `SERVICE_MODULE_COUNTS` from the client's project types), the website link, an **ad-spend row** (inline google/meta SVG glyphs + compact spend from `client.adSpend`, prefixed **`L30`** = last-30-days, only for platforms with a snapshot; `GoogleGlyph`/`MetaGlyph` are exported from Clients.jsx), and a **subtle monthly billing figure** (`billing.baseAmount` + `· intro` when `introPricing`) — users/staff/integration-icons omitted. **Board** = a Trello-style kanban: one column per `CLIENT_STATUS`, `BoardCard`s are HTML5-**draggable** (no dnd lib); dropping a card in another column calls `moveClient()` → `PUT /clients/:id { clientStatus }` (optimistic via `setClients`, reverts + toasts on failure). Drop column highlights on drag-over. Board ignores the status filter (shows all columns).
- **Tile density toggle** (`tile` view only, persisted to `localStorage.clientsTileExpanded`): **Compact** (default) vs **Details**. Details adds a super-lean "Contacts" strip to each `ClientTile` — Sales (from `salesRep`) + per-department POCs (dept tag + mini-avatar + name), with room reserved for future live ad-spend. Requires `GET /api/clients` to populate `salesRep` + `pocs.user` (it now does).

### ClientDetail.jsx

- **Ad performance card** (`CampaignsCard`, above Billing) — ClientDetail fetches `/accounts/:id/campaigns?latest=true` once into `campaignData` and passes it as a `data` prop (shared with Billing). Renders per platform (google/meta): summary metrics (spend/impr./clicks/conv.) + a spend-sorted campaign list (status dot, name, clicks, spend), with the L30 period + last-updated date. Footer footnote shows **billed ad-spend fee = 10% of total L30 spend** across platforms. **Renders nothing** when no snapshots exist (silent). Reuses `GoogleGlyph`/`MetaGlyph` from Clients.jsx.
- **Billing card** (below Ad performance) — **collapsible**: collapsed shows a readable core row (package · base · day · start date, values emphasized) or an amber "Not set up"; the header always shows the **compiled monthly due = base + 10% of L30 spend** (`dueVal`, tooltip breaks down base + fee). Expanded reveals the full `BILLING_DISPLAY` grid **plus a computed "Billed ad-spend fee (L30)"** field (= 10% of total L30 spend from `campaignData`). The Strategy Guide aurora card is a compact variant flush inside the onboarding column (grid cell), not a full-width banner. Managers+ (`canManageBilling`) get an Edit → `SlideOver` with selects (package/type/merchant), number/date/checkbox inputs → `saveBilling` PUTs `{ billing }` (full object; empty enums omitted so they unset; `introEndsDate` never sent — server recomputes). `parentAccountId` renders as a link to that client.
- Assigned staff rows: clickable buttons → `setSelectedPerson(staff)` → SlideOver
- Portal users: same clickable pattern
- "+" (UserPlus) button next to "Portal users" heading → `setInviteSlide(true)` → `AddUserForm` SlideOver
- `AddUserForm`: two modes — "Set password" (direct account creation) and "Send invite email" (token invite). Set-password has an **autogen** password (readable, no ambiguous chars) + show/hide, and on success shows the credentials with a **"Copy login details"** button (email + password + `origin/login`, shown once). Creating directly also **supersedes any pending invite** for that email — `POST /api/users` runs `Invite.deleteMany({ email, used:false })` (covers the invite-email-never-landed case).
- `PersonProfile` SlideOver: shows role badge, "Send sign-in instructions" only if `role === 'client'`, "Remove user" only for clients
- Staff/portal user buttons both use: `className="w-full flex items-center gap-2 py-1 rounded-lg px-1 -mx-1 transition-colors hover:bg-[var(--glass-bg)] text-left"`
- **Department contacts (POCs)** — the first header-grid column is now a **"Dept contacts"** list ONLY (the assigned-staff roster + staff-assign picker were removed from ClientDetail; `assignedStaff` is still fetched and used as the POC picker's pool, just not displayed). Shows the assigned POCs (small avatar + name + right-aligned dept tag → PersonProfile); "None set" when empty. The edit pencil opens a SlideOver with a **searchable per-department picker (`PocPicker`) over ALL active staff** (not just assigned; same-department sorted first) — selecting a contact is how you add someone to a client now (the old assigned-staff roster/assigner was removed). **`savePocs` unions the chosen POCs + sales rep into `assignedStaff`** and PUTs `{ pocs, salesRep, assignedStaff }`, so picking a contact auto-grants access. **Multiple POCs per department (equal peers, no cap)** — `Client.pocs` stays the flat `[{department, user}]` array (no schema change), just allows several rows per department; the editor's dept pickers are multi-select (chips), readers `.filter` (not `.find`) and render all. **Sales stays single** (the dedicated `salesRep` field). `/api/accounts` already emits one entry per pair, so it handles multiples unchanged. **NOTE:** `GET /api/clients/:id` must populate `salesRep` + `pocs.user` (it does now) — otherwise POCs render "undefined undefined" until a mutation returns the fully-`populated()` doc. **Sales is shown/edited in the same list** (`CONTACT_DEPTS = ['sales', ...POC_DEPARTMENTS]`) but is stored in the dedicated `client.salesRep` field (kept separate so intake auto-assign/resolve still works), not in `pocs`. Save sends `PUT /clients/:id { pocs, salesRep }`. (`POC_DEPARTMENTS` = web/growth/google/meta/creative, exported from `Team.jsx` with `DEPARTMENT_LABELS`.) `Client.pocs` is `[{ department, user }]` — an overlay on `assignedStaff`; `department` (User) drives picker order/badges but is NOT the source of truth. Populated on all client fetches + surfaced in `/api/accounts`. Empty departments show only in the editor.
- **GoHighLevel deep-link** — the GHL integration row links straight to the **contact** (`ghlContactUrl(location, contact)`) when `integrations.ghlContactId` is set, else the sub-account dashboard. `ghlContactId` is manual-set via the integrations editor (a GHL contact link needs the contact id, which email/phone can't derive) and is also an intake flat-alias, so it auto-fills if the GHL webhook passes it. `IntegrationRow`'s `def.link(value, integrations)` receives the whole integrations object so a link can use sibling ids.
- **Integrations card** — data-driven from `INTEGRATION_DEFS` (module-scope: googleMccId, metaAdAccountId, slackChannelId, clickupFolderId, ghlSubaccountId). Renders **every** integration always via `IntegrationRow`: set → deep-link (or inline value for Google, which has no link); missing → muted "Not set" row. Header shows a "N missing" amber badge. **Editing is managers+ only** (`canManageIntegrations = owner|super_admin|manager`): the "Edit" button + click-to-add on a missing row open the integrations SlideOver, whose empty fields are tagged "empty". Staff see the same card read-only (missing still visible).

### Onboarding.jsx (client portal)

`pages/client/Onboarding.jsx` — the largest frontend file. Key internal components:

- **`WalkthroughModal`** — two-pane modal (left nav + right content). On desktop: 240px left nav lists steps with progress bar. On mobile (`< 768px`): slides up from bottom as a sheet, left nav hidden, replaced by icon + step-dot progress at top of right pane; `borderRadius: '20px 20px 0 0'`, height `calc(100dvh - 52px)`.
  - Step skip logic: each step can define `skip: (values) => bool`. A `useEffect` auto-advances if the current step becomes skipped.
  - Finish saves via `onSubmit(values)` → `PUT /api/onboarding/:module`.

- **`ModuleCard`** — grid card for each module. Shows state badge (done / incomplete / flagged / not started), description, time estimate, and per-module notes button.

- **`SERVICE_SECTIONS`** — constant mapping `projectType → { label, domain, modules[] }`. Sections are shown only if the client has an active project of that type (derived from `GET /api/projects` which is role-filtered). Logo fetched from logo.dev using the `domain` field (set `domain: ''` for packages with no brand, e.g. `website` — the logo `onError`-hides).

**Adding a service module for a package — sync these 8 places:**
1. `server/models/Onboarding.js` — the field group
2. `VALID_MODULES` in `server/routes/onboarding.js`
3. `SERVICE_SECTIONS` in `pages/client/Onboarding.jsx` (card) + a `*Modal` component + the render-switch entry
4. `MODULE_REQUIRED` + `MODULE_MISSING` in `pages/client/Onboarding.jsx`
5. `SERVICE_SECTIONS` in `pages/admin/ClientOnboarding.jsx` (staff)
6. `MODULE_REQUIRED` + a `ModuleDataView` case in `pages/admin/ClientOnboarding.jsx`
7. `SERVICE_MODULE_COUNTS` in `pages/admin/ClientDetail.jsx`
8. `SIDEBAR_SERVICE_COUNTS` in `components/layout/AdminShell.jsx`

**Package coverage:** `website` → `websiteBrief` ✅ · `creative` → `creativeBrief` (video-focused) ✅ · `ppc_ads`/`social_ads`/`seo` → 2 modules each ✅ · `other` → none (intentional). `creative` now has a `Creative` Service discriminator and is mapped in `SERVICE_MAP`. Only `other` gets no service.

- **`allModules`** — `[...MODULES, ...visibleServiceModules]`. All progress calculations (%, counts, "N of M complete") use this, not just `MODULES`.

- **Hub grid** — `repeat(4, 1fr)` desktop → `repeat(2, 1fr)` at `< 768px` → `repeat(1, 1fr)` at `< 480px`. Hero spans 3→2→1 cols, status card spans 1→2→1, dividers span full width at each breakpoint. Uses `useIsMobile(768)` and `useIsMobile(480)` (named `isMobile` / `isSmall`).

- **Status card mobile** — compact layout: 30px icon + label on one row, description sentence below, count pills as horizontal flex row. Desktop layout unchanged.

- **`MODULE_REQUIRED`** — map of `moduleKey → (data) => bool` defining whether saved data is complete enough to count as `done`. A completed module whose data no longer satisfies this shows as `incomplete`.

- **`getModuleState(key, onboarding)`** — derives display state, priority order:
  1. `'flagged'` — key in `flaggedModules` (explicit staff toggle, NOT derived from staffNotes)
  2. `'confirmed'` — key in `confirmedModules` (staff accepted) → shows "Accepted" (emerald)
  3. `'submitted'` — key in `completedModules` and passes `MODULE_REQUIRED`
  4. `'incomplete'` — submitted/has-data but fails `MODULE_REQUIRED`
  5. `'not_started'` — default

  Flagged cards show a staff-note preview + a "Review & resubmit" button; resubmitting (`PUT /:module`) auto-clears the flag server-side. The mirrored copy of this logic lives in `pages/admin/ClientOnboarding.jsx` for the staff review view.

- **Two modal shells in Onboarding.jsx:**
  - `ModalShell` — simple centered single-pane modal (`maxWidth: 820px`, `maxHeight: 88vh`, scrollable body). Used for `NotesModal` and `BrandAssetsModal`.
  - `WalkthroughModal` — two-pane modal (240px left nav + right content). Used for all data-collection modules. On mobile it becomes a bottom sheet.

- **Google Ads access flow** — MCC model: client provides Customer ID only; AM sends a link request from their Manager Account. New accounts get a setup guide (step 1 skipped if `hasExistingAccount === 'yes'`).

### MiniAvatar

`client/src/components/ui/mini-avatar.jsx`. Props: `user`, `size` (default 20).
Shows photo if `user.avatar`, else initials (first chars of firstName + lastName) with teal muted background.
Border-radius: `Math.round(size * 0.3)`.
Used heavily in task rows, staff lists, person profiles.

### CreateClientForm

Does three things in sequence:
1. `POST /clients` — creates client
2. `POST /projects` (parallel) — creates each selected project with phases + access info
3. `POST /services` — creates Service record for each project type that maps to a service

`DEFAULT_PHASES` map: each project type has pre-defined phase/task structure.
`SERVICE_MAP`: `{ website: 'WebsiteBuild', ppc_ads: 'GoogleAds', social_ads: 'MetaAds', seo: 'SEO', creative: 'Creative' }` (only `other` gets no service).
Company name changes auto-update project name suggestions.

---

## Email System

`server/utils/email.js` — Nodemailer transporter with:
```js
{ host, port: 587, secure: false, requireTLS: true, auth: { user, pass } }
```

**`requireTLS: true` is required** for Gmail SMTP on port 587. `secure: false` + `requireTLS: true` = STARTTLS.

Three email functions (all branded "Atlas", teal CTA buttons, `FROM = "Atlas — Advanced Marketers"`):
- `sendInviteEmail({ to, inviteUrl, role })` — invite to accept account
- `sendLoginInstructionsEmail({ to, firstName, portalUrl })` — sign-in link for existing users
- `sendPasswordResetEmail({ to, firstName, resetUrl })` — 1h password reset link

**Email links use `CLIENT_URL` env var.** Never hardcode URLs. If login instructions send `localhost`, it means `CLIENT_URL` in the server env is set to the local URL, not the deployed frontend URL.

---

## API-based Client Creation

`POST /api/clients` uses `apiKeyOrAuth(...)` middleware (`server/middleware/apiKey.js`): if a valid `x-api-key` header matches `process.env.API_KEY`, the request passes; otherwise it falls back to the normal `protect` + `roles` chain. Same endpoint serves the staff UI and external/automated systems.

**Optional inline user provisioning** — pass a `user` object in the body:
- `{ user: { email, firstName, lastName, password } }` → `createUser()` (account exists immediately)
- `{ user: { email, firstName, lastName } }` → `inviteUser()` (sends an invite email; `invitedBy` is null on the API-key path)

**Optional inline project provisioning** — pass `packages` (sales tokens) or `projects` (raw Atlas types). Each is a **space/comma-separated STRING or an array** (`expandPackages()` handles both, so GHL can send a raw string — no Zapier parsing):
- `{ packages: "web seo ppc creative" }` → `website, seo, ppc_ads, creative`. Token map (`PACKAGE_TO_TYPES`): `web/website`→website, `seo`→seo, `ppc/google`→ppc_ads, `social/meta`→social_ads, `marketing`→ppc_ads+social_ads, `creative`→creative, `other`→other.
- `{ projects: ["website","seo"] }` — raw Atlas types (same expander; unknown tokens are ignored).
- Each resulting type creates a Project (default phases + access info) + a Service record, via `provisionProjects()` — same output as the in-app `CreateClientForm`. Used by the **GHL sales-intake → Atlas** webhook (`intake/intake.html` is the public form's source copy, served live from `client/public/intake/` at `/intake/intake.html`; `temp/index.html` is the legacy form).

Response shape: plain populated client when neither `user` nor projects provided (backward compatible — the in-app form relies on this); `{ client, user, projects }` when `user` or `packages`/`projects` present. The client is created first — if user provisioning throws, the client is still returned with a `userError` (partial success) so callers can retry just the user without duplicating the client.

**`server/utils/provisionUser.js`** is the single source of truth for user creation — `createUser()` and `inviteUser()` are reused by `routes/users.js`, `routes/auth.js` (`/invite`), and `routes/clients.js`. Both throw `HttpError(status, message)`; routes catch it and map to a response. Role-permission checks (e.g. staff can only create client users) stay in the route since they depend on `req.user`.

## Deadlines & Countdowns

Two phase-based deadlines, both live-counted by the same hook/UI:

**Onboarding deadline** (`client.expiryDate`, status `onboarding`) — **Rule (day-based, LA time):** `createdAt` → +2 calendar days → next Mon/Wed/Fri at **12:01am LA**. `server/utils/expiryDate.js`. Set on `POST /api/clients`; backfill `scripts/backfillExpiryDates.js`.

**Go-Live deadline** (status `development`) — **Rule:** **10 business days** (Mon–Fri) after **`createdAt`**, at **6:00pm LA**. Computed **on the frontend** via `client/src/lib/goLive.js` `goLiveDeadlineFrom(createdAt)` (mirrors `server/utils/goLiveDeadline.js`). Not persisted — works for all clients (existing included) since every client has `createdAt`. The unused `client.goLiveDeadline` field + the server util are kept in case it should later anchor on the development-transition date instead.

Frontend live countdown via **`client/src/hooks/use-countdown.js`** `useCountdown(targetDate)` — ticks every minute, returns `{ expired, hours, minutes, totalMinutes }` or `null`. Shown:
- **Client portal** (`Onboarding.jsx` hero) — onboarding pill only
- **ClientDetail** — `CountdownBadge` in the (card-wrapped, teal-tinted) onboarding column; shows time remaining **+ the deadline date** ("Wed, Jul 1"). Label + source switch on status: onboarding→`expiryDate`, development→`goLiveDeadline`.
- **AdminShell sidebar** — `SidebarOnboarding` (progress bar + "Onboard by <date>") for onboarding; `SidebarGoLive` ("Go-Live by <date>" + urgency dot) for development.

Urgency colors: teal (default) → amber (`#f59e0b`) → red (`#ef4444`). Onboarding thresholds <24h/<12h; go-live thresholds <3d/<1d.

---

## Feature Systems (added v0.4 → v0.11)

### Audit Log

Append-only trail written via `recordAudit(req, { action, entityType, entityId, entityLabel, meta })` (`server/utils/audit.js`) — snapshots the actor (name/role) and entity label so entries survive renames/deletes. Emitted from client/user/owner/strategy/offboarding mutations. Read at `/admin/audit` (`pages/admin/AuditLog.jsx`, super_admin only) via `GET /api/audit` (paginated, filterable by action/entityType/actorId). Actions include `client.create|update|delete`, `user.role_change|password_set|reset_link|deactivate|reactivate|delete`, `owner.edit|impersonate`, `strategy.complete`, `offboarding.complete`.

### Owner Mode & Record Editor

`owner` is the top tier (see Auth). `/admin/records` (`RecordEditor.jsx`, owner-only) is a generic collection browser/editor over `/api/owner/*` — pick a collection, search/paginate, open a raw document, edit fields inline (protected keys + user passwords stripped server-side). Impersonation ("view as user") mints a JWT via `POST /api/owner/impersonate/:userId`; `AuthContext.impersonate()/stopImpersonating()` swap tokens (original stashed as `ownerToken`). Grant the role with `node scripts/make-owner.js <email>`.

### Strategy Guide

Per-client planning doc filled during the team-lead strategy meeting (`pages/admin/ClientStrategy.jsx` at `/admin/clients/:id/strategy`; config in `strategyConfig.js`). `STRATEGY_SECTIONS` = departments (growth, googleAds, metaAds, cro, creative), each with an `accent` color, item list, and a point-of-contact. On first open the guide is **pre-filled once** from onboarding (`buildPrefill` in `routes/strategy.js`). Field/poc/cover saves autosave via `PUT /api/strategy/:clientId/{field,poc,cover}`.

**Item types:** `text` (textarea) · `status3` (Yes/No/Pending) · `status2` (Yes/No) · **`budget`** — a repeatable table of `{ campaign, amount }` rows, stored in `fields[key]` as an **array** (not a string). The googleAds + metaAds sections each have a `budget` item (`ga_budget` / `m_budget`). Config exports helpers `budgetRows`, `budgetTotal`, `fmtMoney`, and `isValueFilled` (progress-completion check that understands budget arrays); the editor renders `BudgetField` (add/remove rows + live total). No backend change was needed — `StrategyGuide.fields` is `Mixed`, so the `$set: { fields.<key>: [...] }` stores the array directly.

**Exporters** (`strategyConfig.js`, shared by ClientStrategy + ClientDetail quick-export): `buildStructured` (JSON), `buildMarkdown` (MD), `downloadStrategyFile` (JSON/MD download), `openStrategyDoc` (opens a styled read-only HTML doc in a new tab — uses the internal Suisse Intl stylesheet via same-origin `@font-face`). **Print was intentionally removed** — the doc is a static view, no print dialog. Each service section is color-coded (soft accent background) so departments are visually distinct.

### Offboarding

Internal checklist surfaced when `clientStatus === 'closed'` (`pages/admin/ClientOffboarding.jsx` at `/admin/clients/:id/offboarding`; config `offboardingConfig.js` → `OFFBOARDING_SECTIONS`). State (checked items, decision fields, per-section notes, status) saved via `PUT /api/offboarding/:clientId/{check,field,note,status}`. Staff-only.

### Users / Account Management

`/admin/users` (`Users.jsx`, super_admin/manager). Set passwords directly, email reset links, activate/deactivate accounts, and cancel pending invites — all via the account-management endpoints on `/api/users`. `canActOn` protects admin targets. Team directory (`Team.jsx`) has an editable `department` field + department filter.

### Consolidated Client Files

`FileExplorer` runs in a **client-scoped mode** (all files across the client's projects + client-level files) as well as the original per-project mode. `GET /api/files/for-client/:clientId` aggregates them. UI: tile/list toggle, image lightbox, single + multi-file (zip) download, staff empty state. Client-level files have `projectId: null` + `clientId` set.

### Demo Mode

Public `/demo` (`Demo.jsx`) enters a **sessionStorage** demo — `AuthContext.enterDemo()` seeds a fixture (Summit Air HVAC, `demo/fixture.js`) and an axios request interceptor (`demo/demoApi.js`, via `config.adapter`) short-circuits portal API calls with fixture data when `demoMode()` is on. Zero component changes; no server calls; isolated from real data. `exitDemo()` clears it.

### Sales Intake (GHL / Zapier → Atlas)

`intake/intake.html` is the public sales-intake form's source copy (served live from `client/public/intake/` at `/intake/intake.html` — see `intake/README.md`; `temp/index.html` = legacy). **Flow:** the form POSTs flat snake_case JSON (`name`, `business_name`, `email`, `phone`, `sales_rep`, `packages`, `sales_notes`, `attribution`, …, plus the `billing` object — see below) to a **GHL inbound webhook** (`ORG.ghlIntakeWebhook` in `client/src/lib/org.js` — but the standalone form hard-codes its own copy since it can't import). A GHL workflow then maps those fields into a POST to `/api/clients`. The form loads its sales-rep dropdown from a separate "Beacon" roster endpoint (its own `x-api-key`).

`POST /api/clients` (`apiKeyOrAuth`) accepts **flat top-level fields** (`email`, `firstName`, `lastName`, `password`, `slackChannelId`, `clickupFolderId`, `googleMccId`, `metaAdAccountId`, `ghlSubaccountId`, `salesRep`) in addition to nested `user`/`integrations`/`billing` — so Zapier/GHL's **structured key-value fields** work with no nested JSON (raw JSON bodies break on unescaped inner quotes). `packages`/`projects` accept a space/comma string or array (`expandPackages`). `salesRep` (email or id) is resolved and assigned to the client + its projects. `normalizeClientInput` trims/limits all string input. All creates record an audit entry.

**Intake resilience:** `normalizeClientInput` (runs on every `/api/clients` POST + PUT) calls `sanitizeBilling` on any `billing` object — invalid enums are dropped, bad/blank numbers & dates → `null`, junk `parentAccountId` → `null`, `introEndsDate` is stripped (server computes it). This is deliberate: a sloppy sales-intake payload should still create the client with fields defaulted rather than 400 the whole request. `companyName` is the only hard-required field.

**Billing on intake:** the form's `billing` object maps 1:1 to `Client.billing` (package/baseAmount/billingType/billingDay/firstChargeDate/merchant/introPricing/prepaidMonths/parentAccountId). Nested `billing` in the body persists as-is (in schema). `introEndsDate` is auto-computed server-side (see Billing sub-object above). **Still TODO at the route layer:** flat billing aliases (like the integration aliases) and conditional-required enforcement (`prepaidMonths` when prepaid, `parentAccountId` when upsell).

---

## Known Patterns & Gotchas

### 1. Soft Delete

Both `Client` and `Project` use `isActive: Boolean` for soft delete. **Never use hard DELETE in routes** (except phases/tasks subdocuments which are truly removed).

- Client GET filter: `{ isActive: true }`
- Project GET filter: `{ isActive: { $ne: false } }` (different form — handles docs that predate the field)
- Client DELETE route: `findByIdAndUpdate(id, { isActive: false })`
- Project DELETE route: same

Do not add `isActive` filtering to individual `/:id` GET routes — if you have the ID you already know what you want.

### 2. Populate Helper Pattern

Every route file that has mutations defines a local `populated(id)` function:

```js
const populated = (id) =>
  Project.findById(id)
    .populate('clientId', 'companyName logoUrl bannerUrl')
    .populate('assignedStaff', 'firstName lastName email avatar');
```

Every POST/PUT/DELETE must return `await populated(entity._id)`. Do NOT return the raw `findByIdAndUpdate` result — it comes back unpopulated and breaks the frontend.

### 3. createPortal + Event Bubbling

React synthetic events bubble through the React component tree, not the DOM tree. If you render a modal via `createPortal` and there's an ancestor `onClick` (e.g. a task row that opens the modal), clicking anything inside the portal will also trigger that ancestor handler.

**Fix:** Add `e.stopPropagation()` at the portal root `<div>` onClick:
```jsx
<div onClick={(e) => e.stopPropagation()}>
  {/* portal content */}
</div>
```

This bit us when the X button on the modal was re-opening the modal rather than closing it.

### 4. Date Picker + Portal

The `react-day-picker` (or similar) date picker is portaled out via `createPortal` to escape `backdrop-filter` CSS containing blocks. Because it's not in the DOM subtree of its trigger, the standard "click outside to close" logic fails.

**Fix:** Two refs — one on the trigger button (`dateRef`), one on the portaled picker div (`datePopoverRef`). Mousedown handler checks both:
```js
if (!dateRef.current?.contains(e.target) && !datePopoverRef.current?.contains(e.target)) {
  setDateOpen(false);
}
```

### 5. backdrop-filter Containing Block

CSS `backdrop-filter` creates a new containing block / stacking context. Any child with `position: fixed` or `position: absolute` will be clipped to that ancestor instead of the viewport. This is why task dropdowns and date pickers must use `createPortal` — they're inside glass cards that have `backdrop-filter`.

### 6. Assignee Scoping

Task assignees are scoped to `project.assignedStaff` only. The `PhaseCard` passes `allStaff={project.assignedStaff ?? []}` — do NOT pass all staff from any global source. If someone isn't assigned to the project, they don't appear as an option.

### 7. pendingAssignee Pattern

In `TaskModal`, the assignee dropdown uses a "pending" pattern:
- `pendingAssignee` = local state, initialized from `task.assignedTo` when modal opens
- User clicks an assignee in the dropdown — updates `pendingAssignee` only
- On click-outside (mousedown handler), if `pendingAssignee !== savedAssigneeId` → save to DB
- `displayAssigneeId = assigneeOpen ? pendingAssignee : savedAssigneeId`

This gives instant visual feedback without a DB call per click.

### 8. Access Info Sidebar Background

Do NOT use `hsl(var(--background) / 0.6)` for any sidebar or panel background. It renders as transparent because HSL variables don't support the slash syntax the same way in all browsers. Use `rgba(0,0,0,0.12)` (dark overlay) or a solid CSS variable instead.

### 9. Service Discriminator strict:false

`Service.discriminator(...)` schemas don't need explicit `{ strict: false }` — the base `serviceOptions` handles it. When querying services, `serviceType` is the discriminator key on each document.

### 10. Staff vs Portal Users on Client

These are distinct:
- `client.assignedStaff` — agency employees who manage the account
- `client.users` — portal user accounts (role='client') who belong to this company

Both are populated on all client fetches. Don't confuse them.

### 11. user.clientId — ObjectId vs Populated Object

`user.clientId` from `GET /api/auth/me` can be either a raw ObjectId string OR a populated Client object, depending on the query path. Always extract the raw ID defensively:

```js
const clientId = typeof user?.clientId === 'object' ? user?.clientId?._id : user?.clientId;
```

This pattern appears in ClientShell, Onboarding, and anywhere a raw ID is needed for a subsequent API call.

### 12. Onboarding Module Completion vs Incomplete

Saving a module via `PUT /api/onboarding/:module` always adds the key to `completedModules` (`$addToSet`) AND `$pull`s it from `flaggedModules` (resubmit clears the flag). The frontend re-derives display state from `MODULE_REQUIRED` on every render — it does NOT remove the key from `completedModules` if data becomes insufficient. This means:
- Saving = always marks complete server-side + clears any flag
- Visual state = re-checked client-side against `MODULE_REQUIRED` on each render
- To explicitly un-complete: `DELETE /api/onboarding/:module/complete`

### 13. Onboarding route ordering

In `routes/onboarding.js`, the named PUT routes (`/flag`, `/confirm`, `/staff-notes`, `/notes`) MUST be declared before `PUT /:module`. Otherwise Express matches e.g. `/flag` against the `:module` param and rejects it as an invalid module.

Same rule holds across the newer route files: `owner.js` declares `/collections` before `/:collection`; `users.js` declares `/me/welcome` and `/pending-invites` (and `/invites/:id`) before `/:id`; `clients.js` declares `/:id/invites` and `/:id/banner-suggestion` before the bare `/:id` handlers. Add any new named route ABOVE the param catch-all.

### 15. createdAt is immutable under timestamps:true

Mongoose marks `createdAt` `immutable: true` when a schema uses `timestamps: true`, so a Mongoose `$set` on `createdAt` (even with `{ timestamps: false }`) is **silently ignored**. To backdate it (e.g. legacy import), write through the native driver: `Model.collection.updateOne({ _id }, { $set: { createdAt } })` (or pass `overwriteImmutable: true`). This bit the `--legacy` import — the first run left every row at `now`.

### 14. express-rate-limit needs trust proxy

`authLimiter` keys on `req.ip`. Behind Railway's proxy, `req.ip` is the proxy unless `app.set('trust proxy', 1)` is set in `server.js` — without it every user shares one rate-limit bucket. It IS set; don't remove it.

---

## Default Project Phases

Defined in both `CreateClientForm.jsx` and `CreateProjectForm.jsx`:

| Project Type | Phases |
|---|---|
| website | Discovery & Design → Development → Launch |
| ppc_ads | Build → Launch & Optimize |
| social_ads | Build → Launch & Optimize |
| seo | Audit → On-Page → Off-Page |
| creative | Discovery → Production |
| other | (no default phases) |

---

## Client Portal (read-only view)

`pages/client/Dashboard.jsx` — accessed at `/dashboard/projects`. Shows projects belonging to the logged-in user's `clientId`.

What's hidden from clients (stripped in `GET /api/projects/:id`):
- `internalNotes`
- `accessInfo`
- Tasks with `visibility: 'internal'`

Client portal task rows show due date (red if overdue) and assignee avatar via `MiniAvatar`.

`owner`/`super_admin` can visit `/dashboard/onboarding` to preview the client view (the "Client view" button in AdminShell is **owner-only** now, but the route still admits super_admin). The onboarding hub redirects non-clients to `/dashboard/projects`; ClientShell itself has no clientId guard so owner/super_admin can browse freely.

---

## Versioning

`vMAJOR.MINOR.PATCH`. Currently **`v1.6.x`** (past alpha; v1.0.0 = first real client portal access, now live with real clients). See **Current State (September 2026)** at the top for the v1.x change summary (billing, live ad spend, Projects sunset, internal-Slack removal). Alpha (through v0.11) shipped: Atlas brand, onboarding review, deadlines, password reset, rate limiting, API-key intake, GHL/Zapier sales intake, audit log, error boundary, `owner` role tier (record editor + impersonation), demo mode, Strategy Guide, Offboarding, Users/account-management, consolidated client files, logo/banner hierarchies, Google Business Profile module, non-expiring invites, Sales department.

**v1.x since:** external read API (`/api/accounts`) + `CampaignSnapshot` store-and-forward (Google/Meta), split external Slack + **deprecated/removed internal Slack**, **Key Accounts** (`tier`), Clients List/Tile/**Board** views + tile density/POC strip, **triage Overview** home, **strategy `budget` field type** (ga_budget/m_budget), **`Client.billing` sub-object** (billing queue schema), GHL contact deep-links, `org.js` org-ID single-source, wider CORS (apex + any `*.advancedmarketers.co`).

`v1.0.0` = first client has portal access.

Branch conventions: `feat/name`, `fix/name` off `main`. **Git is hands-off — the maintainer handles all add/commit/push.**

---

## One-time Scripts (`server/scripts/`)

Run from `server/` with: `node scripts/<name>.js`

| Script | Purpose |
|---|---|
| `seed-admin.js` | Creates the first `super_admin` user |
| `seed-dev-accounts.js` | Seeds dev/test accounts |
| `make-owner.js` | `node scripts/make-owner.js <email>` — promotes a user to `owner` |
| `change-password.js` | Resets a user's password by email |
| `backfillAccessInfo.js` | Adds `DEFAULT_ACCESS_INFO` to projects that had empty `accessInfo` (run once, June 2025 — 14/17 projects updated) |
| `backfillExpiryDates.js` | Sets `expiryDate` on existing clients via `computeExpiryDate(createdAt)`. Run once after deploying the deadline feature. |
| `backfillClientLogos.js` | Mirrors each client's first onboarding logo → `Client.onboardingLogoUrl` (already run — 17 clients). Never touches `logoUrl`. |
| `importClients.js` | Bulk-import existing clients from CSV: `node scripts/importClients.js <file.csv> [--dry-run] [--legacy] [--origin=YYYY-MM-DD]`. **Insert-only** (skips existing by name), client shell + integration IDs only, default status `live`. `--legacy` sets `legacy:true` + **rolls** `createdAt`/`updatedAt` across a window (`--origin`=`2026-01-15` → `--origin-end`=`2026-06-30`, in CSV order) so the batch spreads over earlier-this-year and sinks below newest-first lists. Backdate uses the **native driver** (`Client.collection.updateOne`) because `createdAt` is immutable under Mongoose `timestamps:true`. Template: `scripts/clients-import-template.csv`. Writes one `client.bulk_import` audit entry. |
| `revertLegacyImport.js` | Reverts the legacy import (targets `legacy:true`): `--dry-run` / `--soft` (isActive:false) / `--hard` (delete). Refuses to run without a mode; only touches `legacy:true` clients. Writes a `client.bulk_import_revert` audit entry. |
| `backfillIntegrationIds.js` | Seeds `Client.integrations.{googleMccId,metaAdAccountId}` from existing onboarding records (regex-validated, empty-fields-only). `[--dry-run]`. Ran once — 19 IDs populated across ~13 clients; 1 malformed value rejected. |
| `clients-import-template.csv` | Column template for `importClients.js` |
| `s3-cors.json` | S3 bucket CORS config (not a script — applied to the bucket) |

---

## Known Gaps (pre-v1.0)

- Client portal projects view is read-only (no messaging, no file upload from client side)
- No in-app notifications
- No pagination on the admin clients/projects lists (all loaded at once) — owner record editor + audit log ARE paginated
- Services exist in DB but service detail views are not fully built out
- `MASTER_PASSWORD` backdoor still active and can open *any* account including clients (flagged as a launch risk — kept intentionally for now)

**Closed since v0.3:** error boundary (shipped), audit trail (shipped), `manager` now has distinct account-management UI.
