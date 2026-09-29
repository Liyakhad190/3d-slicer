# Architecture

## 0. The one invariant everything else serves

> **The LLM may propose. Deterministic code disposes.**

No path exists by which model-generated text becomes a machine instruction. Every AI output
is a *proposal record* (`ai_decisions`) carrying a reason, evidence references and a confidence
value. A proposal is only accepted if a deterministic service re-derives the same fact from real
geometry, real profile data or a real toolpath — or if a human explicitly overrides it, in which
case the override is recorded with the machine-safety checks that were still applied.

`app/intelligence/decision_gate.py` is the single chokepoint that enforces this. It is not a
helper function; nothing else in the codebase is allowed to accept an AI proposal.

### What is authoritative for what

| Concern | Authority | Why |
| --- | --- | --- |
| Geometry, volume, manifoldness, overhangs | `trimesh` + `manifold3d` (deterministic) | Numerical facts must be reproducible and auditable |
| Machine limits, build volume, thermal envelope | Versioned `machine_profiles` | Safety. A wrong number here injures hardware or voids warranty |
| Slicing / toolpath generation | `CuraEngine` subprocess (deterministic) | Slicing is a solved, validated algorithm; re-implementing it is a liability |
| G-code safety | `app/slicing/validation/*` (deterministic) | Last gate before the machine |
| Orientation *scoring*, parameter *proposals*, risk *explanations* | LLM + heuristics, **recorded and scored**, then gated | Judgement, not arithmetic |
| Whether a risky proposal is *admissible* | Deterministic policy layer | Never delegated |

## 1. Process topology

```
                        ┌──────────────────────────────┐
  Browser  ──HTTPS──▶   │  api  (FastAPI, uvicorn)     │
                        │  auth · uploads · queries    │
                        │  SSE /job-events/{id}        │
                        └───────┬──────────────┬───────┘
                                │ SQLAlchemy   │ enqueue
                                ▼              ▼
                        ┌───────────────┐  ┌──────────────────┐
                        │  PostgreSQL   │  │  job_queue       │
                        │  (SQLite dev) │◀─┤ (DB lease queue) │
                        └───────────────┘  └────────┬─────────┘
                                ▲                   │
                                │        ┌──────────┼──────────┬─────────────┐
                                │        ▼          ▼          ▼             ▼
                                │   geometry     slicing   simulation     ai
                                │   worker       worker     worker       worker
                                │        │          │          │             │
                                │        └──────────┴────┬─────┴─────────────┘
                                │                      │  writes gcode + report
                                │                      ▼
                                │             ┌──────────────────┐
                                │             │ G-code Validator │
                                │             │ (deterministic)  │
                                │             └────────┬─────────┘
                                │                      │ BLOCK or PASS
                                ▼                      ▼
                        ┌──────────────────────────────────────┐
                        │  blob store (content-addressed)      │
                        │  models/ blobs/jobs/ slices/ artifacts│
                        └──────────────────────────────────────┘
```

Each worker is a separate OS process polling the queue with a **lease** (`FOR UPDATE SKIP LOCKED`
on Postgres, a transactional claim on SQLite). A worker that dies releases its lease on timeout
and the stage is retried. Stage execution is **idempotent** and keyed by input hashes, so a retry
re-derives rather than duplicates.

**Sandboxing.** The slicing worker is the only process allowed to spawn `CuraEngine`. It runs with
a scrubbed environment, an explicit `cwd` inside a per-job temp dir, a wall-clock timeout, an
output-size cap, and no network. Uploaded files are never executed — they are parsed by
format-specific readers in a read-only mount.

## 2. Bounded contexts

| Context | Package | Owns |
| --- | --- | --- |
| identity | `app/identity` | users, tokens, roles, audit log |
| assets | `app/assets` | uploaded models, versions, blob store, format sniffing |
| geometry | `app/geometry` | analysis, repair, orientation, support *strategy* (not support geometry) |
| catalog | `app/catalog` | machine profiles, material profiles, nozzle profiles, versions |
| slicing | `app/slicing` | parameter resolution, engine adapters, toolpath, gcode validation, simulation |
| jobs | `app/jobs` | queue, state machine, progress, events |
| intelligence | `app/intelligence` | LLM provider, agent tool surface, decision gate, risk narrative |

Contexts communicate through service functions and DB rows, never by reaching into each other's
internals. `app/adapters` holds ports (Protocols) and their implementations.

## 3. The job state machine

```
QUEUED ─▶ ANALYZING ─▶ REPAIRING ─▶ OPTIMIZING ─▶ SLICING ─▶ VALIDATING ─▶ SIMULATING ─▶ COMPLETED
   │           │            │            │            │            │             │
   └───────────┴────────────┴────────────┴────────────┴────────────┴─────────────┴──▶ FAILED
                                                                                                 
any state ──(policy says it must not reach a machine)──▶ BLOCKED
```

Transitions are validated by a single table in `app/jobs/state_machine.py`; an illegal transition
raises rather than silently corrupting job history. `BLOCKED` is a first-class terminal state:
a print that fails validation must never be presentable as "ready to print", and `GET
/generated-files` refuses to serve a `BLOCKED` job's artifacts.

Progress is derived from stage runs, not fabricated. Each stage reports
`{stage, status, started_at, ended_at, progress, detail}` and the API streams it over SSE.

## 4. Data model

See [`docs/database-schema.md`](docs/database-schema.md). Three rules govern it:

1. **Profiles are versioned and immutable.** A `machine_profile` or `material_profile` row is
   never updated in place; a new version row is inserted and the pointer moves. A `slice_job`
   stores the exact profile *version ids* it ran against, so re-opening a year-old job shows the
   machine as it was, not as it is now. A profile update therefore cannot silently invalidate
   previously generated output.
2. **Blobs are content-addressed and immutable**, keyed by SHA-256. Model versions, generated
   G-code, analysis JSON and simulation frames all point at blobs. Uploading the same STL twice
   stores it once.
3. **Unoriginals are never mutated.** `model_versions.repaired_blob_id` is a *new* row. The
   original blob is retained forever and is always downloadable.

JSON is used for wide, read-whole analytical payloads (analysis reports, validation findings,
AI decisions) and columns for anything that is queried or constrained. The JSON columns become
`JSONB` on Postgres via a portable `JSONType`.

## 5. Security posture

| Threat | Control |
| --- | --- |
| Path traversal via filename | Filenames are **never** used as paths. Blobs are `blobs/<aa>/<sha256>`; the original name is stored as data and sanitised for display only. |
| Malicious model file | Magic-byte format sniffing (never the extension), 200 MB cap, parser runs on a read-only copy, decompression-ratio limit on 3MF (zip-bomb guard) |
| Command injection via slicer args | No `shell=True` anywhere. `CuraEngine` is invoked with an argv list; every setting value is validated against a typed schema before it reaches argv. |
| Slicer crash / fork bomb | Dedicated worker process, `timeout`, output cap, `CREATE_NO_WINDOW`, killed process tree |
| Secrets | All config from env; `AUTH_SECRET` must be explicitly set outside dev, and startup **fails** if it is left at the dev default |
| Privilege escalation | Every read/write path resolves ownership through a guard; cross-tenant access is 404, not 403 (no existence oracle) |
| Unbounded work | Per-user and per-IP rate limits, upload quotas, job queue depth caps |

## 6. The AI layer

The assistant is a tool-calling loop over the **same** service functions the HTTP API uses. It
has no privileged write path. Its tools are the ones in §43 of the requirements
(`analyze_model`, `optimize_parameters`, `run_slicer`, `validate_gcode`, …) and each is a thin
binding over a deterministic service.

Failure modes are engineered, not hoped away:

- **Unknown printer/material/nozzle** → the catalogue lookup returns no profile, the tool returns
  `INSUFFICIENT_INFORMATION`, and the assistant is required to say so rather than interpolate.
  There is no fallback "typical PLA" table consulted at generation time; profiles either exist
  or the request stops.
- **Confidence** is a recorded number with a method attached (`calibration: heuristic|historical|
  uncalibrated`). A heuristic confidence is labelled as such in the UI and is never shown as a
  percentage without that label.
- **Explainability** is a storage requirement, not a prompt instruction: a decision without
  non-empty `reason` and at least one evidence reference cannot be persisted.

## 7. Deliberate non-goals

- **No in-house slicer.** Slicing is delegated to `CuraEngine` behind `SlicerEngineAdapter`.
  Re-implementing plane sweep, perimeter generation and support meshing would be strictly worse
  and unvalidatable.
- **No "your print will be perfect".** All output is scoped to *digital* validation, with a
  standing list of physical factors the software cannot observe (§41 of the requirements).
- **No claimed printer support without a validated profile.** `machine_profiles.validation_status`
  is a real column; the UI refuses to offer an unvalidated profile for output generation.
- **No resin/SLS support in Phase 1.** The adapter interfaces are multi-technology, but shipping
  an unvalidated `.ctb` writer would be exactly the "fake extension" the requirements forbid.
  Marked `NOT IMPLEMENTED` until a real engine is validated.
