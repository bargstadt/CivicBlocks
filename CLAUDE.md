# CLAUDE.md — CivicBlocks

> Paste this file into the root of the `civicblocks/` repo. Claude Code will read it automatically at the start of every session.

---

## What Is This Project

CivicBlocks is an open source civic feedback platform for verified, registered Iowa voters to submit feedback on their elected representatives. An AI layer aggregates feedback into transparent geographic summaries by district and Census block group. Every verified voter gets equal weight — this is a non-negotiable core design principle.

**Current status:** Architecture complete, no code written yet. Building toward an Iowa pilot of 500 verified users across 10+ legislative districts.

**License:** MIT  
**GitHub:** github.com/civicblocks (to be created)

---

## North Star Principles — Non-Negotiable

These are not preferences. Do not suggest changes to them. Flag any code that violates them immediately.

1. **Equal weighting per verified voter** — one person submitting 10 times = 1 voice. Ten different verified voters raising the same concern = 10 voices (real signal). The AI summarization prompt enforces this explicitly. Never write code that counts raw submission volume.

2. **Radical individual privacy** — end-to-end encryption means only the user can read their own data. Not even the operator. Individual data is completely private. Never write code that exposes plaintext individual feedback server-side.

3. **Radically public aggregates** — everything after the AI summarization layer is fully public. No account required, no login, no paywall. `ai_summaries`, `districts_geo`, `representatives`, and `block_groups` tables all have public read RLS.

4. **Full transparency** — codebase, AI prompts, aggregation methodology, and privacy architecture are all open source and auditable.

5. **Minimal footprint** — collect only what is necessary, retain only what is essential, delete completely when asked.

6. **Voter file integrity** — the Iowa voter file is a read-only government dataset. CivicBlocks never modifies it. We only manage the link between a user account and a voter record. The raw CSV is archived after ingestion and never served by the app.

7. **One person, one voice** — one account per `voter_id`, enforced at the database level with a unique constraint. No exceptions.

---

## Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| Database | Supabase (PostgreSQL + pgvector + PostGIS) | Free tier for Iowa pilot |
| Auth | Supabase Auth (magic link) | Handles login + RLS enforcement |
| Encryption | Web Crypto API (browser-native) | No library — client-side only |
| Key derivation | Argon2 / PBKDF2 | Derives encryption key from password — **never stored** |
| Recovery | BIP39 12-word phrase | Generated at signup — **never stored** |
| Address matching | pgvector nearest-neighbor | Embeddings via Anthropic API |
| Identity (Tier 1) | USPS postcard via Lob.com | ~$0.68/user — launch mechanism |
| Identity (Tier 2) | Stripe Identity | ~$1.50/user — future high-stakes features |
| Data pipeline | Python (Pandas, GeoPandas, psycopg2) | All scripts in `/scripts/` |
| AI summarization | Anthropic Claude API | Weekly batch, ephemeral plaintext |
| Geo data | Census TIGER/Line shapefiles | Free, public domain |
| Geocoding | Census Geocoder API + Nominatim fallback | Both free, no key required |
| Frontend | Defer — Supabase built-in tools for POC | Add Next.js when real users demand it |
| Hosting | Vercel + Supabase | Both free tier to start |

---

## Two-Layer Data Model — Critical Design Principle

```
PRIVATE LAYER                        PUBLIC LAYER
(only the user can read this)        (anyone can read this, no account required)

Individual feedback (encrypted)  ──▶  District summaries
Voter identity (E2E encrypted)   AI   Participation rates
Personal civic score            wall  Top themes by district
Account data                         Average ratings
                                      Historical archive
                                      Public REST API
```

**The AI summarization step is a permanent, one-way wall.** Individual data goes in encrypted, aggregate insight comes out. Plaintext is discarded immediately after summarization. What persists is only the summary — never the source.

---

## Repo Structure

```
civicblocks/
├── CLAUDE.md                        ← this file
├── README.md
├── README_PRIVACY.md
├── ARCHITECTURE.md
├── LICENSE (MIT)
├── .env.example
├── requirements.txt
├── schema.sql
├── prompts/
│   └── weekly_summary.txt           ← AI prompt, fully auditable
├── scripts/
│   ├── ingest_voter_file.py
│   ├── geocode_addresses.py
│   ├── spatial_join_districts.py
│   ├── build_address_embeddings.py
│   ├── send_verification_postcard.py
│   └── weekly_ai_summary.py
├── lib/
│   ├── match_voter.py
│   ├── civic_score.py
│   └── crypto.py                    ← key derivation utilities (Python side)
└── client/                          ← add when frontend is built
    └── crypto.js                    ← Web Crypto API implementation
```

---

## Database Schema

Full schema lives in `schema.sql`. Key tables and constraints:

### `voters` (read-only — loaded from Iowa voter file)
```sql
voter_id        TEXT PRIMARY KEY
last_name       TEXT NOT NULL
first_name      TEXT NOT NULL
address         TEXT NOT NULL
city            TEXT NOT NULL
zip             TEXT NOT NULL
party           TEXT
vote_history    JSONB           -- past election participation only, not how they voted
geom            GEOMETRY(Point, 4326)  -- PostGIS, populated after geocoding
block_group_id  TEXT REFERENCES block_groups(block_group_id)
address_embedding VECTOR(1536)  -- pgvector, for address matching
```

### `users`
```sql
user_id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
email_hash           TEXT NOT NULL UNIQUE  -- SHA-256 only, never plaintext
voter_id             TEXT UNIQUE REFERENCES voters(voter_id)  -- UNIQUE enforces one account per voter
encrypted_profile    BYTEA                 -- client-side encrypted blob
civic_score          INTEGER DEFAULT 0
account_status       TEXT DEFAULT 'active'  -- active | orphaned | pending_deletion
verification_status  TEXT DEFAULT 'unverified'  -- unverified | postcard_sent | verified
postcard_code_hash   TEXT                  -- SHA-256 of the 6-digit code, never plaintext
postcard_sent_at     TIMESTAMP
deletion_requested_at TIMESTAMP
verified_at          TIMESTAMP
created_at           TIMESTAMP DEFAULT now()
```

### `feedback`
```sql
feedback_id      UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id          UUID REFERENCES users(user_id)
rep_id           TEXT REFERENCES representatives(rep_id)
encrypted_body   BYTEA     -- client-side encrypted, only user can read
rating           INTEGER CHECK (rating BETWEEN 1 AND 5)
topic_tags       TEXT[]    -- coarse enough to be non-identifying
submitted_at     TIMESTAMP DEFAULT now()
```

### `representatives`
```sql
rep_id       TEXT PRIMARY KEY
name         TEXT NOT NULL
level        TEXT    -- federal | state | local
chamber      TEXT
party        TEXT
district_id  TEXT REFERENCES districts_geo(district_id)
term_end     DATE
```

### `districts_geo`
```sql
district_id    TEXT PRIMARY KEY
district_type  TEXT    -- 'US House' | 'IA Senate' | 'IA House' | etc.
district_name  TEXT
geom           GEOMETRY(Polygon, 4326)
rep_id         TEXT REFERENCES representatives(rep_id)
```

### `block_groups`
```sql
block_group_id  TEXT PRIMARY KEY
geoid           TEXT  -- Census GEOID
geom            GEOMETRY(Polygon, 4326)
state_fips      TEXT
county_fips     TEXT
tract           TEXT
block_group     TEXT
```

### `user_districts` (junction table)
```sql
user_id      UUID REFERENCES users(user_id)
district_id  TEXT REFERENCES districts_geo(district_id)
PRIMARY KEY (user_id, district_id)
```

### `ai_summaries`
```sql
summary_id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
rep_id              TEXT REFERENCES representatives(rep_id)
geo_level           TEXT    -- 'block_group' | 'district' | 'state'
geo_id              TEXT
summary_text        TEXT    -- plaintext — this is the public-facing output
top_themes          TEXT[]
avg_rating          FLOAT
participation_count INTEGER
generated_at        TIMESTAMP DEFAULT now()
```

### `civic_score_events`
```sql
event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id     UUID REFERENCES users(user_id)
event_type  TEXT
points      INTEGER
created_at  TIMESTAMP DEFAULT now()
```

### Row Level Security Rules
```
voters             → read-only for all authenticated users
representatives    → public read (no auth required)
districts_geo      → public read (no auth required)
block_groups       → public read (no auth required)
ai_summaries       → public read (no auth required)
users              → authenticated users can only read/write their own row
feedback           → authenticated users can only read/write their own rows
civic_score_events → authenticated users can only read their own rows
```

---

## Authentication & Verification Flow

```
Step 1 — Email signup
  User provides email → Supabase Auth creates account → magic link sent

Step 2 — Voter file matching
  User enters name + address
  → Stage 1: exact match on ZIP + street number
  → Stage 2: pgvector embedding search if <3 results
  → Top 3 candidates returned (first name, last name, city, ZIP — PARTIAL ONLY)
  → User selects and confirms — NEVER auto-confirmed programmatically
  → Fields NEVER returned: voter_id, full address, party, vote_history, geom

Step 3 — USPS postcard verification (Tier 1 — launch)
  System sends postcard to matched voter file address via Lob.com
  → Postcard contains unique 6-digit code
  → User enters code in app → account activated
  → voter_id locked to account (unique constraint)
  → BIP39 12-word recovery phrase generated and issued (hard gate — user must acknowledge)
  → Postcard codes expire after 30 days → resend flow required

Step 4 — Stripe Identity (Tier 2 — future, not in POC)
  Document scan + selfie via Stripe
  → CivicBlocks never sees the license
  → Stripe returns verified/not-verified signal
```

---

## E2E Encryption Architecture

**Goal: only the user can read their own data. Not the operator, not the DBA, not a subpoena recipient.**

### At registration:
1. User sets password
2. BIP39 12-word recovery phrase generated — user must acknowledge (hard gate, not a checkbox)
3. Encryption key derived from password using Argon2 — **never stored anywhere**
4. Sensitive profile data encrypted client-side using Web Crypto API before leaving the browser
5. Only the encrypted blob reaches the database

### At login:
1. Password re-derives the same key locally in the browser
2. Encrypted data fetched and decrypted in the browser
3. Server and database only ever hold ciphertext — plaintext never touches the server

### For feedback submissions:
1. Feedback text encrypted with user's key before submission
2. Only ciphertext stored in the database
3. For AI summarization: decrypted in a transient ephemeral compute context
4. Summaries generated, plaintext discarded, only aggregate summary persists

### What this makes impossible:
- Operator reading individual feedback
- A database breach exposing readable data
- Government data requests yielding useful individual data

### ⚠️ Security warning for contributors:
Do not ship client-side crypto without review from someone who has done it before. The `client/crypto.js` implementation needs a dedicated security-focused contributor and a paid audit (~$5-15k) before public launch.

---

## Voter Matching Architecture

### Two-stage approach:
1. **Fast filter** — exact match on ZIP + street number → small candidate set
2. **Embedding fallback** — if Stage 1 returns <3 results, embed user's input and run pgvector nearest-neighbor search against pre-computed `address_embedding` column

### Address normalization (apply before embedding):
- Expand abbreviations: `St→Street`, `Ave→Avenue`, `Blvd→Boulevard`, `Dr→Drive`, `Rd→Road`, `Apt→Apartment`
- Uppercase and trim whitespace
- Normalize format: `"{first_name} {last_name} {address} {city} {zip}"`

### Hard rules:
- **NEVER auto-confirm a match** — human confirmation always required
- **NEVER return** `voter_id`, full address, party, vote_history, geom, or `address_embedding` in match results
- Return top 3 candidates showing: first name, last name, partial address, city, ZIP only

---

## AI Summarization

Weekly batch process in `scripts/weekly_ai_summary.py`:
1. For each representative, gather all feedback submitted since last summary
2. Group by district and block group
3. For each group, call Anthropic Claude API with equal-weighting prompt
4. Store summary + themes + avg_rating + participation_count in `ai_summaries`
5. **Discard all plaintext immediately after summary generation**

The prompt lives in `prompts/weekly_summary.txt` — open source and auditable. Key excerpt:

```
WEIGHTING RULES — non-negotiable:
- Each verified voter account counts as exactly one voice, regardless of 
  how many submissions they have made.
- Ten different verified constituents raising the same concern carries real 
  weight — surface it prominently.
- If one person raises a unique concern no one else shares, surface it 
  proportionally as a minority view.
- You are detecting genuine shared sentiment among distinct verified voters, 
  not counting raw submission volume.

Flag explicitly if fewer than 10 unique verified voters submitted.
Do not publish summaries for reps with fewer than 5 submissions.
```

---

## Civic Score

```python
SCORE_EVENTS = {
    "account_verified":      10,
    "first_feedback":        20,
    "feedback_submitted":     5,  # max 2/month/rep to prevent farming
    "multi_district_engaged": 15, # engaged with 3+ of their reps
    "consistent_voice":      25,  # feedback in 3 consecutive months
    "voter_history_5":       20,  # voted in 5+ past elections (from voter file)
    "voter_history_10":      35,  # voted in 10+ past elections
}
```

Score is visible to the user. Individual scores are never publicly exposed. District participation rates (% of registered voters with active accounts) are public — this is the primary adoption mechanic.

---

## Account Recovery & Deletion

### Recovery paths:
- **Lost password, have phrase** → re-derive encryption key, set new password, full restoration
- **Lost password AND phrase** → email-verified deletion only (see below)

### Email-verified deletion (always available):
1. User requests deletion → confirmation email sent
2. User clicks confirm → 48-hour delay before execution
3. During window: cancellation email sent daily (protects against unauthorized deletion)
4. On execution: linking row dropped, key reference dropped, encrypted data permanently unreadable, `voter_id` freed for re-registration

**The Iowa voter file record is never touched.** Only the CivicBlocks link row is deleted.

### MFA recommendation (not in POC, add before public launch):
Require TOTP for: account deletion initiation, recovery phrase viewing/regeneration, changing registered email.

---

## Build Phases

| Phase | Focus | Timeline |
|---|---|---|
| 1 | Data foundation — voter file ingestion, geocoding, districts, pgvector embeddings | Weeks 1–4 |
| 2 | Identity & matching — auth, postcard verification, E2E encryption skeleton | Weeks 5–8 |
| 3 | Feedback & AI — submission, summarization, public dashboard | Weeks 9–12 |
| 4 | Polish & launch — deletion flows, privacy docs, meetup pitch | Weeks 13–16 |

**POC goal:** Get through Phase 1 and Phase 2 with a working voter match + postcard verification flow before touching feedback or AI.

---

## Environment Variables (`.env.example`)

```bash
# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_ANON_KEY=

# Anthropic
ANTHROPIC_API_KEY=

# Lob (USPS postcards)
LOB_API_KEY=

# Iowa voter file path (local only — never committed)
VOTER_FILE_PATH=./data/iowa_voter_file.csv

# Geocoding (no keys needed — Census API and Nominatim are free)
GEOCODING_BATCH_SIZE=100
GEOCODING_DELAY_MS=200
```

---

## Legal & Compliance Flags

⚠️ **Do not write code for production use until these are resolved:**

- Iowa Code §48A.39 legal opinion — voter file permitted use for civic/election purposes (lawyer contact identified, ~$500 consult)
- Iowa HHS ethics officer clearance — required before writing code as an HHS employee
- USPS postcard verification permissibility under Iowa voter file use restrictions

These are not blockers for local development and architecture work, but flag them prominently in any public documentation.

---

## How to Work With Me

- **Build one script at a time.** Explain what you're doing. Pause for confirmation before moving to the next.
- **Ask clarifying questions before writing any code** if anything is ambiguous.
- **Flag security concerns immediately** — especially anything touching the encryption layer, voter file handling, or voter_id exposure.
- **Never suggest shortcuts** on the privacy architecture or equal-weighting rules. These are the product.
- **Start here for POC:** `schema.sql` first, then `README.md`, then `ingest_voter_file.py`. After each file is confirmed, move to the next.
- Synthetic test data is available for development — you do not need a real Iowa voter file to start.

---

*CivicBlocks North Star v1.3 — Iowa Pilot — Last updated April 2026*
