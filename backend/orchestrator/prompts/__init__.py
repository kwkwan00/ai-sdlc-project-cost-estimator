"""Static LLM prompts — one ``<name>.md`` per twin/agent in this directory.

`load_prompt` lives here (not in `orchestrator.nodes._twin_base`) so any agent can load a
prompt without importing twin internals. Prompts are static at runtime, so each file is read
once per process and cached by name.
"""

from functools import cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    """Load the prompt text from ``prompts/<name>.md`` (cached per process)."""
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
