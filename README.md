# Kraken Template Tool

A very simple templating tool using Jinja2 Syntax.

## Usage

- Add template - `kt add [name]`  
- Edit template - `kt edit [name]`  
- Delete template - `kt delete [name]`  
- Render Template - `kt render [name] [--output path]`  
- List Templates - `kt list`

## Installation

Install with the UV package manager:

```bash
uv tool install https://github.com/FluxKraken/Kraken-Templates.git
```

## Command Syntax

The Jinja2 syntax has been extended to allow a shell command to be executed.  In this case the output of the command is substituted for the placeholder.

### Example:

```j2
BETTER_AUTH_SECRET='{>openssl rand -base64 32<}`
```

This will run the command `openssl rand -base64 32` and replace the placeholder `{>openssl rand -base64 32<}` with the output of the command.

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
