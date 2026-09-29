# Printer Profile Specification

A printer is **data, never code**. Adding a machine means inserting a row (or a
seed entry); it must never mean adding a conditional to the application.

## 1. The two levels

| Level | Table | Meaning |
| --- | --- | --- |
| Catalogue | `machines` | A machine *product* exists: vendor, model, technology, motion system, documentation link, known limitations. |
| Capability | `machine_profiles` | A **versioned** set of numbers: build volume, limits, G-code. Immutable. |

One `machines` row can have many `machine_profiles` versions. A user's
`printers` row points at **one specific version**, so a profile change never
silently redefines a job that already ran.

## 2. Required fields

`build_volume` (JSON) — required, all three axes in mm.

```json
{ "x": 220.0, "y": 220.0, "z": 250.0 }
```

`temperature_limits` (JSON) — required. The hard thermal envelope. The
optimizer may never propose a value outside it, and the G-code validator blocks
any file that exceeds it.

```json
{
  "nozzle": { "min": 0, "max": 260 },
  "bed":    { "min": 0, "max": 100 },
  "chamber":{ "min": 0, "max": 0 }
}
```

`speed_limits` (JSON) — required.

```json
{
  "max_print_speed_mm_s": 100,
  "max_travel_speed_mm_s": 150,
  "max_acceleration_mm_s2": 500,
  "max_accel_to_accel_mm_s2": 750
}
```

`max_volumetric_flow_mm3s` — the machine's melt ceiling. The optimizer
reduces speed when the material's recommended flow exceeds it, and the
validator warns on any toolpath move that exceeds it.

`nozzle_diameters` — the list of nozzles the machine physically accepts. A
printer cannot be registered with a nozzle outside this list; this is a machine
constraint, not a preference, and is not overridable by the optimizer or by an
AI proposal.

`machine_restrictions` (JSON) — machine-specific constraints the optimizer must
respect:

| Key | Effect |
| --- | --- |
| `no_abrasive_filament_with_brass_path` | Warn when an abrasive material meets a brass extrusion path |
| `direct_drive_only_materials` | Warn that a Bowden machine may buckle flexible filament |
| `origin_convention` | `corner` (0..build) or `center` (-build/2..build/2). **Drives both the slicer and the validator** |

`start_gcode` / `end_gcode` — free text, machine-specific. Emitted verbatim.
Security note: these are injected into every output file, so a profile
contributor can inject arbitrary G-code. A profile carrying an unreviewed
start/end G-code must not be promotable to `validated`, and any future import
path must run these through the command whitelist before accepting them.

`gcode_dialect_id` — selects the validator's command vocabulary. A dialect with
no thermal commands (e.g. GRBL) disables temperature checks.

## 3. Versioning and validation

`profile_version` — monotonic integer per machine.
`profile_hash` — SHA-256 of the content. Seeding is idempotent on the hash, so
the same data never creates a duplicate version.
`status` — `draft | validated | deprecated`.
`validation_status` — `unvalidated | validated | rejected`.

**The seeded profiles ship as `unvalidated`.** This is deliberate: the platform
refuses to generate output for a machine profile that has not been bench-tested
on real hardware, and nothing in a data file can honestly claim otherwise.
Promotion to `validated` records `validated_at` and `validated_by`.

Editing a profile inserts a new version. It never updates a row in place, so
re-opening a year-old job shows the machine as it was.

## 4. Source and verification

Every profile should carry, in its `changelog` or a `source` field:

* where the numbers came from (manufacturer datasheet, community wiki, measured)
* the date they were last checked
* who checked them

A community-maintained profile is fine — it just has to be labelled as such,
and the platform surfaces `validation_status` in the UI rather than presenting
an unvalidated profile as a guarantee.

## 5. Adding a printer

1. Add a `machines` entry (vendor, model, technology, motion system, doc link,
   known limitations).
2. Add a `machine_profiles` entry with every required field above.
3. Run the regression test `tests/test_api.py::test_machines_and_profiles_are_seeded`
   and a new test asserting the profile's numbers.
4. Slice a known coupon, measure it, and only then set
   `validation_status = 'validated'`.

No application code changes. Ever.

## 6. Current seed set

Five machines, all `unvalidated`: Creality Ender 3, Prusa MK4S, Bambu Lab X1
Carbon, Bambu Lab P1S, Voron 2.4 (350).

The Voron entry is a community approximation of a DIY build and is labelled as
such in its `known_limitations`; the exact machine depends on the BOM used, so
it must be validated against the specific unit.
