-- schema.046.sql -- Theories and Predictions (docs/epistemy.md).
--
-- Two new Artifact kinds, ``Theory`` (a parametric law with its fit) and
-- ``Prediction`` (a quantile forecast frozen before its run), four Belief
-- columns (the earned confidence and the review state), and six edge kinds:
-- predicts, tests, rivals, fits, assumes, about.
--
-- Purely additive: new nullable columns, two sequences, and widened kind CHECKs.
-- No existing row changes, so every CHECK validates instantly. Safe to apply
-- ahead of the restart: the old build never writes the new kinds or columns.
--
-- The per-kind column CHECKs are named here (``inquiries_<column>_check``)
-- where a fresh install leaves them anonymous; their definitions are the ones
-- the generated block produces, which ``schema_migration_test`` checks.

-- Per-kind ref sequences.
CREATE SEQUENCE IF NOT EXISTS seq_theory START 1;
CREATE SEQUENCE IF NOT EXISTS seq_prediction START 1;

-- Kind-specific columns on inquiries.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS belief_earned_confidence DOUBLE PRECISION;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS belief_needs_review BOOLEAN;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS belief_reviewed_at TIMESTAMPTZ;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS belief_review_note TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_explains TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_form TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_variables JSONB;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_parameters JSONB;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_regime TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_fit_ref TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS theory_record JSONB;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS prediction_quantity JSONB;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS prediction_quantiles JSONB;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS prediction_made_by TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS prediction_override_of TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS prediction_registered TIMESTAMPTZ;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS prediction_resolution JSONB;
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_belief_earned_confidence_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_belief_earned_confidence_check CHECK (
        CASE WHEN kind = 'Belief'
            THEN true
         AND (belief_earned_confidence IS NULL OR belief_earned_confidence >= 0 AND belief_earned_confidence <= 1)
            ELSE belief_earned_confidence IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_belief_needs_review_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_belief_needs_review_check CHECK (
        CASE WHEN kind = 'Belief'
            THEN true
            ELSE belief_needs_review IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_belief_reviewed_at_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_belief_reviewed_at_check CHECK (
        CASE WHEN kind = 'Belief'
            THEN true
            ELSE belief_reviewed_at IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_belief_review_note_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_belief_review_note_check CHECK (
        CASE WHEN kind = 'Belief'
            THEN true
            ELSE belief_review_note IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_explains_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_explains_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_explains IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_form_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_form_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_form IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_variables_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_variables_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_variables IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_parameters_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_parameters_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_parameters IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_regime_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_regime_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_regime IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_fit_ref_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_fit_ref_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_fit_ref IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_theory_record_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_theory_record_check CHECK (
        CASE WHEN kind = 'Theory'
            THEN true
            ELSE theory_record IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_prediction_quantity_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_prediction_quantity_check CHECK (
        CASE WHEN kind = 'Prediction'
            THEN true
            ELSE prediction_quantity IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_prediction_quantiles_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_prediction_quantiles_check CHECK (
        CASE WHEN kind = 'Prediction'
            THEN true
            ELSE prediction_quantiles IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_prediction_made_by_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_prediction_made_by_check CHECK (
        CASE WHEN kind = 'Prediction'
            THEN true
         AND (prediction_made_by IS NULL OR prediction_made_by IN ('theory', 'agent', 'house-prior'))
            ELSE prediction_made_by IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_prediction_override_of_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_prediction_override_of_check CHECK (
        CASE WHEN kind = 'Prediction'
            THEN true
            ELSE prediction_override_of IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_prediction_registered_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_prediction_registered_check CHECK (
        CASE WHEN kind = 'Prediction'
            THEN true
            ELSE prediction_registered IS NULL
        END
    );
ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_prediction_resolution_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_prediction_resolution_check CHECK (
        CASE WHEN kind = 'Prediction'
            THEN true
            ELSE prediction_resolution IS NULL
        END
    );

ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_kind_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_kind_check CHECK (kind IN ({inquiry_kinds}));

-- The change_log mirrors of the editable new columns.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_belief_earned_confidence DOUBLE PRECISION;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_belief_earned_confidence DOUBLE PRECISION;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_belief_needs_review BOOLEAN;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_belief_needs_review BOOLEAN;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_belief_reviewed_at TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_belief_reviewed_at TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_belief_review_note TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_belief_review_note TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_explains TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_explains TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_form TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_form TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_variables JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_variables JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_parameters JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_parameters JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_regime TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_regime TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_fit_ref TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_fit_ref TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_theory_record JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_theory_record JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_prediction_resolution JSONB;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_prediction_resolution JSONB;
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_belief_earned_confidence_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_belief_earned_confidence_check
    CHECK (old_belief_earned_confidence IS NULL OR (old_belief_earned_confidence >= 0 AND old_belief_earned_confidence <= 1));
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_belief_earned_confidence_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_belief_earned_confidence_check
    CHECK (new_belief_earned_confidence IS NULL OR (new_belief_earned_confidence >= 0 AND new_belief_earned_confidence <= 1));

-- The kind matrix: each field-change kind populates only its own mirror pair,
-- and only on its own kind's rows.
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_belief_earned_confidence_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_belief_earned_confidence_kind_check
    CHECK (kind = 'belief_earned_confidence' OR old_belief_earned_confidence IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_belief_earned_confidence_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_belief_earned_confidence_kind_check
    CHECK (kind = 'belief_earned_confidence' OR new_belief_earned_confidence IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_belief_earned_confidence_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_belief_earned_confidence_subject_check
    CHECK (kind <> 'belief_earned_confidence' OR subject_kind = 'Belief');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_belief_needs_review_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_belief_needs_review_kind_check
    CHECK (kind = 'belief_needs_review' OR old_belief_needs_review IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_belief_needs_review_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_belief_needs_review_kind_check
    CHECK (kind = 'belief_needs_review' OR new_belief_needs_review IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_belief_needs_review_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_belief_needs_review_subject_check
    CHECK (kind <> 'belief_needs_review' OR subject_kind = 'Belief');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_belief_reviewed_at_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_belief_reviewed_at_kind_check
    CHECK (kind = 'belief_reviewed_at' OR old_belief_reviewed_at IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_belief_reviewed_at_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_belief_reviewed_at_kind_check
    CHECK (kind = 'belief_reviewed_at' OR new_belief_reviewed_at IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_belief_reviewed_at_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_belief_reviewed_at_subject_check
    CHECK (kind <> 'belief_reviewed_at' OR subject_kind = 'Belief');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_belief_review_note_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_belief_review_note_kind_check
    CHECK (kind = 'belief_review_note' OR old_belief_review_note IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_belief_review_note_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_belief_review_note_kind_check
    CHECK (kind = 'belief_review_note' OR new_belief_review_note IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_belief_review_note_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_belief_review_note_subject_check
    CHECK (kind <> 'belief_review_note' OR subject_kind = 'Belief');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_explains_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_explains_kind_check
    CHECK (kind = 'theory_explains' OR old_theory_explains IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_explains_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_explains_kind_check
    CHECK (kind = 'theory_explains' OR new_theory_explains IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_explains_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_explains_subject_check
    CHECK (kind <> 'theory_explains' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_form_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_form_kind_check
    CHECK (kind = 'theory_form' OR old_theory_form IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_form_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_form_kind_check
    CHECK (kind = 'theory_form' OR new_theory_form IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_form_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_form_subject_check
    CHECK (kind <> 'theory_form' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_variables_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_variables_kind_check
    CHECK (kind = 'theory_variables' OR old_theory_variables IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_variables_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_variables_kind_check
    CHECK (kind = 'theory_variables' OR new_theory_variables IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_variables_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_variables_subject_check
    CHECK (kind <> 'theory_variables' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_parameters_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_parameters_kind_check
    CHECK (kind = 'theory_parameters' OR old_theory_parameters IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_parameters_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_parameters_kind_check
    CHECK (kind = 'theory_parameters' OR new_theory_parameters IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_parameters_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_parameters_subject_check
    CHECK (kind <> 'theory_parameters' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_regime_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_regime_kind_check
    CHECK (kind = 'theory_regime' OR old_theory_regime IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_regime_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_regime_kind_check
    CHECK (kind = 'theory_regime' OR new_theory_regime IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_regime_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_regime_subject_check
    CHECK (kind <> 'theory_regime' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_fit_ref_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_fit_ref_kind_check
    CHECK (kind = 'theory_fit_ref' OR old_theory_fit_ref IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_fit_ref_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_fit_ref_kind_check
    CHECK (kind = 'theory_fit_ref' OR new_theory_fit_ref IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_fit_ref_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_fit_ref_subject_check
    CHECK (kind <> 'theory_fit_ref' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_theory_record_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_theory_record_kind_check
    CHECK (kind = 'theory_record' OR old_theory_record IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_theory_record_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_theory_record_kind_check
    CHECK (kind = 'theory_record' OR new_theory_record IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_theory_record_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_theory_record_subject_check
    CHECK (kind <> 'theory_record' OR subject_kind = 'Theory');
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_old_prediction_resolution_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_prediction_resolution_kind_check
    CHECK (kind = 'prediction_resolution' OR old_prediction_resolution IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_new_prediction_resolution_kind_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_prediction_resolution_kind_check
    CHECK (kind = 'prediction_resolution' OR new_prediction_resolution IS NULL);
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_prediction_resolution_subject_check;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_prediction_resolution_subject_check
    CHECK (kind <> 'prediction_resolution' OR subject_kind = 'Prediction');

ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_kind_check,
    ADD CONSTRAINT change_log_kind_check CHECK (kind IN ({change_kinds}));
ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_subject_kind_check,
    ADD CONSTRAINT change_log_subject_kind_check CHECK (subject_kind IN ({inquiry_kinds}));
ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_old_peer_kind_check,
    ADD CONSTRAINT change_log_old_peer_kind_check
        CHECK (old_peer_kind IS NULL OR old_peer_kind IN ({inquiry_kinds}));
ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_new_peer_kind_check,
    ADD CONSTRAINT change_log_new_peer_kind_check
        CHECK (new_peer_kind IS NULL OR new_peer_kind IN ({inquiry_kinds}));
ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_old_peer_edge_kind_check,
    ADD CONSTRAINT change_log_old_peer_edge_kind_check
        CHECK (old_peer_edge_kind IS NULL OR old_peer_edge_kind IN ({edge_kinds}));
ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_new_peer_edge_kind_check,
    ADD CONSTRAINT change_log_new_peer_edge_kind_check
        CHECK (new_peer_edge_kind IS NULL OR new_peer_edge_kind IN ({edge_kinds}));

-- The edge-validity CHECK is anonymous in every database (``edges_checkN``), so
-- find it by the clause only it carries and replace it with the widened one.
DO $$
DECLARE c TEXT;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'edges'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%cites_paper%'
    LOOP
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', c);
    END LOOP;
END $$;
ALTER TABLE edges
    ADD CONSTRAINT edges_kind_endpoints_check CHECK (
        (edge_kind = 'narrows'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     OR (edge_kind = 'requires'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     OR (edge_kind = 'produced_by'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     OR (edge_kind IN ('proves', 'favors')
            AND from_kind IN ({artifact_kinds})
            AND to_kind IN ({claimable_kinds}))
     OR (edge_kind = 'supersedes'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     OR (edge_kind = 'cites_paper'
            AND from_kind IN ({paper_kinds})
            AND to_kind IN ({paper_kinds}))
     OR (edge_kind = 'predicts'
            AND from_kind = 'Prediction' AND to_kind = 'Theory')
     OR (edge_kind = 'tests'
            AND from_kind = 'Experiment' AND to_kind = 'Prediction')
     OR (edge_kind = 'rivals'
            AND from_kind = 'Theory' AND to_kind = 'Theory')
     OR (edge_kind = 'fits'
            AND from_kind = 'Experiment' AND to_kind = 'Theory')
     OR (edge_kind = 'assumes'
            AND from_kind = 'Belief' AND to_kind = 'Belief')
     OR (edge_kind = 'about'
            AND from_kind = 'Belief' AND to_kind = 'Theory')
    );
