## Summary

<!-- Describe the change, the problem it solves, and the affected surface. -->

## Checklist

- [ ] Change is scoped to one architectural boundary.
- [ ] Tests cover new or changed behavior, contracts, or persistence.
- [ ] Local quality gate passes: `uv sync --locked --all-groups`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run ty check`, `uv run pytest -q`.
- [ ] No credentials, production logs, private endpoints, or local absolute paths are included.
- [ ] No live submission, tenant mutation, or production deployment is triggered by this change.
- [ ] Public documentation is updated when a supported interface or workflow changes.