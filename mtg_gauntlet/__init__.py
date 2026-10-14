"""MTG Gauntlet - AI agents playtest Magic: The Gathering Commander decks."""

import tomllib
from pathlib import Path

from dotenv import load_dotenv


def _read_version() -> str:
    """Reads the project version from pyproject.toml (single source of truth).

    pyproject.toml ships next to the package in the Docker image, so it is
    available at runtime. Falls back to "0.0.0" if it can't be read.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0.0.0"


__version__ = _read_version()

load_dotenv()
