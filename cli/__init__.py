from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from pathlib import Path
import re
import subprocess

import click
import duckdb
from jinja2 import Environment, StrictUndefined, meta, nodes
from jinja2.exceptions import TemplateError
from jinja2.visitor import NodeVisitor
import tomllib
from tomlkit import aot, comment, document, dumps, table

APP_NAME = "kt"
DB_FILENAME = "templates.duckdb"
COMMAND_PATTERN = re.compile(r"\{>(.+?)<\}", re.DOTALL)

CREATE_TEMPLATES_SQL = """
CREATE TABLE IF NOT EXISTS templates (
    name TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _ensure_connection():
    db_path = Path(click.get_app_dir(APP_NAME))
    db_path.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path / DB_FILENAME))
    connection.execute(CREATE_TEMPLATES_SQL)
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


def _build_toml_template(source: str) -> str:
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

    rendered = dumps(doc).strip()
    return f"{rendered}\n" if rendered else ""


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
            content = click.edit("", extension=".j2", editor="nvim")
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
        updated = click.edit(template["content"], extension=".j2", editor="nvim")
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

    toml_seed = _build_toml_template(template["content"]) or "# No variables detected\n"
    context_source = click.edit(toml_seed, extension=".toml", editor="nvim")
    if context_source is None:
        raise click.ClickException("Editor closed without saving variables.")

    try:
        context_data = tomllib.loads(context_source)
    except tomllib.TOMLDecodeError as exc:
        raise click.ClickException(f"Invalid TOML: {exc}") from exc

    env = Environment(undefined=StrictUndefined)
    rendered = ""
    try:
        rendered = env.from_string(template["content"]).render(**context_data)
    except TemplateError as exc:
        raise click.ClickException(f"Failed to render template: {exc}") from exc

    rendered = _substitute_command_blocks(rendered)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output.write_text(rendered)
        except OSError as exc:
            raise click.ClickException(f"Failed to write output: {exc}") from exc
        click.echo(f"Rendered template saved to '{output}'.")
    else:
        click.echo(rendered)
