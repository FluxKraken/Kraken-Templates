# Repository Guidelines

## Project Structure & Module Organization
- `cli/__init__.py` contains the entire Click CLI, template/recipe persistence, and Jinja/TOML utilities; treat it as the single source of truth for business logic.
- `pyproject.toml` registers the `kt` console script and tracks dependencies (click, textual, duckdb, jinja2, tomlkit); update this before shipping new modules.
- Runtime data is stored in `~/.config/kt/templates.duckdb`; schema updates must remain backward-compatible because existing installs reuse this file.
- `README.md` documents user-facing commands—mirror wording there whenever you change CLI behavior.

## Build, Test, and Development Commands
- `uv tool install .` installs the local CLI for smoke-testing in an isolated environment.
- `uv run kt --help` verifies the entry point loads and prints the command tree.
- `uv run kt add demo && uv run kt render demo --output /tmp/demo.txt` exercises template CRUD and rendering; delete test templates afterward with `kt delete demo`.
- `uv run kt recipe render sample` is the canonical way to test recipe flows; pair it with `kt recipe list` when validating new actions.

## Coding Style & Naming Conventions
- Stick to Python 3.14 features already in use (type annotations, `Path`, `dict[str, str]` syntax) and 4-space indentation; avoid introducing other style guides without tooling.
- Use `snake_case` for Python identifiers and `kebab-case` or `snake_case` for template and recipe names to align with existing commands (`kt recipe add db-env`).
- Keep CLI output concise and actionable; prefer `click.echo` plus `ClickException` for error paths and log meaningful names (e.g., template/recipe being touched).

## Testing Guidelines
- There is no automated suite yet; rely on scenario tests that mirror the README workflows (add/edit/delete templates, render with TOML prompts, run recipes mixing template/command/prompt actions).
- When changing DuckDB queries or schema, manually check migrations by starting from a real `templates.duckdb` containing templates and recipes, then re-running the CLI commands above.
- Capture regressions early by rendering templates with `{>command<}` placeholders and verifying the warning in the README still applies.

## Commit & Pull Request Guidelines
- Follow the existing git style: short, imperative commits such as `Add Nested Toml Dict expansion`; limit the subject to ~72 characters and focus on what the change does.
- Every PR should describe the scenario it enables, outline manual test steps (`uv run kt render ...`), and mention any migrations to `templates.duckdb` or new env vars like `KT_EDITOR`.
- Link related issues, include screenshots or terminal transcripts for UX tweaks, and update `README.md` whenever flags, prompts, or recipe behaviors change.
