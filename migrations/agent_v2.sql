-- ============================================================================
-- NexaPilot AI Agent v2 — Standalone Idempotent SQL Migration
-- Adds perm_manage_account (Join/Leave, Profile Editing, Bot Interaction)
-- ============================================================================

ALTER TABLE public.agent_configs
    ADD COLUMN IF NOT EXISTS perm_manage_account BOOLEAN NOT NULL DEFAULT FALSE;
