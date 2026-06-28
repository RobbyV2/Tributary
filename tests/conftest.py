import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def pipewire_dir() -> Path:
    return FIXTURES / "pipewire"


@pytest.fixture
def bluez_dir() -> Path:
    return FIXTURES / "bluez"


@pytest.fixture
def remote_dir() -> Path:
    return FIXTURES / "remote"


@pytest.fixture
def pw_dump_idle() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "pipewire" / "pw-dump-idle.json").read_text())


@pytest.fixture
def pw_dump_playing() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "pipewire" / "pw-dump-playing.json").read_text())


@pytest.fixture
def pw_dump_a2dp() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "pipewire" / "pw-dump-a2dp-streaming.json").read_text())
