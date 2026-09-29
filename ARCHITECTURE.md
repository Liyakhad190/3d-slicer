# ARCHITECTURE

Phase 0 audit. This describes the repository as it actually is, including the
defects the audit found. Nothing here is aspirational.

## 1. Current architecture

```
frontend (React 18 + Vite + TS strict)
  │  fetch /api/v1  (Vite dev proxy)
  ▼
api (FastAPI, sync handlers in a threadpool)
  ├── auth/identity        argon2id, JWT, roles, rate limit, audit log
  ├── models/assets        upload → sniff → content-addressed blob
  ├── catalog              machine / material / nozzle knowledge base
  └── slicing              POST /slice, jobs, validation, toolpath
                              │
                              ▼
                         run_pipeline()            app/slicing/pipeline.py
                              │
   ┌──────────┬──────────┬───┴───────┬──────────┬──────────┐
   ▼          ▼          ▼           ▼          ▼          ▼
analyse   optimise   select      slice     validate   toolpath
(trimesh) (optimizer)(registry) (adapter) (validator)(parser)
                                          │
                                          ▼
                                    CuraEngine (subprocess, AGPL-3.0)
```

### Data flow for one job

`POST /slice` runs synchronously and creates a `slice_job` first, so history
survives a crash. Stages write `job_stage_runs` rows and `job_events`, and the
UI reads progress from those rows — there is no progress value invented by the
client.

1. `models_service.run_analysis` → trimesh metrics, cached by content hash
2. build-volume fit check (fails fast, before any engine time is spent)
3. `ParameterOptimizer.optimise` → parameters + a reason per decision
4. `configuration.from_profiles` → `PrintConfiguration`; **refuses unvalidated
   machine profiles**
5. `SlicerRegistry.select` → ranks engines by capability, refuses when none fit
6. `CuraEngineAdapter.slice` → prepares the mesh, runs the binary
7. `validate_gcode` → any `critical` finding blocks the download
8. `parse_toolpath` → renderable segments

## 2. Existing interfaces (do not break)

| Interface | Location | Contract |
| --- | --- | --- |
| `PrintConfiguration` | `app/slicing/configuration.py` | Frozen dataclass; the only thing adapters see. `to_dict()`, `fingerprint()`, `require_runnable()` |
| `MachineLimits` | `app/slicing/validator.py` | Build volume, thermal envelope, speed ceilings, `origin_convention`, `axis_bounds()` |
| `SlicerEngineAdapter` | `app/slicing/cura_adapter.py` | Protocol: availability, probe, capabilities, `validate_configuration`, `prepare_configuration`, `slice`, `validate_output` |
| `SlicerRegistry` | `app/slicing/registry.py` | `register()`, `candidates()`, `select()`, `describe()` |
| `TechnologyRegistry` | `app/slicing/registry.py` | `get`, `require_supported`, `accepts_input`; unsupported technologies raise |
| `ParameterOptimizer` | `app/slicing/optimizer.py` | `optimise(objective, geometry, locked)` → `OptimisationResult` with per-decision reasons |
| `MeshAnalysisReport` | `app/services/geometry/analyzer.py` | Metrics + `Finding`s + score with a published breakdown |
| `prepare_file` | `app/services/geometry/placement.py` | Canonical pose: bottom on plate, centred on the **origin** |

### Extension points that already work

* **New slicer engine** — one class implementing the protocol, one
  `register()` call. No core change.
* **New printer** — a `machines` + `machine_profiles` row. No code. This is
  load-bearing: there is no `if printer == "Ender"` anywhere.
* **New material** — a `materials` + `material_profiles` row.
* **New technology** — a `TechnologySpec` in `registry.py` with
  `supported=False` until an engine exists.
* **New file format** — a `ModelFormat` + a case in `detect_format`, and the
  capability matrix updates itself from data.

## 3. Phase 0 audit findings

The audit read every module. The important results:

**Sound and worth preserving**

* The AI/deterministic boundary holds: the optimizer, not a model, produces
  parameters; a blocked configuration cannot reach an adapter.
* Profile versioning is real — jobs store exact `*_profile_id` values.
* The download guard is a single indexed predicate (`generated_files.is_blocked`).
* Content-sniffed upload with a structural binary-STL check, zip-bomb guard and
  path-free blob keys.
* Strict TS and typed Python throughout; the strict flags have caught real bugs.

**Defects found**

| # | Defect | Severity | Status |
| --- | --- | --- | --- |
| D1 | `infill_sparse_density`, `infill_sparse_pattern`, `infill_sparse_thickness`, `top_bottom_thickness`, `wall_thickness`, `skin_outline_count` are **GUI-derived settings the headless engine ignores** | **Critical** | **Fixed in Phase A**, with a measurement test per setting |
| D2 | `material_diameter` left at the schema placeholder (2.85 mm) | **Investigated: not a defect** | Set explicitly anyway |
| D3 | `bottom_layers` is inert in Cura 5.13; `top_layers` governs both shells | Medium | **Fixed in Phase A** |
| D4 | `total_extruded_length_mm` is *path length*, but the name reads like *filament length*; the two differ ~10x | Medium | **Fixed in Phase A** (split into `extruded_path_length_mm` / `filament_mm`) |
| D5 | `POST /slice` runs the whole pipeline in the request | Medium | Open: job rows exist, execution is inline |
| D6 | No mesh validator separate from the analyzer; no repair at all | Medium | Phases B/C |
| D7 | `slice_parameters` / `parameter_set` tables exist but optimizer decisions are not persisted to them | Medium | Open |
| D8 | `dialect` local in `from_profiles` is assigned and unused | Trivial | Open |
| D9 | No worker process; `job_worker_enabled` / lease columns are unused | Medium | Open |

**D1 was the cause of the fidelity gap.** It is answered, not open. The
requested infill density, pattern and top/bottom thickness were being sent,
accepted, and discarded. Each is now translated to a *primitive* setting the
engine actually reads, and each translation has a test that slices with the
setting varied and measures the deposited geometry from the G-code.

**D2 was investigated and found not to be a defect.** CuraEngine 5.13 emits
identical E values for `material_diameter` of 1.75, 2.85 and unset (verified:
E sum 630.03 mm in all three cases), so the placeholder does not mis-scale the
output. The value is still set explicitly because it is correct and another
engine may honour it. The earlier "critical" label was wrong.

## 4. Technical debt

* `models_service._TRIMESH_FILE_TYPE` and the mapping now in `pipeline.py` are
  duplicated format knowledge; both should come from one place.
* `MaterialProfile` has no filament diameter, so D2 cannot be fixed from data.
* `MeshRepair`, `Orientation`, `AiDecision` and `PrintJob` tables exist with no
  writer. Schema is ahead of behaviour — which is deliberate, but each one needs
  either an implementation or a documented `NOT IMPLEMENTED` endpoint.
* The `RateLimitBucket` fixed-window limiter is single-node only.
* `intelligence/` is an empty package with a docstring.

## 5. Unsupported features (must stay explicit)

| Feature | Where declared |
| --- | --- |
| Resin / powder-bed / metal slicing | `TechnologyRegistry`, `supported=False` |
| MSLA / DLP / SLS / SLM | same |
| OrcaSlicer, PrusaSlicer | absent from `SlicerRegistry`; the port supports them |
| Mesh repair | `pipeline` reports `skipped`, `implemented: false` |
| Print simulation | same |
| Orientation optimisation | no endpoint, no UI affordance |
| STEP/IGES conversion | `formats.NOT_IMPLEMENTED` |
| `ai_decisions` | table only; no writer |

## 6. Proposed implementation boundaries

Phases A–J from the task, mapped onto what already exists:

| Phase | Lands in | Depends on |
| --- | --- | --- |
| A Fidelity | `cura_settings.py`, new `measurement.py`, new calibration suite | — |
| B Mesh validation | new `app/services/geometry/validation.py` | — |
| C Mesh repair | new `app/services/geometry/repair.py`; writes `MeshRepair` | B |
| D Printer capabilities | new `PrinterCapabilities` projection over `MachineProfile` | A |
| E Orientation | new `app/services/geometry/orientation.py`; writes `Orientation` | A, B |
| F Toolpath analysis | extend `measurement.py`; `SimulationResult` | A |
| G Viewer | extend `Viewport.tsx`; consume the canonical toolpath | F |
| H Print intent | new `app/slicing/intent.py`; `Objective` stays as an alias | A |
| I AI layer | `app/intelligence/` + `ai_decisions`; constrained schema, gated | A–H |
| J Multi-slicer | new adapters; no core change | A |

**Boundaries that must hold**

1. The optimizer and validator never learn an engine-specific setting name.
2. Geometry decisions are deterministic. An LLM may rank candidates, never
   compute them.
3. Any AI output is a proposal validated against `MachineLimits` and the
   material profile before use, and is recorded in `ai_decisions`.
4. Every user-visible number is REQUESTED, GENERATED or MEASURED — and the
   three are never conflated. A measurement of G-code is not a measurement of
   plastic.
5. An unimplemented stage reports `skipped` with `implemented: false`. Never a
   green check.
6. Original geometry is never mutated; prepared copies are separate artifacts.
7. Licence metadata stays visible on every engine surface.
