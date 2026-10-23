from pathlib import Path

import pytest

from artistly_mcp.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / 'state', tmp_path / 'downloads')
