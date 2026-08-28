"""Die Version steht in pyproject.toml; hier wird sie nur gelesen."""

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
__version__: str = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
