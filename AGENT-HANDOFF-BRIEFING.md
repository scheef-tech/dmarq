# DMARQ Project - Agent Handoff Briefing

**Date:** 2026-01-24
**Status:** Partially Working - Needs Debugging
**Priority:** High - Data backfill blocked

---

## The Big Picture

Yannic manages **58 domains** across **9 Cloudflare accounts** accumulated over 3 years. The email security (SPF, DKIM, DMARC) across these domains was a mess - reports scattered to different recipients, never analyzed.

**Goal:** Centralize all DMARC reporting into one analytics platform (dmarq) to:
1. See which domains have email authentication issues
2. Identify spoofing attempts
3. Progressively tighten DMARC policies from `p=none` → `p=reject`

---

## Two-Part Solution

### Part 1: Infrastructure (DONE - Another Agent)
- Pulumi IaC at `/Users/yannic/Files/05.dev/02.repos/cloudflare-redirects-pulumi`
- All 58 domains now have DMARC records pointing to: `dmarc-reports@dmarc.scheef.tech`
- See `INFRASTRUCTURE-BRIEFING.md` for full domain list and policies

### Part 2: Analytics App (THIS PROJECT - IN PROGRESS)
- dmarq app at `/Users/yannic/Files/05.dev/02.repos/dmarq`
- Deployed to Coolify at `https://dmarq.scheef.tech`
- Application UUID: `fow044gwkkos400ccs0cg40w`

---

## Data Ingestion Methods

### Method 1: IMAP Backfill (For Historical Data)
- Gmail account: `yannic@scheef.tech`
- Contains **9000+ historical DMARC report emails**
- App connects via IMAP, downloads attachments (XML/ZIP/GZ), parses them
- Triggered on startup and via `/api/v1/admin/trigger-poll?days=N`

### Method 2: Webhook (For Future Reports)
- Cloudflare Email Routing catches emails to `dmarc-reports@dmarc.scheef.tech`
- Forwards to Cloudflare Worker → POST to `/api/v1/webhook/email`
- Webhook secret: `59fad44ba108e6e89d199c9a46712b32af53d6b8c198ea27fc229612121e63d6`

---

## What Was Working (Before My Changes)

There was a working state where:
1. IMAP backfill had successfully loaded data
2. Dashboard showed domain list with statistics
3. But clicking "Details" on a domain returned 404

The 404 was because `index.html` used `/domains/{id}` but the route was `/domain/{id}` (singular).

---

## What I Changed

### Fix 1: URL Path (commit 2a1484d)
- `backend/app/templates/index.html` line 286: `/domains/` → `/domain/`

### Fix 2: Persistent Storage (commits 7fab5df, a3783cc, 303b0c4)

**Problem:** App used in-memory storage. Every restart = all data lost.

**Solution:** Created PostgreSQL-backed storage.

Files created/modified:
- `backend/app/services/persistent_store.py` - NEW: Database-backed ReportStore
- `backend/app/models/domain.py` - Fixed duplicate index definitions
- `backend/app/models/report.py` - Fixed duplicate index definitions
- Updated imports in: `imap_client.py`, `webhook.py`, `reports.py`, `domains.py`, `main.py`

The persistent_store.py creates a `PersistentReportStore` class that:
- Uses SQLAlchemy ORM with PostgreSQL
- Has same interface as the old in-memory store
- Exports as `ReportStore` for backward compatibility

---

## Current State (BROKEN)

**Deployed:** Commit `303b0c4` is live on Coolify

**Problem:**
- Dashboard loads (200 OK)
- API works (`/api/v1/reports/domains` returns `[]`)
- But NO DATA is appearing

**Suspected Cause:**
The IMAP background poll runs on startup but data isn't being stored. Either:
1. Database connection issue (PostgreSQL not connecting properly)
2. IMAP poll failing silently
3. Some exception being swallowed

**Environment Variables (confirmed set in Coolify):**
- `IMAP_SERVER=imap.gmail.com`
- `IMAP_PORT=993`
- `IMAP_USERNAME=yannic@scheef.tech`
- `IMAP_PASSWORD=npoa mmqu mhun weqa` (Google App Password)
- `DATABASE_URL` should come from docker-compose: `postgresql://dmarq_user:dmarq_secure_password@db:5432/dmarq_db`

---

## Key Files Reference

```
backend/
├── app/
│   ├── main.py                 # FastAPI app, startup tasks, IMAP polling
│   ├── core/
│   │   ├── config.py           # Settings from env vars
│   │   └── database.py         # SQLAlchemy engine/session
│   ├── models/
│   │   ├── domain.py           # Domain model
│   │   ├── report.py           # DMARCReport, ReportRecord models
│   │   └── user.py             # User model (for auth, not used yet)
│   ├── services/
│   │   ├── persistent_store.py # NEW: PostgreSQL storage (ReportStore)
│   │   ├── imap_client.py      # IMAP connection, email parsing
│   │   └── dmarc_parser.py     # XML parsing for DMARC reports
│   ├── api/api_v1/endpoints/
│   │   ├── reports.py          # /api/v1/reports/* endpoints
│   │   ├── domains.py          # /api/v1/domains/* endpoints
│   │   └── webhook.py          # /api/v1/webhook/email endpoint
│   └── templates/
│       └── index.html          # Dashboard (Jinja2 + HTMX + DaisyUI)
├── docker-compose.yml          # PostgreSQL + App containers
└── Dockerfile
```

---

## Local Development Setup

**DO NOT keep pushing to Coolify for iterations - it's slow.**

Local setup exists at:
```bash
cd /Users/yannic/Files/05.dev/02.repos/dmarq/backend
source .venv/bin/activate

# Test with SQLite locally:
DATABASE_URL=sqlite:////tmp/test_dmarq.db python -c "
from app.services.persistent_store import PersistentReportStore
store = PersistentReportStore.get_instance()
# ... test code
"

# Run the full app locally:
DATABASE_URL=sqlite:///./local.db \
IMAP_SERVER=imap.gmail.com \
IMAP_PORT=993 \
IMAP_USERNAME=yannic@scheef.tech \
IMAP_PASSWORD='npoa mmqu mhun weqa' \
uvicorn app.main:app --reload --port 8080
```

---

## Immediate Next Steps

1. **Debug why data isn't appearing:**
   - Run app locally with logging
   - Check if IMAP connects successfully
   - Check if database tables are created
   - Check if reports are being written

2. **Once working locally:**
   - Push to GitHub (origin = scheef-tech/dmarq)
   - Coolify auto-deploys from main branch
   - Restart: Use Coolify MCP `restart_application` with UUID `fow044gwkkos400ccs0cg40w`

3. **Verify data persistence:**
   - Restart the app
   - Data should still be there (PostgreSQL volume persists)

---

## Git Remotes

```
origin = git@github.com:scheef-tech/dmarq.git  (Coolify deploys from here)
```

Other remotes were removed. Only push to `origin`.

---

## Coolify MCP Tools Available

```
mcp__coolify__restart_application  - uuid: fow044gwkkos400ccs0cg40w
mcp__coolify__list_application_envs - Check environment variables
mcp__coolify__get_deployment - Check deployment status
```

---

## Success Criteria

1. Dashboard at `https://dmarq.scheef.tech` shows domain list
2. Clicking domain shows details page (not 404)
3. Data persists across container restarts
4. Historical backfill completes (9000+ emails from Gmail)
5. Webhook receives new reports going forward

---

## Questions the User Can Answer

- Do you have container logs access in Coolify? Would help debug the IMAP issue.
- Is there a way to SSH into the server to check database directly?
- Should we add a `/health` or `/debug` endpoint to expose internal state?
