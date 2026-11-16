"""Frozen, ordered diet DDL shared by Alembic and isolated integration tests."""

DIET_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE diet_user_state (
      user_id BIGINT PRIMARY KEY CHECK (user_id > 0),
      timezone TEXT NOT NULL,
      diet_epoch BIGINT NOT NULL DEFAULT 0 CHECK (diet_epoch >= 0),
      target_epoch BIGINT NOT NULL DEFAULT 0 CHECK (target_epoch >= 0),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE meal_plans
      ADD COLUMN title TEXT,
      ADD COLUMN start_date DATE,
      ADD COLUMN version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
      ADD COLUMN deleted_at TIMESTAMPTZ,
      ADD COLUMN migrated_at TIMESTAMPTZ,
      ADD CONSTRAINT meal_plans_owner_id UNIQUE (user_id, id)
    """,
    """
    CREATE TABLE planned_meals (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      plan_id TEXT NOT NULL,
      version BIGINT NOT NULL CHECK (version >= 1),
      local_date DATE NOT NULL,
      meal_type TEXT NOT NULL CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
      note TEXT,
      intake_generation BIGINT NOT NULL DEFAULT 1 CHECK (intake_generation >= 1),
      legacy_item_refs JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      deleted_at TIMESTAMPTZ,
      UNIQUE (user_id, id),
      FOREIGN KEY (user_id, plan_id) REFERENCES meal_plans(user_id, id)
    )
    """,
    """
    CREATE UNIQUE INDEX planned_meals_active_slot
      ON planned_meals(user_id, plan_id, local_date, meal_type) WHERE deleted_at IS NULL
    """,
    """
    CREATE INDEX planned_meals_owner_date ON planned_meals(user_id, local_date, id)
    """,
    """
    CREATE TABLE planned_meal_items (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL,
      planned_meal_id UUID NOT NULL,
      version BIGINT NOT NULL CHECK (version >= 1),
      position INTEGER NOT NULL CHECK (position >= 0),
      body JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      deleted_at TIMESTAMPTZ,
      UNIQUE (user_id, id),
      FOREIGN KEY (user_id, planned_meal_id) REFERENCES planned_meals(user_id, id)
    )
    """,
    """
    CREATE UNIQUE INDEX planned_meal_items_position
      ON planned_meal_items(user_id, planned_meal_id, position) WHERE deleted_at IS NULL
    """,
    """
    CREATE TABLE diet_logs (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      version BIGINT NOT NULL CHECK (version >= 1),
      local_date DATE NOT NULL,
      local_time TIME,
      timezone TEXT NOT NULL,
      utc_offset_minutes INTEGER CHECK (utc_offset_minutes BETWEEN -840 AND 840),
      occurred_at TIMESTAMPTZ,
      meal_type TEXT NOT NULL CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
      channel TEXT NOT NULL CHECK (channel IN ('manual','text','image','chat','plan')),
      note TEXT,
      source_planned_meal_id UUID,
      source_event_kind TEXT CHECK (source_event_kind IN ('default_plan','explicit_repeat','copy')),
      source_event_key TEXT,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      deleted_at TIMESTAMPTZ,
      UNIQUE (user_id, id),
      CHECK ((local_time IS NOT NULL) = (occurred_at IS NOT NULL)),
      CHECK (local_time IS NOT NULL OR utc_offset_minutes IS NULL),
      CHECK (source_event_kind IS DISTINCT FROM 'default_plan'
        OR source_planned_meal_id IS NOT NULL),
      FOREIGN KEY (user_id, source_planned_meal_id) REFERENCES planned_meals(user_id, id)
    )
    """,
    """
    CREATE INDEX diet_logs_owner_date ON diet_logs(user_id, local_date, id)
    """,
    """
    CREATE INDEX diet_logs_plan_source ON diet_logs(user_id, source_planned_meal_id, deleted_at)
    """,
    """
    CREATE UNIQUE INDEX diet_logs_source_event
      ON diet_logs(user_id, source_event_key) WHERE source_event_key IS NOT NULL
    """,
    """
    CREATE TABLE diet_log_items (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL,
      diet_log_id UUID NOT NULL,
      version BIGINT NOT NULL CHECK (version >= 1),
      position INTEGER NOT NULL CHECK (position >= 0),
      body JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
      source_planned_item_id UUID,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      deleted_at TIMESTAMPTZ,
      UNIQUE (user_id, id),
      FOREIGN KEY (user_id, diet_log_id) REFERENCES diet_logs(user_id, id),
      FOREIGN KEY (user_id, source_planned_item_id) REFERENCES planned_meal_items(user_id, id)
    )
    """,
    """
    CREATE UNIQUE INDEX diet_log_items_position
      ON diet_log_items(user_id, diet_log_id, position) WHERE deleted_at IS NULL
    """,
    """
    CREATE TABLE nutrition_targets (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      version BIGINT NOT NULL CHECK (version >= 1),
      effective_date DATE NOT NULL,
      calories_lower NUMERIC(15,6), calories_upper NUMERIC(15,6),
      protein_lower NUMERIC(15,6), protein_upper NUMERIC(15,6),
      fat_lower NUMERIC(15,6), fat_upper NUMERIC(15,6),
      carbohydrates_lower NUMERIC(15,6), carbohydrates_upper NUMERIC(15,6),
      source TEXT NOT NULL CHECK (source IN ('user_form','user_message','legacy_profile')),
      source_ref TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE (user_id, id),
      UNIQUE (user_id, effective_date),
      CHECK ((calories_lower IS NULL AND calories_upper IS NULL)
        OR (calories_lower IS NOT NULL AND calories_upper IS NOT NULL
          AND calories_lower > 0 AND calories_lower <= calories_upper
          AND calories_upper <= 100000)),
      CHECK ((protein_lower IS NULL AND protein_upper IS NULL)
        OR (protein_lower IS NOT NULL AND protein_upper IS NOT NULL
          AND protein_lower >= 0 AND protein_lower <= protein_upper AND protein_upper <= 10000)),
      CHECK ((fat_lower IS NULL AND fat_upper IS NULL)
        OR (fat_lower IS NOT NULL AND fat_upper IS NOT NULL
          AND fat_lower >= 0 AND fat_lower <= fat_upper AND fat_upper <= 10000)),
      CHECK ((carbohydrates_lower IS NULL AND carbohydrates_upper IS NULL)
        OR (carbohydrates_lower IS NOT NULL AND carbohydrates_upper IS NOT NULL
          AND carbohydrates_lower >= 0 AND carbohydrates_lower <= carbohydrates_upper
          AND carbohydrates_upper <= 10000))
    )
    """,
    """
    CREATE TABLE personal_foods (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      version BIGINT NOT NULL CHECK (version >= 1),
      name TEXT NOT NULL,
      brand TEXT,
      basis_type TEXT NOT NULL CHECK (basis_type IN ('per_100g','per_serving')),
      basis_amount NUMERIC(15,6) NOT NULL CHECK (basis_amount > 0),
      basis_unit TEXT NOT NULL CHECK (basis_unit IN ('g','serving')),
      food_state TEXT NOT NULL CHECK (food_state IN ('raw','cooked','as_sold','unknown')),
      nutrients JSONB NOT NULL CHECK (jsonb_typeof(nutrients) = 'object'),
      serving_evidence JSONB NOT NULL,
      allergens JSONB NOT NULL,
      source JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      deleted_at TIMESTAMPTZ,
      UNIQUE (user_id, id)
    )
    """,
    """
    CREATE TABLE diet_day_status (
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      local_date DATE NOT NULL,
      version BIGINT NOT NULL CHECK (version >= 1),
      status TEXT NOT NULL CHECK (status IN ('open','complete','no_intake')),
      declared_at TIMESTAMPTZ,
      updated_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY (user_id, local_date)
    )
    """,
    """
    CREATE TABLE operation_receipts (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      operation TEXT NOT NULL,
      idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 100),
      request_hash TEXT NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
      preview_id UUID,
      preview_version BIGINT CHECK (preview_version >= 1),
      result_refs JSONB NOT NULL CHECK (jsonb_typeof(result_refs) = 'array'),
      result_status TEXT NOT NULL CHECK
        (result_status IN ('prepared','committed','discarded','deleted','purged')),
      created_at TIMESTAMPTZ NOT NULL,
      expires_at TIMESTAMPTZ,
      UNIQUE (user_id, id),
      UNIQUE (user_id, operation, idempotency_key),
      CHECK ((preview_id IS NULL) = (preview_version IS NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX operation_receipts_confirmation
      ON operation_receipts(user_id, preview_id, preview_version)
      WHERE operation LIKE 'confirm:%'
    """,
    """
    CREATE TABLE diet_revisions (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      entity_type TEXT NOT NULL,
      entity_id TEXT NOT NULL,
      entity_version BIGINT NOT NULL CHECK (entity_version >= 1),
      operation TEXT NOT NULL,
      before_body JSONB,
      after_body JSONB,
      receipt_id UUID NOT NULL,
      actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user','migration','maintenance')),
      created_at TIMESTAMPTZ NOT NULL,
      UNIQUE (user_id, entity_type, entity_id, entity_version),
      FOREIGN KEY (user_id, receipt_id) REFERENCES operation_receipts(user_id, id)
        DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE intake_drafts (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      version BIGINT NOT NULL CHECK (version >= 1),
      thread_id TEXT,
      source_turn_id TEXT,
      channel TEXT NOT NULL CHECK (channel IN ('manual','text','image','chat','plan')),
      state TEXT NOT NULL CHECK
        (state IN ('needs_input','ready','committed','discarded','expired')),
      body JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
      field_locks JSONB NOT NULL CHECK (jsonb_typeof(field_locks) = 'array'),
      field_locks_version BIGINT NOT NULL DEFAULT 1 CHECK (field_locks_version >= 1),
      preview_hash TEXT,
      anchor JSONB NOT NULL,
      source_refs JSONB NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      receipt_id UUID,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE (user_id, id),
      CHECK (expires_at = created_at + INTERVAL '24 hours'),
      CHECK ((state = 'committed') = (receipt_id IS NOT NULL)),
      FOREIGN KEY (user_id, receipt_id) REFERENCES operation_receipts(user_id, id)
    )
    """,
    """
    CREATE INDEX intake_drafts_owner_state ON intake_drafts(user_id, state, created_at, id)
    """,
    """
    CREATE TABLE diet_change_previews (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      version BIGINT NOT NULL CHECK (version >= 1),
      thread_id TEXT,
      source_turn_id TEXT,
      operation TEXT NOT NULL,
      payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
      base_refs JSONB NOT NULL,
      base_target_epoch BIGINT,
      content_hash TEXT NOT NULL,
      calculation_version TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('ready','committed','discarded','expired')),
      expires_at TIMESTAMPTZ NOT NULL,
      receipt_id UUID,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE (user_id, id),
      CHECK (expires_at = created_at + INTERVAL '24 hours'),
      CHECK ((state = 'committed') = (receipt_id IS NOT NULL)),
      FOREIGN KEY (user_id, receipt_id) REFERENCES operation_receipts(user_id, id)
    )
    """,
    """
    CREATE TABLE media_assets (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      version BIGINT NOT NULL CHECK (version >= 1),
      kind TEXT NOT NULL CHECK (kind IN ('intake_image','label_image')),
      storage_key TEXT NOT NULL UNIQUE,
      content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
      mime TEXT NOT NULL CHECK (mime IN ('image/jpeg','image/png','image/webp')),
      bytes BIGINT NOT NULL CHECK (bytes > 0 AND bytes <= 10485760),
      width INTEGER CHECK (width > 0),
      height INTEGER CHECK (height > 0),
      state TEXT NOT NULL CHECK (state IN ('pending','ready','purge_pending','purged')),
      owner_refs JSONB NOT NULL,
      expires_at TIMESTAMPTZ,
      purged_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE (user_id, id),
      CHECK ((width IS NULL) = (height IS NULL)),
      CHECK (width::BIGINT * height::BIGINT <= 20000000)
    )
    """,
    """
    CREATE TABLE background_jobs (
      id UUID PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
      queue TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('parse_text','parse_image','export','purge')),
      state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
      payload JSONB NOT NULL,
      request_hash TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 2),
      lease_owner TEXT,
      lease_version BIGINT NOT NULL DEFAULT 0 CHECK (lease_version >= 0),
      heartbeat_at TIMESTAMPTZ,
      available_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      finished_at TIMESTAMPTZ,
      budget JSONB NOT NULL,
      usage JSONB NOT NULL,
      result_refs JSONB NOT NULL,
      error_code TEXT,
      artifact JSONB,
      UNIQUE (user_id, id),
      UNIQUE (user_id, kind, idempotency_key)
    )
    """,
    """
    CREATE INDEX background_jobs_schedule
      ON background_jobs(queue, state, available_at, created_at)
    """,
    """
    CREATE INDEX background_jobs_owner_state ON background_jobs(user_id, state)
    """,
    """
    CREATE TABLE background_job_events (
      job_id UUID NOT NULL,
      user_id BIGINT NOT NULL,
      sequence BIGINT NOT NULL CHECK (sequence >= 1),
      event_type TEXT NOT NULL,
      safe_payload JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY (job_id, sequence),
      FOREIGN KEY (user_id, job_id) REFERENCES background_jobs(user_id, id)
    )
    """,
)

DIET_SCHEMA_V2: tuple[str, ...] = (
    "ALTER TABLE meal_plans ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "UPDATE meal_plans SET updated_at=created_at",
)

DIET_SCHEMA_V3: tuple[str, ...] = (
    """CREATE TABLE diet_purge_tombstones (
        user_id BIGINT NOT NULL REFERENCES diet_user_state(user_id),
        entity_type TEXT NOT NULL CHECK(entity_type IN ('log','draft','preview')),
        entity_id UUID NOT NULL, version BIGINT NOT NULL CHECK(version>=1),
        purged_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(user_id,entity_type,entity_id))""",
)

DIET_SCHEMA_V4: tuple[str, ...] = (
    "ALTER TABLE diet_purge_tombstones DROP CONSTRAINT diet_purge_tombstones_entity_type_check",
    "ALTER TABLE diet_purge_tombstones ALTER COLUMN entity_id TYPE TEXT USING entity_id::TEXT",
    """ALTER TABLE diet_purge_tombstones ADD CONSTRAINT diet_purge_tombstones_entity_type_check
        CHECK(entity_type IN
          ('log','draft','preview','plan','planned_meal','personal_food','target'))""",
)
