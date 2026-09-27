-- Paira security migration for the EXISTING Supabase database
-- Run once in Supabase SQL Editor before starting this GitHub-ready version.

-- Werkzeug password hashes are longer than the original VARCHAR(100).
ALTER TABLE Users
ALTER COLUMN password TYPE TEXT;

-- The Flask server connects directly to PostgreSQL. Prevent browser-facing
-- Supabase roles from directly accessing the application's tables.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;

-- Existing plaintext passwords are intentionally not rewritten here.
-- On each user's next successful login, app.py automatically upgrades
-- that legacy plaintext password to a secure Werkzeug hash.
