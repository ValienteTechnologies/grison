"""grison — a markdown hub between security scanners and Valiente's infra.

Everything is a *source*, *transform*, or *sink* of markdown. See the module
layout (ports & adapters): ``model`` (schema), ``scanners`` (source adapters),
``markdown`` (serialization + HTML⇄md converter), ``sinks`` (file sink),
``remote`` (Ghostwriter + BookStack), and ``cli``.
"""

__version__ = "0.1.0"
