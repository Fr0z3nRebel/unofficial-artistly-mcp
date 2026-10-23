import json
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

ASPECT_RATIOS = {
    '1:1': (1024, 1024), '16:9': (1344, 768), '21:9': (1536, 640),
    '3:2': (1216, 832), '2:3': (832, 1216), '4:5': (896, 1088),
    '5:4': (1088, 896), '3:4': (896, 1152), '4:3': (1152, 896),
    '9:16': (768, 1344), '9:21': (640, 1536),
}
Reference = Annotated[str, Field(
    pattern=r'^(job_[a-f0-9]{32}|[A-Za-z0-9-]{16,64})$',
    description='Local job_id returned by generation, or an Artistly design UUID.',
)]


def is_image_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.hostname == 'cdn.artistly.ai'
                and url.port in (None, 443) and url.username is None and url.password is None)
    except ValueError:
        return False


class GenerateInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=10000, description='Describe the desired image.')
    quality: Literal['fast', 'high_quality'] = Field(default='fast', description='Generation quality.')
    aspect_ratio: str = Field(default='1:1', description='Supported width:height ratio, e.g. 16:9.')
    quantity: int = Field(default=1, ge=1, le=4, description='Number of images to generate.')
    folder_id: int | None = Field(default=None, gt=0, description='Existing Artistly folder ID.')
    visibility: Literal['private', 'public'] = Field(default='private', description='Design visibility.')
    wait_seconds: float = Field(default=15, ge=0, le=45, description='Wait before returning a resumable job.')

    @field_validator('aspect_ratio')
    @classmethod
    def valid_ratio(cls, value: str) -> str:
        if value not in ASPECT_RATIOS:
            raise ValueError('Choose one of: ' + ', '.join(ASPECT_RATIOS))
        return value

    def payload(self) -> dict[str, Any]:
        width, height = ASPECT_RATIOS[self.aspect_ratio]
        return {
            'checkpointId': 1, 'checkpoint': None, 'prompt': self.prompt,
            'positive_prompt': self.prompt, 'width': width, 'height': height,
            'aspect_ratio': self.aspect_ratio, 'quantity': self.quantity,
            'source_image': None, 'seed': 0, 'tool_used': 'AI Image Designer v6',
            'folder_id': self.folder_id, 'category': 'AI Image Designer v6',
            'image_option': 'upload', 'image_selected': 'no', 'negative_prompt': '',
            'quality': 'highQuality' if self.quality == 'high_quality' else 'fast',
            'theme': '', 'image_url': '', 'image_base64': '', 'personal_design': '',
            'denoise_strength': '0.75', 'prefix': '', 'suffix': '', 'lora': None,
            'should_enhance_prompt': False, 'visibility': self.visibility,
        }


class Design(BaseModel):
    uuid: str
    id: int | None = None
    status: str
    ready: bool
    image_urls: list[str]
    prompt: str | None = None
    width: int | None = None
    height: int | None = None
    category: str | None = None
    tool_used: str | None = None
    folder_id: int | None = None
    aspect_ratio: str | None = None
    created_at: str | None = None
    error: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> 'Design':
        images = raw.get('images') or []
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except (ValueError, TypeError):
                images = []
        urls = [u for u in images if is_image_url(u)] if isinstance(images, list) else []
        state = str(raw.get('status', 'unknown'))
        return cls(
            uuid=raw['uuid'], id=raw.get('id'), status=state,
            ready=state in ('private', 'public', 'completed', 'complete', 'success')
            and bool(urls) and not raw.get('user_error'),
            image_urls=urls, prompt=raw.get('positive_prompt'), width=raw.get('width'),
            height=raw.get('height'), category=raw.get('category'),
            tool_used=raw.get('tool_used'), folder_id=raw.get('folder_id'),
            aspect_ratio=raw.get('aspect_ratio'),
            created_at=raw.get('created_at'), error=raw.get('user_error'),
        )


class DesignPage(BaseModel):
    designs: list[Design]
    next_cursor: str | None = None


class JobResult(BaseModel):
    job_id: str
    status: str
    designs: list[Design] = Field(default_factory=list)
    message: str | None = None


class SessionStatus(BaseModel):
    authenticated: bool
    message: str


class DownloadResult(BaseModel):
    paths: list[str]
    total_bytes: int
