import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    download_dir: Path

    @classmethod
    def from_env(cls) -> 'Settings':
        return cls(
            Path(os.getenv('ARTISTLY_STATE_DIR', '~/.local/share/artistly-mcp')).expanduser(),
            Path(os.getenv('ARTISTLY_DOWNLOAD_DIR', '~/Downloads/Artistly')).expanduser(),
        )

    def prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
