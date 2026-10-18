from .cli import app
from . import cli_prospects, cli_send, cli_sources  # noqa: F401  (add the prospect, review / send / cycle and source commands)

app(prog_name="jobbot")
