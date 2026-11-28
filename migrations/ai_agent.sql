-- ============================================================================
-- NexaPilot Autonomous AI Agent — Idempotent Supabase SQL Migration
-- Run once in Supabase SQL Editor. Safe to re-run without data loss.
-- ============================================================================

-- 1. Per-user AI Agent configuration, persona, rules & 7 granular permissions
CREATE TABLE IF NOT EXISTS public.agent_configs (
    owner_user_id        BIGINT PRIMARY KEY,
    enabled              BOOLEAN NOT NULL DEFAULT FALSE,
    auto_reply_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    agent_name           TEXT NOT NULL DEFAULT 'Nexa Agent',
    tone_style           TEXT NOT NULL DEFAULT 'professional',
    response_language    TEXT NOT NULL DEFAULT 'auto',
    system_prompt        TEXT NOT NULL DEFAULT '',
    custom_rules         TEXT NOT NULL DEFAULT '',
    excluded_chats       JSONB NOT NULL DEFAULT '[]'::jsonb,
    voice_reply_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    tts_voice            TEXT NOT NULL DEFAULT 'bn-BD-PradeepNeural',

    -- Granular Per-User Permissions (ALL default FALSE until explicitly enabled)
    perm_reply_private   BOOLEAN NOT NULL DEFAULT FALSE,
    perm_send_private    BOOLEAN NOT NULL DEFAULT FALSE,
    perm_post_groups     BOOLEAN NOT NULL DEFAULT FALSE,
    perm_post_channels   BOOLEAN NOT NULL DEFAULT FALSE,
    perm_react           BOOLEAN NOT NULL DEFAULT FALSE,
    perm_control_server  BOOLEAN NOT NULL DEFAULT FALSE,
    perm_billing_actions BOOLEAN NOT NULL DEFAULT FALSE,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Agent Tool Execution & Audit Log (strictly scoped by owner_user_id)
CREATE TABLE IF NOT EXISTS public.agent_action_logs (
    id                BIGSERIAL PRIMARY KEY,
    owner_user_id     BIGINT NOT NULL,
    trigger_source    TEXT NOT NULL DEFAULT 'bot_chat', -- bot_chat | miniapp_chat | autonomous_incoming
    tool_name         TEXT NOT NULL,
    arguments_summary TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'success',  -- success | denied | error
    result_summary    TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_action_logs_owner_created
    ON public.agent_action_logs (owner_user_id, created_at DESC);

-- 3. Persistent One-Time Scheduled Agent Messages (reloaded into APScheduler on startup)
CREATE TABLE IF NOT EXISTS public.agent_scheduled_tasks (
    id                BIGSERIAL PRIMARY KEY,
    owner_user_id     BIGINT NOT NULL,
    telegram_user_id  BIGINT,
    target_chat       TEXT NOT NULL,
    message_text      TEXT NOT NULL,
    run_at            TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | cancelled
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_scheduled_tasks_pending
    ON public.agent_scheduled_tasks (status, run_at);

CREATE INDEX IF NOT EXISTS idx_agent_scheduled_tasks_owner
    ON public.agent_scheduled_tasks (owner_user_id, created_at DESC);
