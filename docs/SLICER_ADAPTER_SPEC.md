# Slicer Adapter Specification

This is the contract between the platform and any slicing engine. The platform
depends on **this interface only**. No engine-specific type, setting name or
command-line flag may appear outside the adapter that owns it.

## 1. Port

```python
@runtime_checkable
class SlicerEngineAdapter(Protocol):
    key: str                 # stable identifier, e.g. "cura"
    display_name: str
    license: str             # detected from the binary, never assumed

    # -- availability ---------------------------------------------------
    def is_available(self) -> bool: ...
    def probe(self) -> str | None: ...
        # Engine version, or None if it cannot be run.

    # -- capabilities ---------------------------------------------------
    def get_capabilities(self) -> dict[str, Any]: ...
    def get_supported_formats(self) -> list[str]: ...
    def get_supported_technologies(self) -> list[str]: ...
    def get_supported_materials(self) -> list[str]: ...

    # -- configuration --------------------------------------------------
    def validate_configuration(self, configuration: PrintConfiguration) -> list[str]:
        """Pre-flight problems. Empty list means the job may proceed."""

    def prepare_configuration(self, configuration: PrintConfiguration) -> dict[str, Any]:
        """Canonical configuration -> engine-native settings."""

    # -- execution ------------------------------------------------------
    def slice(self, request: SliceRequest) -> SliceResult: ...

    # -- verification ---------------------------------------------------
    def validate_output(self, output_path: Path) -> list[str]:
        """Post-slice problems. Empty list means the file is usable."""
```

## 2. The rules an adapter must obey

1. **Subprocess, never in-process.** Engines are third-party parsers of
   untrusted geometry. Running them out of process bounds the failure surface
   and keeps the licence boundary clean.
2. **No `shell=True`, ever.** Arguments are passed as an argv list. A setting
   value must never be able to become a shell command.
3. **Scrubbed child environment.** The engine does not need the user's
   environment, and inheriting it would leak secrets into a process parsing
   untrusted input.
4. **Time and output caps.** `slicer_timeout_seconds`,
   `slicer_max_output_bytes`. A timeout kills the process; an over-cap output
   is deleted, not stored.
5. **Prepare the model yourself.** Do not trust the engine to place the mesh.
   See §5 — this is a real bug class, not a precaution.
6. **Report failures as failures.** A non-zero exit or a missing/empty output
   raises. Never emit a placeholder file, never fabricate progress.
7. **Drop unknown settings loudly.** Sending a key the engine's schema does not
   define aborts the slice. Unknown keys are dropped and reported.
8. **The validator and the slicer must agree.** Both derive the build volume and
   the coordinate frame from the same machine profile fields. If they disagree,
   the safety check is theatre.

## 3. `PrintConfiguration` — the canonical intermediate representation

Adapters consume this, never the optimizer's internals. It is what makes
vendor lock-in impossible.

```
PrintConfiguration
  technology          fdm | msla | dlp | sls | slm
  machine_profile     -> build volume, thermal envelope, speed/accel ceilings,
                         max volumetric flow, coordinate frame, start/end G-code
  material_profile    -> temperature ranges, flow, retraction, speed, abrasiveness
  nozzle              -> diameter, material (brass/hardened)
  parameters          layer height, line width, walls, top/bottom, infill,
                      speed, acceleration, temperatures, cooling, retraction,
                      support, adhesion
  objective           quality | strength | speed | material | balanced
  limits              MachineLimits, shared with the validator
```

## 4. Adding an adapter

A new engine is a new class plus a registry entry. No change to the optimizer,
the validator, the job pipeline or the API. Checklist:

- [ ] `key`, `display_name`, `license` (detected, not assumed)
- [ ] `is_available()` — does the binary exist and run
- [ ] `probe()` — real version string
- [ ] `get_capabilities()` — formats, technologies, features, licence flag
- [ ] `validate_configuration()` — reject a technology it cannot slice
- [ ] `prepare_configuration()` — the full translation table
- [ ] `slice()` — argv, scrubbed env, timeout, output cap, real errors
- [ ] `validate_output()` — non-empty, plausible
- [ ] Tests marked `slow`, skipped when the engine is absent
- [ ] A slicing test asserting layer count, Z extent and bed bounds

**Do not copy business logic between adapters.** Profile resolution, parameter
optimisation, compatibility checks and G-code validation live in the core. An
adapter that re-implements them will drift from the core and from the other
adapters.

## 5. Model preparation is not optional

Engines disagree about where a mesh ends up. Two bugs found during development,
both silent, both printing the wrong part:

| Bug | Symptom | Root cause |
| --- | --- | --- |
| Model not lifted | A 20 mm cube produced a 10 mm toolpath, 33 layers instead of 67, with no warning | The engine does not auto-drop a mesh onto the plate. Everything below Z=0 is clipped. |
| Double centring | The brim printed at X 210–229 on a 220 mm bed | The mesh was pre-centred on the bed, then the engine centred it again. |

The fix is `app/services/geometry/placement.py`, which emits a canonical pose:
**bottom face on the plate, centred on the build origin**. The engine then maps
the origin onto its own coordinate frame. Preparation never modifies the
original upload and never scales without `allow_scale=True`.

## 6. Verified behaviour: CuraEngine 5.13

Established by reading `CuraEngine help` and confirmed by experiment.

### Licence — this corrects an earlier assumption in this project

CuraEngine 5.13 reports **AGPL-3.0**:

> This program is free software: you can redistribute it and/or modify it under
> the terms of the GNU Affero General Public License …

This is stricter than the LGPL that older Cura releases used. AGPL carries
network-use copyleft. Running it as a separate process keeps this project's
source from becoming a derivative work, but **a legal review is required before
any commercial deployment**. `get_capabilities()["licence_review_required"]` is
`True` and the platform reports it rather than hiding it. OrcaSlicer and
PrusaSlicer are GPL-3.0 with the same consideration.

### Command line

```
CuraEngine slice [-v] [-p] [-j <settings.def.json>] [-s <key>=<value>] ...
                 [-o <output.gcode>] [-l <model.stl>]
```

* `slice` is a **required leading subcommand**.
* `-j` registers a settings *definition* file. Without `fdmprinter.def.json` no
  settings are known, every lookup fails with *"Trying to retrieve setting with
  no value given"*, and the engine writes an empty file.
* `-s key=value`, repeated. This is the only form that round-trips multi-line
  values such as start/end G-code. `-r` (resolved values as JSON) is not
  reliable — it ignores Cura's section nesting.
* `-l` loads the model; a bare filename is an unknown command.
* No shell. Settings are validated as scalars before reaching argv.

### Settings

The baseline is derived from `fdmprinter.def.json` — all 718 settings with
their `default_value` — rather than hand-listed, so a Cura upgrade does not
break the adapter. Keys the schema does not define are dropped and logged.

Cura 5.13 renamed much of its motion and shell vocabulary. Current names:

| Our parameter | Cura 5.13 |
| --- | --- |
| layer height | `layer_height` (first layer `layer_height_0`) |
| extrusion width | `line_width` |
| wall count | `wall_line_count` |
| top/bottom layers | `top_bottom_thickness` (**millimetres**, converted from a count) |
| print speed | `speed_print` (not `print_speed`) |
| travel speed | `speed_travel` |
| acceleration | `machine_acceleration` / `acceleration_print` |
| nozzle temperature | `material_print_temperature` |
| bed temperature | `material_bed_temperature` |
| part cooling | `cool_fan_speed` (not `material_print_fan_speed`) |
| start / end G-code | `machine_start_gcode` / `machine_end_gcode` |
| dialect | `machine_gcode_flavor` |

### Coordinate frame

`machine_profiles.machine_restrictions.origin_convention` decides it, and both
the slicer settings and the validator read the same field:

* `corner` (default) — bed spans `0..build`, the stock Marlin/RepRap convention.
  Cura: `machine_center_is_zero = False`.
* `center` — bed spans `-build/2..+build/2`. Cura: `machine_center_is_zero = True`.

Getting this wrong is not cosmetic: it makes every out-of-bounds finding either
absent or spurious.

### Known gaps

* **Wall-count and infill-density fidelity is unverified.** A 20 mm cube at
  0.3 mm produces a correct 67-layer, full-height, on-bed toolpath that passes
  validation, but the extruded volume is lower than a hand calculation for the
  requested `wall_line_count` predicts. The mapping is suspected to involve
  Cura's `skin_outline_count` and per-extruder scoping. **Do not assume wall
  thickness is honoured** until measured against a caliper-printed coupon.
  This is an open Phase 1 gap.
* Cura's `;Filament used:` and `;MINX/MAXX:` header comments are unreliable
  without a full printer definition. The platform computes filament from the E
  axis instead (`stats["filament_extruded_mm"]`).
* Only the fixture set has been sliced end to end; no organic or multi-part
  model has been validated.

## 7. Registry and routing

Adapters register by capability. Selection is data-driven:

```
technology  ->  candidates
            ->  filter by (material, output format, required features)
            ->  filter by profile validation_status
            ->  prefer the adapter that has a validated profile for this machine
            ->  no candidate  =>  InsufficientInformationError
```

Never `if printer == "Ender"` in application logic. A machine is a profile row;
a profile row names a technology and a dialect; the registry resolves the rest.
