"""AI print preparation: model + profiles + objective -> a validated print plan.

The package is the realisation of one invariant:

    THE LLM MAY PROPOSE. THE DETERMINISTIC SYSTEM MUST VALIDATE.
    THE SLICER GENERATES MACHINE CODE. THE MACHINE CODE IS VALIDATED AGAIN.

`analysis`, `orientation` and `placement` measure the mesh without a model in
the loop. `schemas` is the only shape a model may answer in. `validators`
re-derives every number from verified printer, material, nozzle and slicer
data. `planner` runs the propose -> validate -> replan loop. Nothing here
executes G-code, and nothing here bypasses `app.slicing`.
"""
