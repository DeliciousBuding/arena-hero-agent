# Contributing

Thanks for contributing to Arena Hero Agent.

## Development workflow

1. Create a focused branch or repository-local worktree.
2. Keep changes inside one architectural boundary whenever possible.
3. Add tests for behavior, contracts, persistence, parsing, or API changes.
4. Run the complete quality gate before requesting review.
5. Update public documentation when a supported interface or workflow changes.

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest -q
```

## Design expectations

- Prefer small replaceable ports over framework-dependent abstractions.
- Keep domain code deterministic and free from I/O.
- Do not duplicate contracts owned by the Python SDK or the Lab.
- Do not mix behavior migration with strategy retuning in the same change.
- Preserve explicit uncertainty in differential results.

## Community standards

All contributors are expected to follow the [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Harassment
or other unacceptable behavior may be reported to the maintainers through the repository's GitHub
Security tab.

## Public content

Do not include credentials, production logs, private endpoints, workstation paths, or internal
coordination notes in issues, fixtures, documentation, or commits.
