-- CivicBlocks schema
-- Supabase (PostgreSQL) — requires uuid-ossp, postgis, pgvector extensions
-- North Star: equal weighting per verified voter, radical individual privacy,
--             radically public aggregates.

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Public / aggregate layer tables
-- (No circular FK issue — these have no FK to representatives)
-- ---------------------------------------------------------------------------

CREATE TABLE block_groups (
    block_group_id  TEXT PRIMARY KEY,
    geoid           TEXT,
    geom            GEOMETRY(Polygon, 4326),
    state_fips      TEXT,
    county_fips     TEXT,
    tract           TEXT,
    block_group     TEXT
);

-- districts_geo.rep_id is intentionally left as plain TEXT (no FK) to avoid
-- a circular reference with representatives. Add the FK constraint once both
-- tables are populated with synthetic/real data and the relationship is stable.
CREATE TABLE districts_geo (
    district_id    TEXT PRIMARY KEY,
    district_type  TEXT,    -- 'US House' | 'IA Senate' | 'IA House' | etc.
    district_name  TEXT,
    geom           GEOMETRY(Polygon, 4326),
    rep_id         TEXT     -- references representatives(rep_id) — FK deferred
);

CREATE TABLE representatives (
    rep_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    level        TEXT,    -- 'federal' | 'state' | 'local'
    chamber      TEXT,
    party        TEXT,
    district_id  TEXT REFERENCES districts_geo(district_id),
    term_end     DATE
);

-- ---------------------------------------------------------------------------
-- Voter file table (read-only — loaded from Iowa voter file CSV)
-- Never modified by the application. Raw CSV archived after ingestion.
-- ---------------------------------------------------------------------------

CREATE TABLE voters (
    voter_id          TEXT PRIMARY KEY,
    last_name         TEXT NOT NULL,
    first_name        TEXT NOT NULL,
    address           TEXT NOT NULL,
    city              TEXT NOT NULL,
    zip               TEXT NOT NULL,
    party             TEXT,
    vote_history      JSONB,   -- past election participation only, never how they voted
    geom              GEOMETRY(Point, 4326),
    block_group_id    TEXT REFERENCES block_groups(block_group_id),
    address_embedding VECTOR(1536)   -- pgvector, pre-computed for address matching
);

-- ---------------------------------------------------------------------------
-- Private layer tables
-- ---------------------------------------------------------------------------

-- CRITICAL: voter_id UNIQUE enforces one account per registered voter (North Star #7)
CREATE TABLE users (
    user_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_hash            TEXT NOT NULL UNIQUE,  -- SHA-256 only, never plaintext
    voter_id              TEXT UNIQUE REFERENCES voters(voter_id),  -- one account per voter
    encrypted_profile     BYTEA,                 -- client-side encrypted blob, server never sees plaintext
    -- civic_score column deferred — add when gamification is implemented
    account_status        TEXT DEFAULT 'active'
                              CHECK (account_status IN ('active', 'orphaned', 'pending_deletion')),
    verification_status   TEXT DEFAULT 'unverified'
                              CHECK (verification_status IN ('unverified', 'postcard_sent', 'verified')),
    postcard_code_hash    TEXT,                  -- SHA-256 of 6-digit code, never plaintext
    postcard_sent_at      TIMESTAMP WITH TIME ZONE,
    deletion_requested_at TIMESTAMP WITH TIME ZONE,
    verified_at           TIMESTAMP WITH TIME ZONE,
    created_at            TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- encrypted_body: client-side encrypted before submission — server never holds plaintext feedback
CREATE TABLE feedback (
    feedback_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID REFERENCES users(user_id),
    rep_id         TEXT REFERENCES representatives(rep_id),
    encrypted_body BYTEA,    -- only the user can decrypt this
    rating         INTEGER CHECK (rating BETWEEN 1 AND 5),
    topic_tags     TEXT[],   -- coarse tags, non-identifying
    submitted_at   TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE user_districts (
    user_id     UUID REFERENCES users(user_id),
    district_id TEXT REFERENCES districts_geo(district_id),
    PRIMARY KEY (user_id, district_id)
);

-- civic_score_events table deferred — add when gamification is implemented

-- ---------------------------------------------------------------------------
-- Public aggregate layer
-- The AI summarization step is a one-way wall. Individual plaintext is
-- discarded immediately after summarization. Only the summary persists.
-- NEVER backtrack from this table to individual feedback.
-- ---------------------------------------------------------------------------

CREATE TABLE ai_summaries (
    summary_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rep_id              TEXT REFERENCES representatives(rep_id),
    geo_level           TEXT CHECK (geo_level IN ('block_group', 'district', 'state')),
    geo_id              TEXT,
    summary_text        TEXT,    -- plaintext — this is the public-facing output
    top_themes          TEXT[],
    avg_rating          FLOAT,
    participation_count INTEGER, -- unique verified voter count, NOT raw submission count
    generated_at        TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Spatial indexes for district assignment and geocoding lookups
CREATE INDEX idx_voters_geom          ON voters          USING GIST(geom);
CREATE INDEX idx_districts_geo_geom   ON districts_geo   USING GIST(geom);
CREATE INDEX idx_block_groups_geom    ON block_groups    USING GIST(geom);

-- pgvector index for address embedding nearest-neighbor search
-- lists = 1500 targets ~2.2M Iowa registered voters (pgvector rule: sqrt(n) for n > 1M)
-- Rebuild this index after bulk ingestion — ivfflat indexes on empty tables are useless
CREATE INDEX idx_voters_address_embedding ON voters USING ivfflat (address_embedding vector_cosine_ops)
    WITH (lists = 1500);

-- Common lookup indexes
CREATE INDEX idx_voters_zip           ON voters           (zip);
CREATE INDEX idx_feedback_user_id     ON feedback         (user_id);
CREATE INDEX idx_feedback_rep_id      ON feedback         (rep_id);
CREATE INDEX idx_ai_summaries_rep_id  ON ai_summaries     (rep_id);
CREATE INDEX idx_ai_summaries_geo     ON ai_summaries     (geo_level, geo_id);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE voters             ENABLE ROW LEVEL SECURITY;
ALTER TABLE users              ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback           ENABLE ROW LEVEL SECURITY;
ALTER TABLE representatives    ENABLE ROW LEVEL SECURITY;
ALTER TABLE districts_geo      ENABLE ROW LEVEL SECURITY;
ALTER TABLE block_groups       ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_districts     ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_summaries       ENABLE ROW LEVEL SECURITY;

-- Public read — no auth required (North Star #3)
CREATE POLICY "public read representatives" ON representatives
    FOR SELECT USING (true);

CREATE POLICY "public read districts_geo" ON districts_geo
    FOR SELECT USING (true);

CREATE POLICY "public read block_groups" ON block_groups
    FOR SELECT USING (true);

CREATE POLICY "public read ai_summaries" ON ai_summaries
    FOR SELECT USING (true);

-- voters — authenticated read only, no writes from the app
CREATE POLICY "authenticated read voters" ON voters
    FOR SELECT TO authenticated USING (true);

-- users — each user can only access their own row
CREATE POLICY "users own row select" ON users
    FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "users own row update" ON users
    FOR UPDATE TO authenticated USING (auth.uid() = user_id);

-- feedback — each user can only access their own rows
CREATE POLICY "feedback own rows select" ON feedback
    FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "feedback own rows insert" ON feedback
    FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

-- user_districts — each user can only access their own rows
CREATE POLICY "user_districts own rows select" ON user_districts
    FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "user_districts own rows insert" ON user_districts
    FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

-- civic_score_events RLS deferred — add when gamification is implemented
