CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    discord_thread_id VARCHAR(64) UNIQUE NOT NULL,
    mode VARCHAR(32) NOT NULL DEFAULT 'dnd',
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS turn_logs (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    actor VARCHAR(64) NOT NULL,
    user_input TEXT NOT NULL,
    ai_raw_output TEXT NOT NULL,
    accepted_commands JSONB NOT NULL DEFAULT '[]'::jsonb,
    rejected_commands JSONB NOT NULL DEFAULT '[]'::jsonb,
    narration TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    discord_message_id VARCHAR(64) NOT NULL,
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    comment TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS players (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    discord_user_id VARCHAR(64) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_player_campaign_discord_user UNIQUE (campaign_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS characters (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    name VARCHAR(128) NOT NULL,
    role VARCHAR(64) NOT NULL DEFAULT 'adventurer',
    hp INTEGER NOT NULL DEFAULT 10,
    max_hp INTEGER NOT NULL DEFAULT 10,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    item_states JSONB NOT NULL DEFAULT '{}'::jsonb,
    effects JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id SERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    item_key VARCHAR(128) NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_inventory_player_item UNIQUE (player_id, item_key)
);

CREATE TABLE IF NOT EXISTS campaign_rules (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    rule_key VARCHAR(128) NOT NULL,
    rule_value TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_campaign_rule_key UNIQUE (campaign_id, rule_key)
);

CREATE TABLE IF NOT EXISTS agency_rule_blocks (
    id SERIAL PRIMARY KEY,
    rule_id VARCHAR(128) UNIQUE NOT NULL,
    title VARCHAR(200) NOT NULL,
    priority VARCHAR(32) NOT NULL DEFAULT 'high',
    body TEXT NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sys_admin_users (
    id SERIAL PRIMARY KEY,
    discord_user_id VARCHAR(64) UNIQUE NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS learned_relevance (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    item_key VARCHAR(128) NOT NULL,
    context_tag VARCHAR(64) NOT NULL,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_learned_relevance_triplet UNIQUE (campaign_id, item_key, context_tag)
);

CREATE TABLE IF NOT EXISTS global_learned_relevance (
    id SERIAL PRIMARY KEY,
    item_key VARCHAR(128) NOT NULL,
    context_tag VARCHAR(64) NOT NULL,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_global_learned_relevance_pair UNIQUE (item_key, context_tag)
);

CREATE TABLE IF NOT EXISTS item_knowledge (
    id SERIAL PRIMARY KEY,
    item_key VARCHAR(128) UNIQUE NOT NULL,
    canonical_name VARCHAR(200) NOT NULL DEFAULT '',
    object_type VARCHAR(64) NOT NULL DEFAULT 'unknown',
    portability VARCHAR(32) NOT NULL DEFAULT 'unknown',
    rarity VARCHAR(32) NOT NULL DEFAULT 'unknown',
    summary TEXT NOT NULL DEFAULT '',
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    observation_count INTEGER NOT NULL DEFAULT 0,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS item_observations (
    id SERIAL PRIMARY KEY,
    item_key VARCHAR(128) NOT NULL,
    turn_log_id INTEGER REFERENCES turn_logs(id) ON DELETE SET NULL,
    observation_text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS global_effect_relevance (
    id SERIAL PRIMARY KEY,
    effect_key VARCHAR(128) NOT NULL,
    context_tag VARCHAR(64) NOT NULL,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_global_effect_relevance_pair UNIQUE (effect_key, context_tag)
);

CREATE TABLE IF NOT EXISTS effect_knowledge (
    id SERIAL PRIMARY KEY,
    effect_key VARCHAR(128) UNIQUE NOT NULL,
    canonical_name VARCHAR(200) NOT NULL DEFAULT '',
    category VARCHAR(32) NOT NULL DEFAULT 'misc',
    summary TEXT NOT NULL DEFAULT '',
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    observation_count INTEGER NOT NULL DEFAULT 0,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS effect_observations (
    id SERIAL PRIMARY KEY,
    effect_key VARCHAR(128) NOT NULL,
    turn_log_id INTEGER REFERENCES turn_logs(id) ON DELETE SET NULL,
    observation_text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS campaign_snapshots (
    id SERIAL PRIMARY KEY,
    source_campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    created_by_discord_user_id VARCHAR(64) NOT NULL,
    scope VARCHAR(64) NOT NULL DEFAULT 'all',
    snapshot_key VARCHAR(64) UNIQUE NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_campaign_snapshot_key UNIQUE (snapshot_key)
);

CREATE TABLE IF NOT EXISTS campaign_memory_summaries (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    start_turn_id INTEGER NOT NULL,
    end_turn_id INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS game_rulesets (
    id SERIAL PRIMARY KEY,
    key VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(128) NOT NULL,
    system VARCHAR(64) NOT NULL DEFAULT 'dnd',
    version VARCHAR(64) NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    is_official BOOLEAN NOT NULL DEFAULT FALSE,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    rules_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rulebooks (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(128) UNIQUE NOT NULL,
    title VARCHAR(200) NOT NULL,
    system VARCHAR(64) NOT NULL DEFAULT 'dnd',
    version VARCHAR(64) NOT NULL DEFAULT '',
    source VARCHAR(200) NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rulebook_entries (
    id SERIAL PRIMARY KEY,
    rulebook_id INTEGER NOT NULL REFERENCES rulebooks(id) ON DELETE CASCADE,
    entry_key VARCHAR(128) NOT NULL,
    title VARCHAR(200) NOT NULL,
    section VARCHAR(200) NOT NULL DEFAULT '',
    page_ref VARCHAR(64) NOT NULL DEFAULT '',
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    content TEXT NOT NULL DEFAULT '',
    searchable_text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_rulebook_entry_key UNIQUE (rulebook_id, entry_key)
);

CREATE TABLE IF NOT EXISTS dice_roll_logs (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    actor_discord_user_id VARCHAR(64) NOT NULL DEFAULT '',
    actor_display_name VARCHAR(128) NOT NULL DEFAULT '',
    expression VARCHAR(64) NOT NULL DEFAULT '',
    normalized_expression VARCHAR(64) NOT NULL DEFAULT '',
    sides INTEGER NOT NULL DEFAULT 20,
    roll_count INTEGER NOT NULL DEFAULT 1,
    modifier INTEGER NOT NULL DEFAULT 0,
    advantage_mode VARCHAR(16) NOT NULL DEFAULT 'none',
    total INTEGER NOT NULL DEFAULT 0,
    breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(128) UNIQUE NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    password_salt VARCHAR(64) NOT NULL,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    discord_user_id VARCHAR(64),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_auth_user_username UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS auth_roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_auth_role_name UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS auth_permissions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_auth_permission_name UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS auth_user_roles (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES auth_roles(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_auth_user_role_pair UNIQUE (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS auth_role_permissions (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES auth_roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES auth_permissions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_auth_role_permission_pair UNIQUE (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS system_logs (
    id SERIAL PRIMARY KEY,
    service VARCHAR(64) NOT NULL,
    level VARCHAR(32) NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    source VARCHAR(64) NOT NULL DEFAULT 'runtime',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id SERIAL PRIMARY KEY,
    actor_source VARCHAR(32) NOT NULL DEFAULT 'unknown',
    actor_id VARCHAR(128) NOT NULL DEFAULT '',
    actor_display VARCHAR(128) NOT NULL DEFAULT '',
    action VARCHAR(128) NOT NULL,
    target VARCHAR(256) NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_configs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) UNIQUE NOT NULL,
    discord_token TEXT NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
