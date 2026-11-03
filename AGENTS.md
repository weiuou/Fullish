# Repository Guidelines

## Project Structure & Module Organization

Fullish is a Python 3.13 QQ bot built with NoneBot2 and FastAPI. Keep it a modular monolith.

- `src/fullish/`: application, conversations, policies, and MCP integrations. `collection/` parses incoming messages; `sending/` represents and encodes outgoing messages; `generation/` provides media generation.
- `tests/`: pytest suites and isolated deployment/container smoke checks.
- `scripts/` and `deploy/`: checks, deployment tooling, and container definitions.
- `docs/`: architecture, configuration, and operational guides; `docs/assets/` contains documentation images.

## Build, Test, and Development Commands

Use uv **0.13.1** and the Python version in `.python-version`.

```sh
uv sync --locked --group deploy          # Install development and deployment dependencies
uv run --locked python -m fullish        # Start locally; defaults to port 8080
uv build                                # Build distribution archives
uv run --locked ruff check .             # Lint
uv run --locked ruff format --check .    # Verify formatting
uv run --locked --group deploy pyright  # Check types
uv run --locked --group deploy pytest   # Run tests
python scripts/check_docs.py            # Validate local documentation links and assets
```

Without credentials, the application runs in foundation mode.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions/modules, and `PascalCase` classes. Ruff targets Python 3.13 with a 100-character line limit and import sorting. Preserve LF endings in shell files. Update `uv.lock` alongside dependency declarations.

## Testing Guidelines

Prioritize a few representative scenario replays with correlated traces over numerous isolated unit tests. Exercise real internal paths from input through authorization, model/tools, delivery acknowledgement, and resulting state. Assert meaningful outcomes and failure behavior; logs alone are insufficient. Mock external boundaries, not internal collaborators; distinguish simulated replays from live integration evidence. Use `tests/test_<feature>.py` and `test_<behavior>`; see `tests/test_message_flow.py`.

Do not chase coverage percentages or add implementation-mirroring tests. Add focused unit tests only for critical logic poorly covered by scenarios. Keep existing checks passing. Use synthetic/anonymized inputs and credential-free defaults; live checks require controlled integration environments. Run deployment smoke checks for deployment changes; `tests/setup_smoke.sh` requires an isolated temporary container.

## Commit & Pull Request Guidelines

History commonly uses `feat:` and `fix:` prefixes; use imperative subjects. Keep PRs focused. Follow the PR template: explain behavior, validation, and configuration/migration/rollback impact. Link relevant issues and report scenario outcomes with trace evidence. Document interface, storage, deployment-permission, or scheduling designs in an issue before implementation.

## Security & Architecture Boundaries

Never commit credentials, production configuration, or unauthorized chat records. Recheck group/tool permissions before execution and delivery; content generators should return candidates through the shared sending path. Preserve confirmed-send history semantics. Consult [architecture](docs/architecture.md) and [contribution guidance](CONTRIBUTING.md); the [proactive messaging proposal](docs/proactive-messaging.md) describes planned capabilities, not implemented behavior.

## Documentation Lifecycle

At task start and before delivery, check relevant active docs against code and agreed scope. Update valid guides in place; move superseded documents to `docs/archive/YYYY-MM-DD-topic.md`. Record archive date, reason, original path/revision, and replacement; repair links and update the [archive index](docs/archive/README.md). Separate current behavior from proposals. Archives are historical evidence, not current guidance: exclude `docs/archive/` from routine context gathering and searches; read specific entries only for historical investigation. Do not load all docs merely to check freshness.

Validate cross-references when editing, renaming, or archiving documents: check target paths, section anchors, inbound links, moved documents' relative links, and whether cited content supports the surrounding claim. Current guidance should reference current replacements; label intentional historical references. Run `python scripts/check_docs.py` for local paths/assets; review anchors and reference meaning separately because the script does not validate them. Verify changed external references against their source when relevant.
