-- White-label branding: widen the singleton branding row (migration 006)
-- with an optional inline-data-URI logo and the operator-specific legal
-- identity fields that the impressum/datenschutz pages used to carry as
-- bracketed placeholders. Defaults are neutral so a fresh install carries
-- no company identity at all.
--
-- The CHECK (id = 1) singleton constraint from 006 is preserved; this
-- migration only ADD COLUMNs and adjusts defaults, never touches the PK.

-- New columns. All TEXT NOT NULL DEFAULT '' so existing rows backfill to
-- empty without a separate UPDATE and the API layer can treat "" as unset.
ALTER TABLE branding ADD COLUMN IF NOT EXISTS logo_data_uri        TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS company_legal_name   TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS company_address      TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS company_representative TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS contact_email        TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS privacy_email        TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS register_info        TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS vat_id               TEXT NOT NULL DEFAULT '';
ALTER TABLE branding ADD COLUMN IF NOT EXISTS supervisory_authority TEXT NOT NULL DEFAULT '';

-- Neutral defaults for the original triplet. New installs that insert a bare
-- row get the white-label identity instead of the old Stilbezirk seed.
ALTER TABLE branding ALTER COLUMN app_name     SET DEFAULT 'Wissensplattform';
ALTER TABLE branding ALTER COLUMN sub_brand    SET DEFAULT '';
ALTER TABLE branding ALTER COLUMN accent_color SET DEFAULT '#2563EB';

-- Re-neutralise an existing install that still holds the exact Stilbezirk
-- seed. Guarded so an operator who has already customised any of these three
-- values is never overwritten (idempotent: a second run matches nothing).
UPDATE branding
   SET app_name     = 'Wissensplattform',
       sub_brand    = '',
       accent_color = '#2563EB'
 WHERE id = 1
   AND app_name     = 'stilbezirk'
   AND sub_brand    = 'wissen'
   AND accent_color = '#6DDD15';

-- db.apply_migrations() also records this filename's stem in schema_migrations
-- after the file runs; the explicit insert keeps the file self-contained and
-- idempotent if applied out of band (matches the 001_init precedent).
INSERT INTO schema_migrations(version) VALUES ('014_branding_whitelabel')
    ON CONFLICT (version) DO NOTHING;
