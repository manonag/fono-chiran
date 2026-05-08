-- CHIRAN Phase A: Profile layer migration
-- Adds profiles table and links projects to owner + collaborators.
-- Schema only. No tool changes. No behavior changes for existing sessions.
-- Author: Mano (via Claude Code)
-- Date: 2026-05-08
--
-- Note: the projects table uses column "id" as the slug (text PK),
-- not "slug". Every reference below to a project is keyed by id.

BEGIN;

-- 1. Create profiles table
CREATE TABLE profiles (
    profile_id   SERIAL PRIMARY KEY,
    slug         TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('human', 'system')),
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_profiles_slug ON profiles(slug);

-- 2. Seed human profiles
INSERT INTO profiles (slug, display_name, kind) VALUES
    ('mano',    'Mano (Venkata Nagireddi)',         'human'),
    ('madhu',   'Madhu (Vagdevi Nagireddi)',        'human'),
    ('teja',    'Venkata Guna Teja Siripurapu',     'human'),
    ('sarat',   'Kanaka Sarat Siripurapu',          'human'),
    ('mourya',  'Thanish Venkata Mourya Nagireddi', 'human'),
    ('mayukha', 'Mayukha Nagireddi',                'human');

-- 3. Add ownership columns to projects (nullable initially for backfill)
ALTER TABLE projects
    ADD COLUMN owner_profile_id INTEGER REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    ADD COLUMN collaborator_profile_ids JSONB DEFAULT '[]'::jsonb NOT NULL;

-- 4. Backfill: every project owned by Mano
UPDATE projects
SET owner_profile_id = (SELECT profile_id FROM profiles WHERE slug = 'mano');

-- 5. Backfill collaborators for projects with explicit profile interest
UPDATE projects SET collaborator_profile_ids = '["teja"]'::jsonb    WHERE id = 'naukri';
UPDATE projects SET collaborator_profile_ids = '["sarat"]'::jsonb   WHERE id = 'examprep';
UPDATE projects SET collaborator_profile_ids = '["mourya"]'::jsonb  WHERE id = 'mourya';
UPDATE projects SET collaborator_profile_ids = '["mayukha"]'::jsonb WHERE id = 'mayukha';
UPDATE projects SET collaborator_profile_ids = '["madhu"]'::jsonb   WHERE id = 'madhu';
UPDATE projects SET collaborator_profile_ids = '["teja"]'::jsonb    WHERE id = 'teja';

-- 6. Verify backfill: zero NULL owners
DO $$
DECLARE
    null_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO null_count FROM projects WHERE owner_profile_id IS NULL;
    IF null_count > 0 THEN
        RAISE EXCEPTION 'Backfill incomplete: % projects have NULL owner_profile_id', null_count;
    END IF;
END $$;

-- 7. Lock down owner_profile_id
ALTER TABLE projects ALTER COLUMN owner_profile_id SET NOT NULL;

-- 8. Indexes for Phase B query patterns
CREATE INDEX idx_projects_owner ON projects(owner_profile_id);
CREATE INDEX idx_projects_collaborators ON projects USING gin(collaborator_profile_ids);

COMMIT;
