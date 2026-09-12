"""Optional API layer for serving HR Edge Lab results.

Not required for the core pipeline (ingestion -> models -> reports ->
database) to function. Add a lightweight framework (e.g. FastAPI) here
later if external consumers (including a future MLB Edge Lab export
integration) need programmatic access.
"""
