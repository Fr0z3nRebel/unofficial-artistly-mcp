import io
import os
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .state import ArtistlyError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_PIXELS = 40_000_000
UPLOAD_CHUNK_BYTES = 5_000_000


@dataclass(frozen=True)
class UploadImage:
    data: bytes
    extension: str


def read_upload_image(image_path: str) -> UploadImage:
    """Read a bounded regular image file, verify its format, and retain no extra copies."""
    path = Path(image_path).expanduser()
    if not path.is_absolute():
        raise ArtistlyError('image_path must be an absolute local path to the user-provided image.')
    try:
        # Avoid special files, blocking FIFOs, and last-component symlinks.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ArtistlyError('Reference image must be a regular file, not a link or device.')
            if info.st_size == 0 or info.st_size > MAX_UPLOAD_BYTES:
                raise ArtistlyError('Reference image must be nonempty and at most 20 MiB.')
            data = stream.read(MAX_UPLOAD_BYTES + 1)
    except OSError:
        raise ArtistlyError('Cannot read the reference image. Provide an accessible regular image file.') from None
    if len(data) > MAX_UPLOAD_BYTES:
        raise ArtistlyError('Reference image exceeds 20 MiB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                extension = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}.get(image.format)
                if not extension:
                    raise ArtistlyError('Reference image must be JPEG, PNG, or WebP.')
                if image.width * image.height > MAX_UPLOAD_PIXELS:
                    raise ArtistlyError('Reference image exceeds the 40 megapixel limit.')
                image.verify()
            # Decode once to reject truncated image data before uploading anything.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ArtistlyError('Reference image is invalid, truncated, or too large to decode safely.') from None
    return UploadImage(data=data, extension=extension)
