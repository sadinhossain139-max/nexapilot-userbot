-- ============================================================================
-- NexaPilot Zero-Trust Admin Panel & Dynamic Settings — Idempotent SQL Migration
-- Run once in Supabase SQL Editor. Safe to re-run without data loss.
-- ============================================================================

-- 1. Multi-Admin Registry (Super Admins from ENV ADMIN_USER_IDS + Dynamic Admins)
CREATE TABLE IF NOT EXISTS public.admin_users (
    telegram_user_id  BIGINT PRIMARY KEY,
    role              TEXT NOT NULL DEFAULT 'admin', -- 'super_admin' | 'admin'
    note              TEXT,
    added_by          BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Live Dynamic ENV Settings Override Table
CREATE TABLE IF NOT EXISTS public.app_settings (
    key               TEXT PRIMARY KEY,
    value             JSONB NOT NULL,
    updated_by        BIGINT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Admin Audit Log
CREATE TABLE IF NOT EXISTS public.admin_audit_logs (
    id                BIGSERIAL PRIMARY KEY,
    admin_user_id     BIGINT NOT NULL,
    action            TEXT NOT NULL,
    target_user_id    BIGINT,
    details           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created
    ON public.admin_audit_logs (created_at DESC);

-- 4. Add is_banned column to bot_users if not present
ALTER TABLE public.bot_users
    ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
