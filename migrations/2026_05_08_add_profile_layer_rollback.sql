-- Rollback for: 2026_05_08_add_profile_layer.sql
-- Reverts profile layer. Data in profiles table is dropped.

BEGIN;

DROP INDEX IF EXISTS idx_projects_collaborators;
DROP INDEX IF EXISTS idx_projects_owner;

ALTER TABLE projects DROP COLUMN IF EXISTS collaborator_profile_ids;
ALTER TABLE projects DROP COLUMN IF EXISTS owner_profile_id;

DROP INDEX IF EXISTS idx_profiles_slug;
DROP TABLE IF EXISTS profiles;

COMMIT;
