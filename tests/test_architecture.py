"""Layering rules, checked on the source (DESIGN section 11).

``app/domain`` must work with zero AI in the system: it may not import the agent, the pipeline,
the channels, the API, or any LLM/HTTP client. ``app/pipeline`` owns the agent *port*
(a Protocol) and may not import the agent implementation either.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"

LLM_AND_HTTP = ("openai", "anthropic", "httpx", "requests", "aiohttp", "litellm", "langchain")
RULES = {
    "domain": ("app.agent", "app.pipeline", "app.channels", "app.api", *LLM_AND_HTTP),
    "pipeline": ("app.agent", "app.channels", "app.api", *LLM_AND_HTTP),
    "db": ("app.agent", "app.pipeline", "app.channels", "app.api", *LLM_AND_HTTP),
}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize(("package", "forbidden"), sorted(RULES.items()))
def test_layer_does_not_import_forbidden_modules(package: str, forbidden: tuple[str, ...]) -> None:
    files = sorted((APP / package).rglob("*.py"))
    assert files, f"no sources found for app/{package}"
    violations = [
        f"{path.relative_to(APP.parent)} imports {name}"
        for path in files
        for name in imports_of(path)
        if any(name == f or name.startswith(f + ".") for f in forbidden)
    ]
    assert violations == []
