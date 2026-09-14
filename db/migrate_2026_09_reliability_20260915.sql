-- Preserve legacy integer attribution if any; never manufacture UUID mappings.
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='nwt_opportunity_outcomes'
            AND column_name='source_decision_id' AND data_type='bigint') THEN
  ALTER TABLE nwt_opportunity_outcomes RENAME COLUMN source_decision_id TO legacy_source_decision_id;
  ALTER TABLE nwt_opportunity_outcomes ADD COLUMN source_decision_id UUID;
 END IF;
END $$;
ALTER TABLE nwt_opportunity_outcomes ADD COLUMN IF NOT EXISTS reconstructed_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS nwt_entry_intents (
 intent_key TEXT PRIMARY KEY, ticket_id UUID UNIQUE NOT NULL REFERENCES nwt_tickets(ticket_id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE nwt_entry_intents IS 'Conservative once-per-session strategy intent reservation; never auto-delete on ambiguous submission';
