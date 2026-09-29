# AI Slicing Platform

An AI-powered universal slicing orchestration platform. Give it a 3D model, a
printer and a material; it analyses the geometry, resolves the profiles,
optimises the parameters, runs a **real slicing engine**, validates the
resulting toolpath, and hands back a printer-specific file.

The design principle, and the reason for most of the code here:

> **The LLM may propose. Deterministic code disposes.**

Geometry, machine limits, toolpaths and safety are computed by real engines and
profile data, never by a model. See [`ARCHITECTURE.md`](ARCHITECTURE.md).

## What actually works today

Verified end to end against the real engine, not mocked:

```
upload STL -> analyze -> printer + material + nozzle -> resolve profiles
  -> PrintConfiguration -> SlicerRegistry -> CuraEngineAdapter -> REAL G-code
  -> G-code validator -> toolpath parse -> download
```

A 20 mm cube at 0.3 mm produces a **67-layer, full-height, on-bed** toolpath
that passes validation, in about half a second. `python backend/scripts/verify_stack.py`
re-runs the whole thing against a live uvicorn server.

| Area | Status |
| --- | --- |
| Mesh analysis (volume, manifold, overhangs, thin walls by ray casting) | **Working**, unit-tested against known ground truth |
| Secure upload (magic-byte sniffing, dedup, zip-bomb guard, traversal-proof) | **Working** |
| Auth, RBAC, rate limiting, audit log | **Working** |
| Printer / material / nozzle knowledge base, versioned | **Working** (5 machines, 11 material families, 12 material profiles) |
| Parameter optimisation with reasons, confidence, locks | **Working** |
| CuraEngine 5.13 slicing (AGPL-3.0, out of process) | **Working**, 17 tests against the live binary |
| G-code validation + the download guard | **Working** |
| Toolpath parsing for 3D visualisation | **Working** |
| Technology + slicer registries, data-driven routing | **Working** |
| React/TS-strict workbench with 3D model and toolpath viewport | **Working** |
| Mesh repair | **NOT IMPLEMENTED** — reported as `skipped`, original used unchanged |
| Print simulation | **NOT IMPLEMENTED** — reported as `skipped`, no frames produced |
| Orientation optimisation | **NOT IMPLEMENTED** |
| AI assistant / LLM orchestration | **NOT IMPLEMENTED** (provider hookup only) |
| Resin / powder-bed slicing | **NOT IMPLEMENTED** — declared in the registry, refused with a reason |
| OrcaSlicer / PrusaSlicer adapters | **NOT IMPLEMENTED** — the port exists; adding one is a class plus one `register()` call |

Anything not implemented is labelled as such in the UI and in the API. No
simulated progress, no placeholder files, no fake print results.

## Quick start

Requires Python 3.10+, Node 20+, and a slicing engine for actual slicing.

```bash
# backend
cd backend
python -m venv .venv && .venv\Scripts\activate        # or: source .venv/bin/activate
pip install -e ".[dev,postgres]"
cp ../.env.example ../.env
uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
pnpm install
pnpm --filter ./frontend dev                          # http://localhost:5173
```

The catalogue (machines, materials, nozzles, G-code dialects) is seeded on first
start from `backend/app/catalog/seed_data.json`.

### Slicing needs an engine

Point `CURA_ENGINE_PATH` at a CuraEngine binary. The adapter also needs
`fdmprinter.def.json` next to it — without that file the engine has no settings
schema and silently produces an empty file.

```bash
python backend/scripts/real_slice.py     # one real slice, with the report
python backend/scripts/verify_stack.py   # the whole product, live server
```

### Validated profiles are a prerequisite

Seed profiles ship as `validation_status = unvalidated`, and the platform
**refuses to generate G-code for an unvalidated machine profile**. That is
deliberate: nothing in a data file can honestly claim to have been tested
against real hardware. Promote a profile after bench-testing it.

## Tests

```bash
cd backend
python tests/fixtures/build.py      # regenerate the regression models
pytest -q                            # 106 tests
pytest -q -m "not slow"              # skip the ones that need a slicing engine
```

`tests/test_real_slicing.py` and `tests/test_pipeline_e2e.py` invoke the actual
binary and are skipped, not faked, when it is absent. Every test there
corresponds to a bug that was found by running the engine.

## Documentation

| Document | Contents |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Invariants, process topology, bounded contexts, job state machine, security posture |
| [`docs/database-schema.md`](docs/database-schema.md) | All 34 tables, rationale, requirement mapping, indexing, retention |
| [`docs/SLICER_ADAPTER_SPEC.md`](docs/SLICER_ADAPTER_SPEC.md) | The adapter port, the rules, the checklist for a new engine, verified Cura behaviour |
| [`docs/PRINTER_PROFILE_SPEC.md`](docs/PRINTER_PROFILE_SPEC.md) | Machine profile fields, versioning, how to add a printer |
| [`docs/MATERIAL_PROFILE_SPEC.md`](docs/MATERIAL_PROFILE_SPEC.md) | Material profile fields, versioning, how to add a material |

## Two things to know before deploying

**Licence.** CuraEngine 5.13 is **AGPL-3.0**, not LGPL as older Cura releases
were. It is driven as a separate process, so this project's source is not a
derivative work, but AGPL has network-use copyleft and **needs a legal review
before commercial use**. The engine's licence is read from its own banner rather
than assumed, and surfaced in `GET /api/v1/slicers` as
`licence_review_required: true`. OrcaSlicer and PrusaSlicer are GPL-3.0 with the
same consideration.

**Scope of validation.** Everything the platform produces is *digitally*
validated: geometry, machine limits, toolpath bounds, thermal envelope. A
successful validation is not a prediction that the part will print well. That
also depends on bed levelling, calibration, filament moisture, ambient
temperature, mechanical wear, belt tension, nozzle condition and the machine's
current state. The product says so in the footer rather than promising a perfect
print.

## Known open gaps

* **Wall-count and infill-density fidelity is unverified.** The toolpath is
  correct in extent, height and bed position, and passes validation, but the
  extruded volume is lower than a hand calculation for the requested
  `wall_line_count` predicts. The mapping likely involves Cura's
  `skin_outline_count` and per-extruder scoping. Measure a caliper coupon
  before trusting it. Tracked in `SLICER_ADAPTER_SPEC.md` §6.
* STEP/STP and IGES are recognised but not converted; a CAD kernel (OpenCascade)
  worker is required. The format matrix reports this rather than claiming
  support.
* Only the regression fixtures have been sliced end to end. No organic or
  multi-part model has been validated.
