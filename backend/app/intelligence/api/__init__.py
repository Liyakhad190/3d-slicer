"""AI HTTP surface.

Intentionally does not re-export `router`: that name would shadow the
`app.intelligence.api.router` submodule on `from app.intelligence.api import
router`, which is an easy source of confusing import errors. Callers import the
module path explicitly.
"""