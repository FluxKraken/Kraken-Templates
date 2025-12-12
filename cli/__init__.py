from __future__ import annotations

from collections import defaultdict
from collections.abc import MutableMapping
from contextlib import closing
from pathlib import Path
import os
import re
import subprocess
from typing import Any

import click
import duckdb
from jinja2 import Environment, StrictUndefined, meta, nodes
from jinja2.exceptions import TemplateError
from jinja2.visitor import NodeVisitor
import tomllib
from tomlkit import aot, comment, document, dumps, table
from tomlkit.items import AoT

EDITOR = os.environ.get("KT_EDITOR")

APP_NAME = "kt"
DB_FILENAME = "templates.duckdb"
COMMAND_PATTERN = re.compile(r"\{>(.+?)<\}", re.DOTALL)
VARIABLE_PATTERN = re.compile(r"\$\((?P<name>[A-Za-z_][A-Za-z0-9_]*)\)")

CREATE_TEMPLATES_SQL = """
CREATE TABLE IF NOT EXISTS templates (
    name TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_RECIPES_SQL = """
CREATE TABLE IF NOT EXISTS recipes (
    name TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _default_recipe_content() -> str:
    return (
        "# Define the ordered actions for the recipe\n"
        "[[actions]]\n"
        "type = \"template\"\n"
        "name = \"example-template\"\n"
        "output = \"output.txt\"\n\n"
        "[[actions]]\n"
        "type = \"command\"\n"
        "command = [\"echo\", \"Hello from Kraken Templates\"]\n\n"
        "[[actions]]\n"
        "type = \"prompt\"\n"
        "var = \"name\"\n"
        "prompt = \"What is your name?\"\n"
    )


def _ensure_connection():
    db_path = Path(click.get_app_dir(APP_NAME))
    db_path.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path / DB_FILENAME))
    connection.execute(CREATE_TEMPLATES_SQL)
    connection.execute(CREATE_RECIPES_SQL)
    return connection


def _fetch_template(conn: duckdb.DuckDBPyConnection, name: str) -> dict[str, str]:
    row = conn.execute(
        "SELECT name, content FROM templates WHERE name = ?", [name]
    ).fetchone()
    if row is None:
        raise click.ClickException(f"Template '{name}' does not exist.")
    return {"name": row[0], "content": row[1]}


def _template_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    result = conn.execute("SELECT 1 FROM templates WHERE name = ?", [name]).fetchone()
    return result is not None


def _list_template_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute("SELECT name FROM templates ORDER BY name").fetchall()
    return [row[0] for row in rows]


def _fetch_recipe(conn: duckdb.DuckDBPyConnection, name: str) -> dict[str, str]:
    row = conn.execute(
        "SELECT name, content FROM recipes WHERE name = ?",
        [name],
    ).fetchone()
    if row is None:
        raise click.ClickException(f"Recipe '{name}' does not exist.")
    return {"name": row[0], "content": row[1]}


def _recipe_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    result = conn.execute("SELECT 1 FROM recipes WHERE name = ?", [name]).fetchone()
    return result is not None


def _list_recipe_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute("SELECT name FROM recipes ORDER BY name").fetchall()
    return [row[0] for row in rows]


class TemplateIntrospector(NodeVisitor):
    """Extract top-level, table, and list variables from a Jinja template."""

    def __init__(self) -> None:
        super().__init__()
        self.list_fields: dict[str, set[str]] = defaultdict(set)
        self.nested_fields: dict[str, set[str]] = defaultdict(set)
        self._scope_stack: list[set[str]] = [set()]
        self._loop_stack: list[tuple[set[str], str | None]] = []

    def visit_For(self, node: nodes.For) -> None:
        target_names = self._extract_target_names(node.target)
        iter_name = self._iterable_name(node.iter)

        self._loop_stack.append((target_names, iter_name))
        self._scope_stack.append(set(target_names) | {"loop"})

        for child in node.body or []:
            self.visit(child)

        self._scope_stack.pop()
        self._loop_stack.pop()

        for child in node.else_ or []:
            self.visit(child)

    def visit_Getattr(self, node: nodes.Getattr) -> None:
        self._register_access(node)
        self.generic_visit(node)

    def visit_Getitem(self, node: nodes.Getitem) -> None:
        self._register_access(node)
        self.generic_visit(node)

    def _register_access(self, node: nodes.Expr) -> None:
        parts = self._flatten_access(node)
        if len(parts) < 2:
            return

        base = parts[0]
        attr = parts[1]

        if self._is_local(base):
            iter_name = self._loop_iter_for_local(base)
            if iter_name:
                self.list_fields[iter_name].add(attr)
        else:
            self.nested_fields[base].add(attr)

    def _flatten_access(self, node: nodes.Expr) -> list[str]:
        parts: list[str] = []
        current = node
        while isinstance(current, (nodes.Getattr, nodes.Getitem)):
            if isinstance(current, nodes.Getattr):
                parts.append(current.attr)
                current = current.node
            else:
                key = self._literal_key(current.arg)
                if key is None:
                    break
                parts.append(key)
                current = current.node

        if isinstance(current, nodes.Name):
            parts.append(current.name)
            return list(reversed(parts))

        return []

    def _literal_key(self, node: nodes.Expr) -> str | None:
        if isinstance(node, nodes.Const) and isinstance(node.value, str):
            return node.value
        return None

    def _is_local(self, name: str) -> bool:
        return any(name in scope for scope in reversed(self._scope_stack))

    def _loop_iter_for_local(self, name: str) -> str | None:
        for targets, iter_name in reversed(self._loop_stack):
            if name in targets:
                return iter_name
        return None

    def _extract_target_names(self, target: nodes.Expr) -> set[str]:
        if isinstance(target, nodes.Name):
            return {target.name}
        if isinstance(target, (nodes.Tuple, nodes.List)):
            names: set[str] = set()
            for item in target.items:
                names |= self._extract_target_names(item)
            return names
        return set()

    def _iterable_name(self, node: nodes.Expr) -> str | None:
        if isinstance(node, nodes.Name):
            return node.name
        return None


def _build_toml_template(source: str, preset: dict[str, Any] | None = None) -> str:
    env = Environment()
    parsed = env.parse(source)

    global_vars = sorted(meta.find_undeclared_variables(parsed))
    inspector = TemplateIntrospector()
    inspector.visit(parsed)

    scalar_vars: list[str] = []
    table_vars: dict[str, set[str]] = {}

    for name in global_vars:
        if name in inspector.list_fields:
            continue
        if name in inspector.nested_fields:
            table_vars[name] = inspector.nested_fields[name]
        else:
            scalar_vars.append(name)

    doc = document()
    doc.add(comment("Update the values below and save to render the template."))

    for var in scalar_vars:
        doc[var] = ""

    for var, attrs in sorted(table_vars.items()):
        tbl = table()
        for attr in sorted(attrs):
            tbl[attr] = ""
        doc[var] = tbl

    for var, attrs in sorted(inspector.list_fields.items()):
        entries = aot()
        entry = table()
        values = attrs or {"value"}
        for attr in sorted(values):
            entry[attr] = ""
        entries.append(entry)
        doc[var] = entries

    if preset:
        _apply_preset_to_doc(doc, preset)

    rendered = dumps(doc).strip()
    return f"{rendered}\n" if rendered else ""


def _apply_preset_to_doc(target: MutableMapping[str, Any], preset: dict[str, Any]) -> None:
    for key, value in preset.items():
        if isinstance(value, dict):
            existing = target.get(key)
            if isinstance(existing, MutableMapping):
                _apply_preset_to_doc(existing, value)
            else:
                tbl = table()
                _apply_preset_to_doc(tbl, value)
                target[key] = tbl
        elif isinstance(value, list):
            existing = target.get(key)
            if isinstance(existing, AoT):
                existing.clear()
                for entry in value:
                    if isinstance(entry, dict):
                        row = table()
                        _apply_preset_to_doc(row, entry)
                        existing.append(row)
                    else:
                        existing.append(entry)
            else:
                target[key] = value
        else:
            target[key] = value


def _read_template_from_file(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise click.ClickException(f"Failed to read '{path}': {exc}") from exc


def _substitute_command_blocks(content: str) -> str:
    """Replace {>cmd<} blocks with the stdout of the shell command."""

    def _run(match: re.Match[str]) -> str:
        command = match.group(1).strip()
        if not command:
            raise click.ClickException(
                "Encountered empty command substitution block {><}."
            )

        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise click.ClickException(f"Failed to run command '{command}': {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise click.ClickException(
                f"Command '{command}' failed with exit code {completed.returncode}{detail}"
            )

        return completed.stdout.rstrip("\n")

    return COMMAND_PATTERN.sub(_run, content)


def _prompt_context_for_template(
    template_content: str, preset: dict[str, Any] | None = None
) -> dict[str, Any]:
    toml_seed = _build_toml_template(template_content, preset)
    if not toml_seed.strip():
        return preset or {}
    context_source = click.edit(toml_seed, extension=".toml", editor=EDITOR)
    if context_source is None:
        raise click.ClickException("Editor closed without saving variables.")

    try:
        return tomllib.loads(context_source)
    except tomllib.TOMLDecodeError as exc:
        raise click.ClickException(f"Invalid TOML: {exc}") from exc


def _render_template_content(template_content: str, context_data: dict[str, Any]) -> str:
    env = Environment(undefined=StrictUndefined)
    try:
        rendered = env.from_string(template_content).render(**context_data)
    except TemplateError as exc:
        raise click.ClickException(f"Failed to render template: {exc}") from exc

    return _substitute_command_blocks(rendered)


def _substitute_variables(text: str, variables: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in variables:
            raise click.ClickException(
                f"Unknown variable '{name}' referenced in recipe action."
            )
        return str(variables[name])

    return VARIABLE_PATTERN.sub(_replace, text)


def _coerce_command_value(value: Any) -> list[str | list[str]]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not value:
            raise click.ClickException(
                "Command actions must provide a non-empty 'command' value."
            )
        if all(isinstance(item, str) for item in value):
            return [value]
        normalized: list[str | list[str]] = []
        for item in value:
            if isinstance(item, str):
                normalized.append(item)
            elif (
                isinstance(item, list)
                and item
                and all(isinstance(arg, str) for arg in item)
            ):
                normalized.append(item)
            else:
                raise click.ClickException(
                    "Command actions must provide strings, lists of strings, "
                    "or a list combining those command definitions."
                )
        return normalized
    raise click.ClickException(
        "Command actions must provide 'command' as a string, list of strings, "
        "or list of command definitions."
    )


def _resolve_context_values(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        resolved = _substitute_variables(value, variables)
        if resolved == value and value in variables:
            return variables[value]
        return resolved
    if isinstance(value, list):
        return [_resolve_context_values(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_context_values(val, variables) for key, val in value.items()}
    return value


def _expand_dotted_context_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Convert dictionaries that use dotted keys into nested mappings."""

    expanded: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            value = _expand_dotted_context_keys(value)

        parts = key.split(".")
        target: MutableMapping[str, Any] = expanded
        for part in parts[:-1]:
            existing = target.get(part)
            if existing is None:
                existing = {}
                target[part] = existing
            elif not isinstance(existing, MutableMapping):
                raise click.ClickException(
                    f"Context key '{key}' conflicts with previously defined scalar '{part}'."
                )
            target = existing

        leaf = parts[-1]
        existing = target.get(leaf)
        if isinstance(existing, MutableMapping) and not isinstance(value, MutableMapping):
            raise click.ClickException(
                f"Context key '{key}' cannot override nested values under '{leaf}'."
            )
        if isinstance(existing, MutableMapping) and isinstance(value, MutableMapping):
            existing.update(value)
        else:
            target[leaf] = value

    return expanded


def _load_recipe_actions(content: str) -> list[dict[str, Any]]:
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise click.ClickException(f"Invalid recipe TOML: {exc}") from exc

    actions = parsed.get("actions")
    if not isinstance(actions, list) or not actions:
        raise click.ClickException(
            "Recipe must define at least one [[actions]] entry."
        )

    normalized: list[dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise click.ClickException(f"Action #{index} must be a TOML table.")
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type:
            raise click.ClickException(f"Action #{index} is missing a 'type'.")
        normalized.append(action)

    return normalized


def _should_run_action(
    action: dict[str, Any], variables: dict[str, str], index: int
) -> bool:
    gate_value = action.get("gate")
    if gate_value is None:
        return True
    if not isinstance(gate_value, str) or not gate_value:
        raise click.ClickException(
            f"Action #{index} gate must be a non-empty string when provided."
        )
    prompt_text = _substitute_variables(gate_value, variables)
    confirmed = click.confirm(
        f"[{index}] {prompt_text}", default=True, show_default=True
    )
    if not confirmed:
        click.echo(f"[{index}] Skipping action.")
    return confirmed


def _execute_recipe_actions(
    conn: duckdb.DuckDBPyConnection, actions: list[dict[str, Any]]
) -> None:
    variables: dict[str, str] = {}
    for index, action in enumerate(actions, start=1):
        if not _should_run_action(action, variables, index):
            continue
        action_type = action.get("type")
        if action_type == "template":
            _run_template_action(conn, action, variables, index)
        elif action_type == "command":
            _run_command_action(action, variables, index)
        elif action_type == "prompt":
            _run_prompt_action(action, variables, index)
        else:
            raise click.ClickException(
                f"Unsupported action type '{action_type}' at position {index}."
            )


def _run_template_action(
    conn: duckdb.DuckDBPyConnection,
    action: dict[str, Any],
    variables: dict[str, str],
    index: int,
) -> None:
    template_name = action.get("name")
    if not isinstance(template_name, str) or not template_name:
        raise click.ClickException(
            f"Template action #{index} must include a non-empty 'name'."
        )

    template = _fetch_template(conn, template_name)
    click.echo(f"[{index}] Rendering template '{template_name}'.")
    preset_context: dict[str, Any] | None = None
    context_override = action.get("context")
    if context_override is not None:
        if not isinstance(context_override, dict):
            raise click.ClickException(
                f"Template action #{index} expected 'context' to be a table."
            )
        resolved = _resolve_context_values(context_override, variables)
        if isinstance(resolved, dict):
            preset_context = _expand_dotted_context_keys(resolved)
        else:
            raise click.ClickException(
                f"Template action #{index} context must resolve to a table."
            )
    context_data = _prompt_context_for_template(template["content"], preset_context)
    rendered = _render_template_content(template["content"], context_data)

    output_value = action.get("output")
    if output_value is None:
        click.echo(rendered)
        return

    if not isinstance(output_value, str) or not output_value:
        raise click.ClickException(
            f"Template action #{index} must supply 'output' as a non-empty string."
        )

    resolved_path = _substitute_variables(output_value, variables)
    output_path = Path(resolved_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(rendered)
    except OSError as exc:
        raise click.ClickException(
            f"Failed to write template output to '{output_path}': {exc}"
        ) from exc

    click.echo(f"[{index}] Saved output to '{output_path}'.")


def _run_command_action(action: dict[str, Any], variables: dict[str, str], index: int) -> None:
    if "command" not in action:
        raise click.ClickException(
            f"Command action #{index} must define a 'command' field."
        )

    command_sequence = _coerce_command_value(action["command"])
    env = os.environ.copy()
    env.update({key: str(value) for key, value in variables.items()})

    for command_value in command_sequence:
        try:
            if isinstance(command_value, str):
                command_text = _substitute_variables(command_value, variables)
                completed = subprocess.run(
                    command_text,
                    shell=True,
                    check=False,
                    env=env,
                )
            else:
                args = [_substitute_variables(arg, variables) for arg in command_value]
                completed = subprocess.run(args, check=False, env=env)
        except OSError as exc:
            raise click.ClickException(
                f"Failed to run command action #{index}: {exc}"
            ) from exc

        if completed.returncode != 0:
            raise click.ClickException(
                f"Command action #{index} exited with code {completed.returncode}."
            )

    click.echo(f"[{index}] Command completed successfully.")


def _run_prompt_action(
    action: dict[str, Any], variables: dict[str, str], index: int
) -> None:
    prompt_text = action.get("prompt")
    var_name = action.get("var")
    if not isinstance(prompt_text, str) or not prompt_text:
        raise click.ClickException(
            f"Prompt action #{index} must include a non-empty 'prompt'."
        )
    if not isinstance(var_name, str) or not var_name:
        raise click.ClickException(
            f"Prompt action #{index} must include a non-empty 'var'."
        )

    default_value = action.get("default")
    kwargs: dict[str, Any] = {}
    if default_value is not None:
        kwargs["default"] = str(default_value)
        kwargs["show_default"] = True

    value = click.prompt(prompt_text, **kwargs)
    variables[var_name] = value
    click.echo(f"[{index}] Stored variable '{var_name}'.")


@click.group()
def kt() -> None:
    """Kraken Template CLI."""


@kt.command(name="list")
def list_templates() -> None:
    """List stored templates."""
    with closing(_ensure_connection()) as conn:
        names = _list_template_names(conn)
    if not names:
        click.echo("No templates stored yet.")
        return
    for name in names:
        click.echo(f"- {name}")


@kt.command()
@click.argument("name")
@click.option(
    "-f",
    "--file",
    "file_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Seed the template content from a file.",
)
def add(name: str, file_path: Path | None) -> None:
    """Create a new template."""
    with closing(_ensure_connection()) as conn:
        if _template_exists(conn, name):
            raise click.ClickException(f"Template '{name}' already exists.")

        if file_path is not None:
            content = _read_template_from_file(file_path)
        else:
            content = click.edit("", extension=".j2", editor=EDITOR)
            if content is None:
                raise click.ClickException("Editor closed without saving content.")

        if not content.strip():
            raise click.ClickException("Template content cannot be empty.")

        conn.execute(
            "INSERT INTO templates (name, content) VALUES (?, ?)", [name, content]
        )
    click.echo(f"Template '{name}' created.")


@kt.command()
@click.argument("name")
def edit(name: str) -> None:
    """Edit an existing template."""
    with closing(_ensure_connection()) as conn:
        template = _fetch_template(conn, name)
        updated = click.edit(template["content"], extension=".j2", editor=EDITOR)
        if updated is None:
            raise click.ClickException("Editor closed without saving changes.")
        if updated == template["content"]:
            click.echo("No changes detected; template left untouched.")
            return
        conn.execute(
            "UPDATE templates SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            [updated, name],
        )
    click.echo(f"Template '{name}' updated.")


@kt.command()
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def delete(name: str, yes: bool) -> None:
    """Remove a template."""
    with closing(_ensure_connection()) as conn:
        if not _template_exists(conn, name):
            raise click.ClickException(f"Template '{name}' does not exist.")

    if not yes:
        click.confirm(f"Delete template '{name}'?", abort=True)

    with closing(_ensure_connection()) as conn:
        conn.execute("DELETE FROM templates WHERE name = ?", [name])

    click.echo(f"Template '{name}' deleted.")


@kt.command()
@click.argument("name")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write the rendered content to this file.",
)
def render(name: str, output: Path | None) -> None:
    """Render a template with TOML variables."""
    with closing(_ensure_connection()) as conn:
        template = _fetch_template(conn, name)

    context_data = _prompt_context_for_template(template["content"])
    rendered = _render_template_content(template["content"], context_data)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output.write_text(rendered)
        except OSError as exc:
            raise click.ClickException(f"Failed to write output: {exc}") from exc
        click.echo(f"Rendered template saved to '{output}'.")
    else:
        click.echo(rendered)


@kt.group()
def recipe() -> None:
    """Manage stored recipes."""


@recipe.command(name="list")
def list_recipes() -> None:
    with closing(_ensure_connection()) as conn:
        names = _list_recipe_names(conn)

    if not names:
        click.echo("No recipes stored yet.")
        return

    for name in names:
        click.echo(f"- {name}")


@recipe.command(name="add")
@click.argument("name")
@click.option(
    "-f",
    "--file",
    "file_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Seed the recipe definition from a file.",
)
def add_recipe(name: str, file_path: Path | None) -> None:
    with closing(_ensure_connection()) as conn:
        if _recipe_exists(conn, name):
            raise click.ClickException(f"Recipe '{name}' already exists.")

        if file_path is not None:
            content = _read_template_from_file(file_path)
        else:
            content = click.edit(
                _default_recipe_content(), extension=".toml", editor=EDITOR
            )
            if content is None:
                raise click.ClickException("Editor closed without saving content.")

        if not content.strip():
            raise click.ClickException("Recipe content cannot be empty.")

        conn.execute(
            "INSERT INTO recipes (name, content) VALUES (?, ?)",
            [name, content],
        )

    click.echo(f"Recipe '{name}' created.")


@recipe.command(name="edit")
@click.argument("name")
def edit_recipe(name: str) -> None:
    with closing(_ensure_connection()) as conn:
        recipe_data = _fetch_recipe(conn, name)
        updated = click.edit(recipe_data["content"], extension=".toml", editor=EDITOR)
        if updated is None:
            raise click.ClickException("Editor closed without saving changes.")
        if updated == recipe_data["content"]:
            click.echo("No changes detected; recipe left untouched.")
            return
        conn.execute(
            "UPDATE recipes SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            [updated, name],
        )

    click.echo(f"Recipe '{name}' updated.")


@recipe.command(name="delete")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def delete_recipe(name: str, yes: bool) -> None:
    with closing(_ensure_connection()) as conn:
        if not _recipe_exists(conn, name):
            raise click.ClickException(f"Recipe '{name}' does not exist.")

    if not yes:
        click.confirm(f"Delete recipe '{name}'?", abort=True)

    with closing(_ensure_connection()) as conn:
        conn.execute("DELETE FROM recipes WHERE name = ?", [name])

    click.echo(f"Recipe '{name}' deleted.")


@recipe.command(name="render")
@click.argument("name")
def render_recipe(name: str) -> None:
    with closing(_ensure_connection()) as conn:
        recipe_data = _fetch_recipe(conn, name)
        actions = _load_recipe_actions(recipe_data["content"])
        _execute_recipe_actions(conn, actions)
