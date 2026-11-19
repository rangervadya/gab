BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
ALTER TABLE gab_crm_import_batches ADD COLUMN IF NOT EXISTS worker_token TEXT;
ALTER TABLE gab_crm_import_batches DROP CONSTRAINT IF EXISTS gab_crm_import_batches_file_extension_check;
ALTER TABLE gab_crm_import_batches ADD CONSTRAINT gab_crm_import_batches_file_extension_check
    CHECK (file_extension IN ('pdf','xls','xlsx','doc','docx','jpg','jpeg','png','tif','tiff','csv','pptx'));
ALTER TABLE gab_crm_import_rows
    ADD COLUMN IF NOT EXISTS emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS phones JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS field_sources JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS extraction_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS manual_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS edit_version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS gab_crm_import_edits (
    id BIGSERIAL PRIMARY KEY,
    import_row_id BIGINT NOT NULL REFERENCES gab_crm_import_rows(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    changes JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gab_crm_import_edits_row_idx
    ON gab_crm_import_edits(import_row_id, created_at DESC);
GRANT SELECT, INSERT ON gab_crm_import_edits TO gab_review_user;
GRANT USAGE, SELECT ON SEQUENCE gab_crm_import_edits_id_seq TO gab_review_user;

-- Apply manual values after every write, including n8n upserts. raw_data remains
-- the latest unedited extraction. Confirmed records cannot be rewritten by OCR.
CREATE OR REPLACE FUNCTION gab_preserve_import_edits() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE f TEXT;
BEGIN
    IF OLD.import_status IN ('created', 'matched', 'rejected') THEN
        RETURN OLD;
    END IF;
    FOREACH f IN ARRAY ARRAY['company_name','inn','city','full_name','position',
        'phone','email','revenue_rub','revenue_year','revenue_source','notes'] LOOP
        IF NEW.manual_overrides ? f THEN
            NEW := jsonb_populate_record(NEW, jsonb_build_object(f, NEW.manual_overrides -> f));
            NEW.field_sources := NEW.field_sources || jsonb_build_object(f, 'manual');
        END IF;
    END LOOP;
    IF NEW.manual_overrides ? 'email' THEN
        NEW.emails := CASE WHEN NEW.email IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(NEW.email) END;
    END IF;
    IF NEW.manual_overrides ? 'phone' THEN
        NEW.phones := CASE WHEN NEW.phone IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(NEW.phone) END;
    END IF;
    IF NEW.manual_overrides ? 'revenue_rub' THEN
        NEW.revenue_status := CASE WHEN NEW.revenue_rub IS NULL THEN 'unknown'
            WHEN NEW.revenue_rub >= 100000000 THEN 'eligible' ELSE 'below_threshold' END;
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN
        NEW.edit_version := OLD.edit_version + 1;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS gab_preserve_import_edits ON gab_crm_import_rows;
CREATE TRIGGER gab_preserve_import_edits BEFORE UPDATE ON gab_crm_import_rows
    FOR EACH ROW EXECUTE FUNCTION gab_preserve_import_edits();
COMMIT;
