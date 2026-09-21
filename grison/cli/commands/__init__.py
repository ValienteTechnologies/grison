"""The CLI's commands, one module per verb — see :mod:`grison.cli` for the CLI
itself. Importing this package has no side effects; :mod:`grison.cli` imports each
command module by name so its ``@app.command()``/``@hook_app.command()``
decorators run and register it."""
