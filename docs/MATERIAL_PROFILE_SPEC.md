# Material Profile Specification

A material is **data**. Two levels again: a *family* (`materials`) and a
**versioned profile** (`material_profiles`). Two PLA filaments from different
manufacturers are different profiles, because they are not interchangeable.

## 1. Family vs profile

`materials` — the family: `pla`, `petg`, `abs`, `asa`, `tpu95a`, `nylon`, `pc`,
`pa-cf`, `pla-cf`, `petg-cf`, `pa6-gf`, … with `is_composite`.

`material_profiles` — the printable thing: vendor, SKU, version, and every
number below. Seeded with 12 profiles across 11 families.

## 2. Required fields

`nozzle_temp` (JSON) — required. `{min, typical, max}` in °C. The optimizer
clamps `typical` into this range **and** into the machine's rating, whichever is
narrower. A locked user value is still clamped by the machine, never by the
material.

`bed_temp` (JSON) — required, same shape.

`layer_height_range` (JSON) — required. `{min, max}`. An upper bound the
optimizer will not exceed.

`max_volumetric_flow_mm3s` — the material's melt capability. If this exceeds
the machine's, speeds are reduced and a warning is emitted.

`retraction` (JSON) — `{distance_mm, speed_mm_s, minimum_layer_mm}`.

`print_speed` (JSON) — `{min, max, travel_max}`.

`adhesion` (JSON) — `{bed_surfaces[], recommended, warping_tendency,
moisture_sensitivity}`. `warping_tendency` drives the automatic brim decision.

`abrasiveness` (enum `none|low|medium|high`) — drives the hardened-nozzle
warning. Composites must be `high`.

`requires_enclosure` (bool) — when true and the machine has no heated chamber,
the configuration is **blocked**, not merely warned. Printing nylon or PC in an
open chamber produces a warped, delaminated part.

`moisture_sensitivity` (`low|medium|high|very_high`) — informational, and the
basis for the "dry your filament" note.

`mechanical` (JSON), `shrinkage_pct`, `glass_transition_c`, `manufacturer_notes`
— informational, surfaced in the UI.

## 3. Versioning

`profile_version` + `profile_hash` (SHA-256 of content). Seeding is idempotent
on the hash. A change to any value creates a new version; a `slice_job` stores
the exact `material_profile_id` it ran against.

## 4. Adding a material

1. Add a family entry if new.
2. Add a profile with every required field above.
3. Test the temperature ordering
   (`min < typical <= max`) and that a composite is `high` abrasiveness.
4. `tests/test_api.py::test_material_profiles_carry_real_constraints` covers the
   invariants that must hold for every profile.

## 5. Honesty requirements

* Never present a profile as authoritative when it is a generic estimate. The
  seeded generic profiles say so in `manufacturer_notes`.
* Never invent a manufacturer SKU. If the filament is not identified, use the
  `generic` vendor rather than guessing a brand.
* `vendor` + `brand_sku` are part of the profile's identity. "PLA" from two
  vendors is two profiles.
