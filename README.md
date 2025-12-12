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

In the event that a template contains no variables requiring user input, the editor will be empty.  Just save and quit anyway, and the template will render.

## Recipe Automation

Recipes let you describe a sequence of template renders, shell commands, and interactive prompts in a single TOML document.  The new `kt recipe` command group manages these definitions.

### Actions

- `template` – render a stored template, optionally writing the result to an `output` path.  When executed the familiar TOML editor opens so you can supply the template variables.
- `command` – run shell commands.  Provide either a string (executed through the shell) or a list of strings (executed without a shell).  Values like `$(var_name)` are replaced by previously captured prompt variables before execution, and every variable is also exported to the child process environment.
- `prompt` – ask the user for input and stash it under `var`.  The stored value can be re-used by later actions with the `$(var)` syntax.

### Example recipe

```toml
[[actions]]
type = "template"
name = "svelte-env"
output = ".env"

[[actions]]
type = "command"
command = ["touch", "README.md"]

[[actions]]
type = "prompt"
prompt = "What is your name?"
var = "name"

[[actions]]
type = "command"
command = "echo \"NAME=$(name)\" >> .env"
```

Save this to `kt recipe add my-recipe`, then run it with `kt recipe render my-recipe`.  Each action is executed sequentially, allowing you to automate common bootstrap tasks without dropping back to the shell between templates.
