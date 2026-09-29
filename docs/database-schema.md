# Database schema

PostgreSQL 15+ in production, SQLite 3.35+ for local development. The same SQLAlchemy models
serve both: portable column types are used throughout, and `JSONType` becomes `JSONB` on
Postgres. Migrations are Alembic, and every migration is required to run on both backends —
`tests/test_migrations.py` enforces this by upgrading an ephemeral SQLite database and running
`alembic upgrade head` as a test.

## Naming

* `id` — `UUID` primary key, generated in the application (`uuid4`), never a database sequence.
  This lets a client create an object and know its id before the transaction commits.
* Timestamps are `TIMESTAMPTZ`, stored UTC. Money-like quantities are `NUMERIC`, never `FLOAT`.
* Every table has `created_at`; mutable tables have `updated_at`, maintained by a shared trigger.
* Foreign keys are `ON DELETE` explicit. Cascade is used only for owned children (a job's events,
  a model's versions). Deleting a `user` is a soft delete (`deleted_at`); model blobs and audit
  log are retained.

## Entity map

```
users ─┬─< model_versions ─── models ─< model_analyses
       │                       └──────< mesh_repairs ──> orientations
       ├─< printers >── machine_profiles >── machine_models(machines)
       │                     │
       │                     ├─< profile_nozzles >── nozzles
       │                     └─> gcode_dialects
       ├─< material_profiles >── materials
       ├─< slice_jobs ─┬─< job_stage_runs
       │               ├─< job_events            (SSE tail)
       │               ├─< slice_parameter_sets ──< slice_parameters
       │               ├─< generated_files >── blobs
       │               ├─< validation_results ──< validation_findings
       │               ├─< simulation_results >── blobs
       │               ├─< failure_predictions
       │               └─< ai_decisions ──< ai_decision_evidence
       ├─< print_jobs ─< print_history
       ├─< telemetry_samples
       └─< audit_log
```

## Mapping to the requirements' table names

The requirements list `printers`, `printer_models`, `machines` separately. Three tables for one
concept invites drift, so the mapping is fixed and deliberate (ADR-0004):

| Requirement | Actual table | Rationale |
| --- | --- | --- |
| `printer_models`, `machines` | **`machines`** | One catalogue of machine *products* across all AM technologies (FDM, MSLA, DLP, SLS). Technology-agnostic. |
| `printer_profiles` | **`machine_profiles`** | Versioned capability + safety profile. Renamed because "printer" was FDM-specific and these apply to resin and powder systems too. |
| `printers` | **`printers`** | A user's owned instance, bound to one specific `machine_profile` **version**. |

## Tables

### Identity

**`users`** — `email` (citext, unique), `password_hash` (argon2id), `role`
(`user|operator|admin`), `is_active`, `failed_login_count`, `locked_until`, `last_login_at`,
`deleted_at`. Argon2 parameters are versioned in the stored hash string.

**`api_tokens`** — `user_id`, `name`, `token_hash` (sha256 of the token; the plaintext is shown
once at creation), `scopes` (JSON array), `expires_at`, `last_used_at`. Bearer tokens and JWT are
both supported; JWT for the browser, opaque tokens for CI and machine clients.

**`audit_log`** — append-only. `actor_user_id`, `action`, `object_type`, `object_id`, `before`
(JSON), `after` (JSON), `ip`, `user_agent`, `at`. Never updated or deleted by application code.

### Assets

**`blobs`** — `sha256` (unique, the storage key), `size_bytes`, `media_type`, `detected_format`,
`storage_path`, `created_at`. Immutable. `storage_path` is always
`blobs/<first2>/<sha256>` and is written by the storage layer, never by callers.

**`models`** — the logical asset the user sees in a list. `user_id`, `name`, `current_version_id`
(circular FK to `model_versions`, set after insert), `deleted_at`.

**`model_versions`** — immutable snapshot. `model_id`, `version`, `original_filename`,
`sanitized_filename`, `blob_id` (the **original, never mutated**), `repaired_blob_id`
(nullable), `format`, `byte_size`, `sha256`, `analysis_status`, `repair_status`,
`triangle_count`, `bbox_*` (denormalised from analysis for cheap list rendering).

> `original_filename` is stored verbatim for display. It is **never** used to build a path.
> `sanitized_filename` is the display-safe form. See ARCHITECTURE §5.

**`model_analyses`** — one row per analysis run. `model_version_id`, `engine` (`trimesh|x`),
`engine_version`, `input_sha256`, `report` (JSON: bbox, volume, area, manifold/overhang/thin-wall
findings, units, triangle count, density), `printability_score`, `risk_level`,
`findings_count_by_severity` (JSON), `duration_ms`. Analyses are immutable and keyed by
`input_sha256` so re-analysing identical geometry is cacheable.

**`mesh_repairs`** — `model_version_id`, `engine`, `from_sha256`, `to_sha256`, `operations`
(JSON: what was attempted, what changed, what failed), `source_blob_id`, `result_blob_id`,
`report` (JSON: before/after comparison), `user_approved` (bool). Repairs are *proposals* until
approved; the repaired blob is a new blob, never an overwrite.

**`orientations`** — `model_version_id`, `strategy` (`user|auto`), `objective`, `candidate_index`,
`quaternion` (JSON, xyzw), `euler_xyz_deg` (JSON), `score` (NUMERIC 0..100), `score_breakdown`
(JSON: support volume, overhang area, Z height, time, material — each with its own contribution),
`metrics` (JSON), `selected` (bool), `confidence`, `rationale`, `evidence` (JSON).
Partial unique index on `(model_version_id) WHERE selected` guarantees exactly one winner.

### Catalog

**`machines`** — catalogue row. `vendor`, `model`, `generation`, `technology`
(`fdm|msla|dlp|sls`), `motion_system` (`cartesian|corexy|corexz|delta|idex|toolchanger|cartesian_3d`),
`documentation_url`, `known_limitations` (JSON).

**`machine_profiles`** — versioned, immutable. `machine_id`, `profile_version` (int),
`status` (`draft|validated|deprecated`), `validation_status` (`unvalidated|validated`),
`validated_at`, `validated_by`, `build_volume` (JSON x/y/z), `bed_type`, `heated_bed` (bool),
`chamber` (JSON), `nozzle_diameters` (JSON float[]), `extruders` (int),
`temperature_limits` (JSON: nozzle min/max, bed min/max, chamber max),
`speed_limits` (JSON: max print/travel/accel/accel-to-accel), `max_volumetric_flow_mm3s`,
`machine_restrictions` (JSON), `start_gcode`, `end_gcode`, `gcode_dialect_id`,
`calibration_requirements` (JSON), `profile_hash`, `changelog`.
Unique `(machine_id, profile_version)`. **Immutable** — updates insert a new version.

**`gcode_dialects`** — `name` (`reprap|marlin|klipper|repetier|bambu|creality|grbl|unknown`),
`description`, `known_commands` (JSON), `temperature_commands` (JSON), `extrusion_axis` (JSON),
`quirks` (JSON). Drives the validator's command whitelist.

**`nozzles`** — `diameter_mm` (NUMERIC), `material` (`brass|steel|hardened|tungsten|...`),
`wear_resistance_rating`, `notes`. Unique `(diameter_mm, material)`.

**`materials`** — material *family*. `key` (`pla`, `petg`, `abs`, `tpu95a`, ...), `display_name`,
`family`, `is_composite`.

**`material_profiles`** — versioned, immutable, manufacturer-specific. `material_id`, `vendor`,
`brand_sku`, `profile_version`, `nozzle_temp` (JSON min/typical/max), `bed_temp` (JSON),
`chamber_temp`, `glass_transition_c`, `max_volumetric_flow_mm3s`, `retraction`
(JSON distance/speed), `print_speed` (JSON), `layer_height_range` (JSON),
`requires_enclosure`, `adhesion` (JSON: recommended surfaces, brim/raft),
`mechanical` (JSON: tensile/impact/elongation), `moisture_sensitivity`, `shrinkage_pct`,
`abrasiveness` (enum: none/low/medium/high — drives nozzle-wear warnings),
`manufacturer_notes`, `profile_hash`. Unique `(material_id, vendor, brand_sku, profile_version)`.

### Slicing

**`slicer_engines`** — registered engine. `key` (`cura`), `display_name`, `executable_path`,
`engine_version`, `license` (`LGPL-3.0`), `integration_mode` (`subprocess`), `capabilities`
(JSON: formats in/out, tree support, multi-material), `is_available` (bool), `last_probed_at`.

**`slicer_profiles`** — a named, reusable settings set for one engine. `engine_id`, `key`,
`display_name`, `settings` (JSON: the engine's own setting→value map), `profile_version`,
`profile_hash`.

**`slice_jobs`** — the pipeline. `user_id`, `model_version_id`, `printer_id`,
`machine_profile_id` (+`material_profile_id`, `nozzle_id` — the **exact versions** used),
`status`, `priority`, `stage_progress` (JSON), `error_code`, `error_detail`, `cancel_requested`,
`input_fingerprint` (hash over every input incl. profile versions), `queued_at`, `started_at`,
`finished_at`, `lease_owner`, `lease_expires_at`, `attempts`.
Index on `(status, priority, queued_at)` for the worker claim query.

**`job_stage_runs`** — one row per stage attempt. `slice_job_id`, `stage`, `attempt`,
`status`, `started_at`, `ended_at`, `duration_ms`, `progress` (0..1), `detail` (JSON),
`worker_id`, `input_fingerprint`, `output_blob_id`. The source of truth for progress reporting.

**`job_events`** — append-only, high volume, trimmed by retention job. `slice_job_id`, `at`,
`level`, `stage`, `message`, `data` (JSON). Backs the SSE stream and the audit trail.

**`slice_parameter_sets`** — one resolved parameter set per job. `slice_job_id`, `source`
(`auto|user|ai_proposal|historical`), `objective` (JSON: quality/strength/time/material/accuracy
weights), `resolved_by`, `confidence`, `rationale` (JSON list), `engine_settings` (JSON: the
final, validated key/value map actually handed to the slicer).

**`slice_parameters`** — one row per parameter so it can be locked and overridden. `parameter_set_id`,
`key`, `value` (JSON), `source`, `locked` (bool), `locked_by`, `constraint` (JSON: hard bounds
from the machine/material profile), `reason` (text), `confidence`.
`CHECK (locked = 0 OR locked_by IS NOT NULL)`. **The optimiser must skip locked parameters** —
enforced in the service, asserted in tests.

**`generated_files`** — `slice_job_id`, `blob_id`, `kind` (`gcode|three_mf|project|ctb|report`),
`filename`, `sha256`, `byte_size`, `is_blocking_blocked` (bool: true when the validator
rejected it — such files are never downloadable), `blocked_reason`, `created_at`.

**`validation_results`** — `slice_job_id`, `validator` (`gcode-v1`), `validator_version`,
`input_sha256`, `passed` (bool), `severity_counts` (JSON), `blocks_output` (bool),
`summary`, `checked_at`, `duration_ms`.

**`validation_findings`** — `validation_result_id`, `severity` (`critical|warning|info`),
`code` (`OUT_OF_BOUNDS`, `TEMPERATURE_TOO_HIGH`, …), `message`, `line_number`, `axis`,
`measured_value`, `limit_value`, `unit`, `mitigation`, `source` (`deterministic|ai_suggested`).

**`simulation_results`** — `slice_job_id`, `blob_id` (frames), `renderer`,
`estimated_duration_s`, `filament_used_mm` / `filament_used_g`, `layer_count`,
`travel_moves`, `extrusion_moves`, `retraction_count`, `tool_changes`, `params` (JSON),
`warnings` (JSON), `render_version`.

### Printing, telemetry, learning

**`print_jobs`** — the act of printing. `user_id`, `slice_job_id`, `printer_id`, `status`
(`queued|sent|printing|paused|completed|failed|cancelled`), `sent_at`, `started_at`,
`finished_at`, `progress_pct`, `estimated_duration_s`, `actual_duration_s`,
`material_used_g`, `operator_notes`.

**`print_history`** — the learning substrate. `print_job_id`, `user_id`, `printer_id`,
`machine_profile_id`, `material_profile_id`, `nozzle_id`, `parameters` (JSON),
`outcome` (`success|partial_success|failure`), `failure_mode`, `quality_score` (0..100),
`observed_by`, `observed_at`. A unique index over
`(printer_id, material_profile_id, outcome)` feeds the confidence-weighted historical lookup.
Rows created without operator confirmation are flagged `observed_by = 'inferred'` and are
weighted at 0.2 when they inform a recommendation.

**`telemetry_samples`** — append-only, high volume, partitioned by time. `print_job_id`,
`at`, `nozzle_temp_c`, `bed_temp_c`, `chamber_temp_c`, `axis_position` (JSON),
`motor_state` (JSON), `layer`, `progress_pct`, `flow_rate`, `firmware_error_code`,
`camera_fault` (JSON, from the CV pipeline).

**`failure_predictions`** — `slice_job_id`, `print_job_id`, `failure_mode`, `probability`
(NUMERIC 0..1), `confidence` (NUMERIC 0..1), `calibration` (`heuristic|historical|uncalibrated`),
`severity`, `affected_region` (JSON), `evidence` (JSON), `mitigations` (JSON),
`predicted_at`, `was_realised` (nullable bool, filled in by telemetry), `outcome_recorded_at`.
`was_realised` is what makes these predictions *auditable* instead of decorative.

**`ai_decisions`** — every AI-derived or AI-influenced decision. `user_id`, `slice_job_id`,
`model_version_id`, `decision_type` (`orientation|parameter|support|diagnosis|repair|chat`),
`proposed_value` (JSON), `applied_value` (JSON), `reason` (text, **NOT NULL, non-empty**),
`confidence` (NUMERIC), `calibration`, `source` (`llm|heuristic|historical|human`),
`model_identifier`, `prompt_hash`, `overridden_by`, `overridden_at`, `override_reason`.
A `CHECK` constraint enforces `length(trim(reason)) > 0`.

**`ai_decision_evidence`** — `decision_id`, `kind` (`analysis|profile|toolpath|history|document`),
`ref_type`, `ref_id`, `excerpt` (text), `weight` (NUMERIC 0..1). `CHECK (count > 0)` is enforced
in the service layer: a decision with no evidence row is rejected.

## Indexes and constraints worth calling out

* `blobs.sha256` unique — deduplication and integrity check in one.
* `model_analyses (model_version_id, input_sha256)` — analysis cache lookup.
* `slice_jobs (status, priority DESC, queued_at)` — the worker claim query; partial index
  `WHERE status IN ('QUEUED')` keeps it small.
* `slice_jobs.input_fingerprint` indexed — idempotent re-runs.
* Partial unique `orientations (model_version_id) WHERE selected` — exactly one winning
  orientation, enforced by the database, not by application hope.
* `generated_files.is_blocking_blocked` — the download guard is a single indexed predicate.
* `users.email` citext unique — case-insensitive login without application-level `lower()`.
* All `*_id` FKs indexed explicitly; Postgres does not do this for you.

## Retention

| Data | Policy |
| --- | --- |
| Original model blobs | Retained indefinitely, never deleted |
| `job_events` | 30 days |
| `telemetry_samples` | 90 days (or per-user setting) |
| `simulation_results` frames | 90 days; the summary row is kept |
| `ai_decisions` | Retained indefinitely — this is the audit trail for automated behaviour |
| `audit_log` | Retained indefinitely |
