# Kraken Template Tool

A very simple templating tool using Jinja2 Syntax.

## Usage

- Add template - `kt add [name]`  
- Edit template - `kt edit [name]`  
- Delete template - `kt delete [name]`  
- Render Template - `kt render [name] [--output path]`  
- List Templates - `kt list`

## Recipe Automation

- Add recipe - `kt recipe add [name]`
- Edit recipe - `kt recipe edit [name]`
- Delete recipe - `kt recipe delete [name]`
- Render recipe - `kt recipe render [name]`
- List recipes - `kt recipe list`

## Installation

Install with the UV package manager:

```bash
uv tool install https://github.com/FluxKraken/Kraken-Templates.git
```

### Default Editor

The environment variable KT_EDITOR is checked for the preferred editor.  If not set, the system defaults to $EDITOR.

## Command Syntax

### Security warning

> **WARNING:** This feature can execute arbitrary shell commands on your machine.
> Never run a template from an untrusted source without inspecting it first.

The Jinja2 syntax has been extended to allow a shell command to be executed.  In this case the output of the command is substituted for the placeholder.

### Example:

```j2
BETTER_AUTH_SECRET='{>openssl rand -base64 32<}'
```

This will run the command `openssl rand -base64 32` and replace the placeholder `{>openssl rand -base64 32<}` with the output of the command.

The resulting output will be:

```env
BETTER_AUTH_SECRET='CWeNHmEvYd/j77qDafzqYpEQ/cpelr7jODOAINEBIvs='
```

You can also use standard Jinja2 syntax:

Examples:

```j2
Hello {{ name }}

{% for item in items %}
- {{ item.name }}: {{ item.description }}
{% endfor %}
```

## Rendering with TOML input.

When rendering a template, the variables will be parsed and an editor opened allowing you to enter in the values.  The opened file is rendered in TOML format.

In the case of the following template:

```j2
Hello {{ name }}

{% for item in items %}
- {{ item.name }}: {{ item.description }}
{% endfor %}
```

This will open an editor with the following toml content:

```toml
name=""

[[items]]
name=""
description=""
```

Just fill in the values and close the editor, and the template will be rendered with the values substituted.

```toml
name="John"

[[items]]
name="Apple"
description="A red fruit."

[[items]]
name="Orange"
description="A orange fruit."

[[items]]
name="Banana"
description="A yellow fruit."
```

This will result in the following output:

```
Hello John

- Apple: A red fruit.
- Orange: A orange fruit.
- Banana: A yellow fruit.
```

### No Variable Template

In the event that a template contains no variables requiring user input, `kt render` (as well as template actions in recipes) skips the TOML editor entirely and renders the template immediately using any preset context.

## Recipe Automation

Recipes let you describe a sequence of template renders, shell commands, and interactive prompts in a single TOML document.  The new `kt recipe` command group manages these definitions.

### Actions

- `template` – render a stored template, optionally writing the result to an `output` path.  The familiar TOML editor still opens, but providing a `context` table pre-fills its values (reusing prompt answers via `$(var)` where needed) so you only have to tweak what’s missing.  Add an optional `comment = "Fill in the project title"` to show extra guidance under the TOML header while editing.
- `command` – run shell commands.  Provide either a string (executed through the shell), a list of strings (executed without a shell), or a list containing multiple command definitions to run sequentially.  Values like `$(var_name)` are replaced by previously captured prompt variables before execution, and every variable is also exported to the child process environment.
- `prompt` – ask the user for input and stash it under `var`.  The stored value can be re-used by later actions with the `$(var)` syntax.
- `recipe` – run another stored recipe inline without spawning a new `kt` process (avoiding DuckDB locks).  The nested recipe shares the current variable context and accepts `$(var)` interpolation in its `name`.
- `gate` – optionally include a `gate = "Question?"` string on any action to ask whether it should run.  Answer `y` to proceed or `n` to skip that action.

### Example recipe

```toml
[[actions]]
type = "prompt"
prompt = "Postgres User Name:"
var = "pguser"

[[actions]]
type = "prompt"
prompt = "Postgres Password:"
var = "pgpass"

[[actions]]
type = "prompt"
prompt = "Postgres Database Name:"
var = "pgdb"

[[actions]]
type = "template"
name = "db-env"
output = ".env"
context = { postgres.name = "$(pguser)", postgres.password = "$(pgpass)", postgres.db_name = "$(pgdb)" }

[[actions]]
type = "command"
command = "echo 'Generated database env'"

[[actions]]
type = "command"
gate = "Create Git repository?"
command = [
  ["git", "init"],
  ["git", "add", "."],
  ["git", "commit", "-m", "Initial commit"]
]

[[actions]]
type = "recipe"
gate = "Run shared post-steps?"
name = "post-setup"
```

When the template action sees a `context` table it preloads those values in the editor so you can review or extend them before rendering.  Any string value that matches a previously prompted variable is resolved automatically, and you can also embed `$(var)` anywhere in the string to interpolate values inline.  Save this recipe with `kt recipe add db-env` and run it via `kt recipe render db-env` to generate the `.env` file end-to-end; create a separate `post-setup` recipe if you want the gated nested step to run.
