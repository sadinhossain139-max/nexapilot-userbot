-- ============================================================================
-- NexaPilot Monetization, Force-Join, Trial, Referral & Payment Migration
-- Safe & Idempotent for Existing Supabase Deployments
-- ============================================================================

-- 1. Extend bot_users with subscription, trial, force-join & referral columns
ALTER TABLE bot_users
    ADD COLUMN IF NOT EXISTS plan_type TEXT NOT NULL DEFAULT 'none'
        CHECK (plan_type IN ('none', 'trial', 'premium', 'ultra')),
    ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS trial_claimed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS channel_verified BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS referred_by BIGINT REFERENCES bot_users(owner_user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS referral_count INTEGER NOT NULL DEFAULT 0 CHECK (referral_count >= 0),
    ADD COLUMN IF NOT EXISTS referral_rewards_claimed INTEGER NOT NULL DEFAULT 0 CHECK (referral_rewards_claimed >= 0);

CREATE INDEX IF NOT EXISTS idx_bot_users_plan
    ON bot_users (plan_type, plan_expires_at);

CREATE INDEX IF NOT EXISTS idx_bot_users_referred_by
    ON bot_users (referred_by);

-- 2. Referrals Table (unique referred_user_id prevents duplicate referral farming)
CREATE TABLE IF NOT EXISTS referrals (
    id                  BIGSERIAL PRIMARY KEY,
    referrer_id         BIGINT NOT NULL REFERENCES bot_users(owner_user_id) ON DELETE CASCADE,
    referred_user_id    BIGINT NOT NULL UNIQUE REFERENCES bot_users(owner_user_id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'verified')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_referrals_no_self CHECK (referrer_id <> referred_user_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer_status
    ON referrals (referrer_id, status);

-- 3. Payment Requests Table (Multi-Method Manual Proof + Admin Approval)
CREATE TABLE IF NOT EXISTS payment_requests (
    id                  TEXT PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES bot_users(owner_user_id) ON DELETE CASCADE,
    plan_type           TEXT NOT NULL CHECK (plan_type IN ('premium', 'ultra')),
    duration_days       INTEGER NOT NULL CHECK (duration_days >= 1),
    payment_method      TEXT NOT NULL
                        CHECK (payment_method IN ('bkash', 'nagad', 'rocket', 'trx', 'usdt', 'btc', 'card')),
    amount_display      TEXT NOT NULL,
    trx_id              TEXT NOT NULL,
    proof_file_id       TEXT,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by         BIGINT,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_requests_user
    ON payment_requests (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_payment_requests_status
    ON payment_requests (status, created_at DESC);
