BEGIN;

CREATE OR REPLACE FUNCTION gab_crm_notify_investor_events()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN

        IF NEW.temperature = 'hot' THEN
            INSERT INTO gab_crm_notifications
                (investor_id, event_type, title, message)
            VALUES (
                NEW.id,
                'investor_became_hot',
                'Горячий инвестор',
                format('Инвестор «%s» отмечен как горячий.', NEW.name)
            )
            ON CONFLICT (investor_id, event_type)
                WHERE read_at IS NULL
            DO NOTHING;
        END IF;

        IF NEW.qualified IS TRUE THEN
            INSERT INTO gab_crm_notifications
                (investor_id, event_type, title, message)
            VALUES (
                NEW.id,
                'investor_qualified',
                'Инвестор квалифицирован',
                format(
                    'Инвестор «%s» набрал %s из 5 критериев.',
                    NEW.name,
                    NEW.qualification_score
                )
            )
            ON CONFLICT (investor_id, event_type)
                WHERE read_at IS NULL
            DO NOTHING;
        END IF;

    ELSE

        IF NEW.temperature = 'hot'
           AND OLD.temperature IS DISTINCT FROM 'hot' THEN
            INSERT INTO gab_crm_notifications
                (investor_id, event_type, title, message)
            VALUES (
                NEW.id,
                'investor_became_hot',
                'Горячий инвестор',
                format('Инвестор «%s» отмечен как горячий.', NEW.name)
            )
            ON CONFLICT (investor_id, event_type)
                WHERE read_at IS NULL
            DO NOTHING;
        END IF;

        IF NEW.qualified IS TRUE
           AND OLD.qualified IS DISTINCT FROM TRUE THEN
            INSERT INTO gab_crm_notifications
                (investor_id, event_type, title, message)
            VALUES (
                NEW.id,
                'investor_qualified',
                'Инвестор квалифицирован',
                format(
                    'Инвестор «%s» набрал %s из 5 критериев.',
                    NEW.name,
                    NEW.qualification_score
                )
            )
            ON CONFLICT (investor_id, event_type)
                WHERE read_at IS NULL
            DO NOTHING;
        END IF;

    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_gab_crm_investor_notifications
ON gab_crm_investors;

CREATE TRIGGER trg_gab_crm_investor_notifications
AFTER INSERT OR UPDATE OF
    temperature,
    has_budget_source,
    interested_in_asset,
    horizon_within_6_months,
    decision_maker_access,
    agreed_next_step
ON gab_crm_investors
FOR EACH ROW
EXECUTE FUNCTION gab_crm_notify_investor_events();

COMMIT;
