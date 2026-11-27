-- ============================================================================
-- NexaPilot — Multi-User Telegram Userbot Automation Server
-- Clean, Normalized & Idempotent Supabase PostgreSQL Schema + Migration
-- Safe to run on both fresh databases and existing deployments.
-- ============================================================================

-- 0. Helper trigger function for automatic updated_at timestamps
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 1. Bot Control Panel Users (Multi-User Isolation & Per-User Account Limits)
CREATE TABLE IF NOT EXISTS bot_users (
    owner_user_id       BIGINT PRIMARY KEY,
    username            TEXT,
    first_name          TEXT,
    max_accounts        INTEGER NOT NULL DEFAULT 1 CHECK (max_accounts >= 1),
    is_banned           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_bot_users_updated_at ON bot_users;
CREATE TRIGGER trg_bot_users_updated_at
    BEFORE UPDATE ON bot_users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 2. Endpoint & Bot OTP-Authenticated Userbot Sessions
CREATE TABLE IF NOT EXISTS user_sessions (
    telegram_user_id    BIGINT PRIMARY KEY,
    owner_user_id       BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    phone_number        TEXT,
    username            TEXT,
    session_string      TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    mention_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    mention_template_id TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_sessions
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_user_sessions_owner_active
    ON user_sessions (owner_user_id, is_active);

DROP TRIGGER IF EXISTS trg_user_sessions_updated_at ON user_sessions;
CREATE TRIGGER trg_user_sessions_updated_at
    BEFORE UPDATE ON user_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 3. Manual / Pasted Session-String Accounts
CREATE TABLE IF NOT EXISTS manual_sessions (
    id                  BIGSERIAL PRIMARY KEY,
    owner_user_id       BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    session_string      TEXT NOT NULL,
    label               TEXT,
    telegram_user_id    BIGINT,
    username            TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE manual_sessions
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_manual_sessions_active
    ON manual_sessions (is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_manual_sessions_owner_active
    ON manual_sessions (owner_user_id, is_active);

DROP TRIGGER IF EXISTS trg_manual_sessions_updated_at ON manual_sessions;
CREATE TRIGGER trg_manual_sessions_updated_at
    BEFORE UPDATE ON manual_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 4. Unified Per-Account Automation Settings (Mention, Welcome, Offline)
CREATE TABLE IF NOT EXISTS user_settings (
    telegram_user_id          BIGINT PRIMARY KEY,
    owner_user_id             BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    mention_enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    mention_template_id       TEXT,
    welcome_enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    welcome_template_id       TEXT,
    offline_enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    offline_mode              TEXT NOT NULL DEFAULT 'manual'
                              CHECK (offline_mode IN ('manual', 'auto')),
    offline_template_id       TEXT,
    offline_cooldown_seconds  INTEGER NOT NULL DEFAULT 300
                              CHECK (offline_cooldown_seconds >= 10),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_user_settings_owner
    ON user_settings (owner_user_id);

DROP TRIGGER IF EXISTS trg_user_settings_updated_at ON user_settings;
CREATE TRIGGER trg_user_settings_updated_at
    BEFORE UPDATE ON user_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Backfill legacy mention settings from user_sessions if any
INSERT INTO user_settings (telegram_user_id, owner_user_id, mention_enabled, mention_template_id)
SELECT telegram_user_id, owner_user_id, COALESCE(mention_enabled, FALSE), mention_template_id
FROM user_sessions
ON CONFLICT (telegram_user_id) DO NOTHING;

-- 5. Welcomed DM Senders (First-Time Welcome Deduplication)
CREATE TABLE IF NOT EXISTS welcomed_users (
    telegram_user_id    BIGINT NOT NULL,
    sender_user_id      BIGINT NOT NULL,
    owner_user_id       BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    welcomed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (telegram_user_id, sender_user_id)
);

ALTER TABLE welcomed_users
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_welcomed_users_owner
    ON welcomed_users (telegram_user_id);

-- 6. Message Templates (UTF-16 Entities + Premium Custom Emoji IDs)
CREATE TABLE IF NOT EXISTS templates (
    id                  TEXT PRIMARY KEY,
    telegram_user_id    BIGINT NOT NULL,
    owner_user_id       BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    text                TEXT NOT NULL,
    entities_json       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE templates
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_templates_user
    ON templates (telegram_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_templates_owner
    ON templates (owner_user_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_templates_updated_at ON templates;
CREATE TRIGGER trg_templates_updated_at
    BEFORE UPDATE ON templates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 7. Keyword Auto-Reply Rules
CREATE TABLE IF NOT EXISTS keyword_replies (
    id                  BIGSERIAL PRIMARY KEY,
    telegram_user_id    BIGINT NOT NULL,
    owner_user_id       BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    trigger_text        TEXT NOT NULL,
    template_id         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE keyword_replies
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_keyword_replies_user
    ON keyword_replies (telegram_user_id);

CREATE INDEX IF NOT EXISTS idx_keyword_replies_owner
    ON keyword_replies (owner_user_id);

DROP TRIGGER IF EXISTS trg_keyword_replies_updated_at ON keyword_replies;
CREATE TRIGGER trg_keyword_replies_updated_at
    BEFORE UPDATE ON keyword_replies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 8. Scheduled Posts (One-Time & Recurring)
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id                  BIGSERIAL PRIMARY KEY,
    telegram_user_id    BIGINT NOT NULL,
    owner_user_id       BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    template_id         TEXT NOT NULL,
    target_chat_id      BIGINT NOT NULL,
    run_at              TIMESTAMPTZ,
    interval_minutes    INTEGER,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE scheduled_posts
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_scheduled_posts_active
    ON scheduled_posts (is_active, telegram_user_id)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_scheduled_posts_owner
    ON scheduled_posts (owner_user_id, is_active);

DROP TRIGGER IF EXISTS trg_scheduled_posts_updated_at ON scheduled_posts;
CREATE TRIGGER trg_scheduled_posts_updated_at
    BEFORE UPDATE ON scheduled_posts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 9. Campaigns (Buy/Sell & Telegram Folder Broadcasts)
CREATE TABLE IF NOT EXISTS campaigns (
    id                      TEXT PRIMARY KEY,
    telegram_user_id        BIGINT NOT NULL,
    owner_user_id           BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    name                    TEXT NOT NULL,
    template_id             TEXT NOT NULL,
    target_mode             TEXT NOT NULL DEFAULT 'buysell'
                            CHECK (target_mode IN ('buysell', 'folder', 'keywords', 'folder_buysell')),
    folder_name             TEXT,
    keywords                JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_groups              INTEGER NOT NULL DEFAULT 0 CHECK (max_groups >= 0),
    delay_seconds           INTEGER NOT NULL DEFAULT 15 CHECK (delay_seconds >= 5),
    repeat_interval_minutes INTEGER NOT NULL DEFAULT 0 CHECK (repeat_interval_minutes >= 0),
    target_groups           JSONB NOT NULL DEFAULT '[]'::jsonb,
    status                  TEXT NOT NULL DEFAULT 'paused'
                            CHECK (status IN ('running', 'paused', 'blocked', 'completed', 'stopped')),
    total_sent              INTEGER NOT NULL DEFAULT 0,
    total_failed            INTEGER NOT NULL DEFAULT 0,
    last_run_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_campaigns_user
    ON campaigns (telegram_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_campaigns_owner
    ON campaigns (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_campaigns_running
    ON campaigns (status)
    WHERE status = 'running';

DROP TRIGGER IF EXISTS trg_campaigns_updated_at ON campaigns;
CREATE TRIGGER trg_campaigns_updated_at
    BEFORE UPDATE ON campaigns
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 10. Drop any legacy FKs on feature tables pointing to user_sessions
--     so manual_sessions accounts operate seamlessly alongside OTP sessions.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT conname, conrelid::regclass AS table_name
        FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid = 'public.user_sessions'::regclass
          AND conrelid::regclass::text IN (
              'templates', 'keyword_replies', 'scheduled_posts',
              'public.templates', 'public.keyword_replies', 'public.scheduled_posts'
          )
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT IF EXISTS %I', r.table_name, r.conname);
    END LOOP;
END $$;
