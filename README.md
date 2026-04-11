# CivicBlocks

An open source civic feedback platform for verified, registered Iowa voters to submit feedback on their elected representatives. An AI layer aggregates feedback into transparent geographic summaries by district and Census block group. Every verified voter gets equal weight.

**Status:** Architecture complete, no production code deployed. Building toward an Iowa pilot of 500 verified users across 10+ legislative districts.

**License:** MIT

---

## How It Works

CivicBlocks has two distinct data layers:

```
PRIVATE LAYER                        PUBLIC LAYER
(only the user can read this)        (anyone can read, no account required)

Individual feedback (encrypted)  ──▶  District summaries
Voter identity (E2E encrypted)   AI   Participation rates
Account data                    wall  Top themes by district
Account data                         Average ratings
                                      Public REST API
```

The AI summarization step is a permanent, one-way wall. Individual data goes in encrypted; aggregate insight comes out. Plaintext is discarded immediately after summarization. What persists is only the summary — never the source.

---

## Core Principles

These are non-negotiable. They are the product.

1. **Equal weighting per verified voter** — one person submitting 10 times counts as one voice. Ten different verified voters raising the same concern counts as ten voices.
2. **Radical individual privacy** — end-to-end encryption means only the user can read their own data. Not the operator, not the DBA.
3. **Radically public aggregates** — everything after the AI summarization layer is fully public. No account required.
4. **Full transparency** — codebase, AI prompts, aggregation methodology, and privacy architecture are all open source and auditable.
5. **Minimal footprint** — collect only what is necessary, retain only what is essential, delete completely when asked.
6. **Voter file integrity** — the Iowa voter file is read-only. CivicBlocks never modifies it.
7. **One person, one voice** — one account per `voter_id`, enforced at the database level.

---

## Tech Stack

| Layer | Tool |
|---|---|
| Database | Supabase (PostgreSQL + pgvector + PostGIS) |
| Auth | Supabase Auth (magic link) |
| Encryption | Web Crypto API (browser-native, client-side only) |
| Key derivation | Argon2 / PBKDF2 — never stored |
| Recovery | BIP39 12-word phrase — never stored |
| Address matching | pgvector nearest-neighbor (embeddings via Anthropic API) |
| Identity verification | USPS postcard via Lob.com (~$0.68/user) |
| AI summarization | Anthropic Claude API (weekly batch, ephemeral plaintext) |
| Geo data | Census TIGER/Line shapefiles |
| Geocoding | Census Geocoder API + Nominatim fallback |
| Hosting | Vercel + Supabase (free tier for pilot) |

---

## Repository Structure

```
civicblocks/
├── CLAUDE.md                        ← AI assistant context and architecture guide
├── README.md                        ← this file
├── README_PRIVACY.md                ← detailed privacy architecture
├── ARCHITECTURE.md                  ← system design decisions
├── LICENSE
├── .env.example
├── requirements.txt
├── schema.sql                       ← full database schema with RLS
├── scripts/
│   ├── ingest_voter_file.py
│   ├── geocode_addresses.py
│   ├── spatial_join_districts.py
│   ├── build_address_embeddings.py
│   ├── send_verification_postcard.py
│   └── demo.py
└── lib/
    ├── match_voter.py
    └── crypto.py
```

---

## Getting Started (Local Development)

No real Iowa voter file is required to run locally — synthetic test data is sufficient for development.

### Prerequisites

- Python 3.11+
- A free [Supabase](https://supabase.com) project with the `pgvector` and `PostGIS` extensions enabled
- API keys for Anthropic and Lob (see `.env.example`)

### Setup

```bash
git clone https://github.com/civicblocks/civicblocks.git
cd civicblocks
pip install poetry
poetry install
cp .env.example .env
# Fill in your Supabase URL, service role key, and other values in .env
```

### Apply the schema

```bash
# Run schema.sql against your Supabase project via the SQL editor,
# or use psql directly:
psql $DATABASE_URL -f schema.sql
```

### Run with synthetic data

```bash
# Run the end-to-end demo (no API keys required beyond DATABASE_URL)
poetry run python scripts/demo.py

# Or run the ingestion script directly
poetry run python scripts/ingest_voter_file.py --synthetic
```

---

## Build Phases

| Phase | Focus |
|---|---|
| 1 | Data foundation — voter file ingestion, geocoding, districts, pgvector embeddings |
| 2 | Identity & matching — auth, postcard verification, E2E encryption skeleton |
| 3 | Feedback & AI — submission, summarization, public dashboard |
| 4 | Polish & launch — deletion flows, privacy docs, meetup pitch |

**POC goal:** Complete Phase 1 and Phase 2 (working voter match + postcard verification) before touching feedback or AI.

---

## Security Notes

The client-side encryption implementation (`client/crypto.js`, not yet written) requires review by someone with prior Web Crypto API experience and a paid security audit (~$5–15k) before any public launch. Do not skip this.

See [README_PRIVACY.md](README_PRIVACY.md) for the full encryption architecture.

---

## ⚠️ Legal & Compliance

**This project is not yet cleared for production use.** The following must be resolved first:

- **Iowa Code §48A.39** — legal opinion needed confirming voter file use for civic/election purposes is permitted (~$500 consult, lawyer identified)
- **Iowa HHS ethics clearance** — required before production deployment
- **USPS postcard permissibility** — must confirm postcard verification is permitted under Iowa voter file use restrictions

These are not blockers for local development and architecture work. They must be resolved before any real voter data is loaded or any public-facing service is launched.

---

## Contributing

CivicBlocks welcomes contributors, especially those with experience in:

- PostgreSQL / PostGIS / pgvector
- Web Crypto API and client-side encryption
- Python data pipelines (Pandas, GeoPandas)
- Iowa civic/election law

Please read [CLAUDE.md](CLAUDE.md) before contributing — it contains the full architecture guide and the non-negotiable design principles that govern all code in this repository.

---

*CivicBlocks — Iowa Pilot — [MIT License](LICENSE)*
