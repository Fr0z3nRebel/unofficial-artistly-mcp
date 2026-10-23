import json
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote
from uuid import uuid4

import httpx

from .models import Design, DesignPage
from .state import ArtistlyError, SessionExpired, State

BASE_URL = 'https://app.artistly.ai'
DESIGNER_PATH = '/ai/image-designer-v6'
MAX_IMAGE_BYTES = 50 * 1024 * 1024


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.page: dict[str, Any] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == 'data-page' and value:
                self.page = json.loads(value)


class ArtistlyClient:
    def __init__(self, state: State, transport: httpx.AsyncBaseTransport | None = None):
        self.state = state
        self.version: str | None = None
        self.transport = transport
        self.http = httpx.AsyncClient(
            base_url=BASE_URL, follow_redirects=False, timeout=30,
            transport=transport, headers={'Accept': 'application/json',
                                          'X-Requested-With': 'XMLHttpRequest'},
        )

    async def __aenter__(self) -> Self:
        try:
            self.load_session()
        except BaseException:
            await self.http.aclose()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.http.aclose()

    def load_session(self) -> None:
        data = self.state.session()
        self.http.cookies.clear()
        for cookie in data['cookies']:
            domain = cookie.get('domain', '')
            if domain not in ('app.artistly.ai', '.artistly.ai', 'artistly.ai'):
                continue
            self.http.cookies.set(cookie['name'], cookie['value'], domain=domain,
                                  path=cookie.get('path', '/'))
        self.version = data.get('inertia_version')

    def save_session(self) -> None:
        self.state.save_session({
            'cookies': [
                {'name': c.name, 'value': c.value, 'domain': c.domain,
                 'path': c.path, 'expires': c.expires, 'secure': c.secure}
                for c in self.http.cookies.jar
                if c.domain in ('app.artistly.ai', '.artistly.ai', 'artistly.ai')
            ], 'inertia_version': self.version,
        })

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ArtistlyError('Artistly network request failed. For a submitted generation, '
                                'resume the job; do not submit it again.') from None
        self.save_session()
        return response

    @staticmethod
    def check_response(response: httpx.Response) -> None:
        if response.status_code in (401, 419) or (
            response.is_redirect and '/login' in response.headers.get('location', '')
        ):
            raise SessionExpired()
        if response.status_code == 429:
            raise ArtistlyError('Artistly rate limited this request. Wait before checking again. '
                                'Do not automatically resubmit generations.')
        if response.status_code == 403:
            raise ArtistlyError('Artistly denied access. Check account permissions or sign in again.')
        if response.status_code >= 400:
            raise ArtistlyError(f'Artistly returned HTTP {response.status_code}. '
                                'No generation has been retried.')

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.request('GET', path, params=params)
        self.check_response(response)
        if response.status_code != 200:
            raise ArtistlyError('Unexpected redirect from Artistly. Sign in again if necessary.')
        if 'application/json' not in response.headers.get('content-type', ''):
            raise SessionExpired()
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            raise ArtistlyError('Artistly returned an unexpected response format.') from None

    async def prepare_generation(self) -> None:
        response = await self.request('GET', DESIGNER_PATH, headers={'Accept': 'text/html'})
        self.check_response(response)
        if response.status_code != 200:
            raise SessionExpired()
        parser = PageParser()
        try:
            parser.feed(response.text)
        except ValueError:
            raise ArtistlyError('Artistly page metadata changed; generation was not submitted.') from None
        if not parser.page or 'version' not in parser.page:
            raise ArtistlyError('Artistly page metadata is unavailable; generation was not submitted.')
        self.version = parser.page['version']
        self.save_session()

    async def list_designs(self, folder_id: int | None = None, cursor: str | None = None,
                           tool_used: str | None = None) -> DesignPage:
        params: dict[str, Any] = {'folder_id': folder_id or 'all'}
        if cursor:
            params['cursor'] = cursor
        if tool_used:
            params['tool_used'] = tool_used
        raw = await self.get_json('/api/internal/designs', params)
        return DesignPage(designs=[Design.from_api(d) for d in raw['data']],
                          next_cursor=(raw.get('meta') or {}).get('next_cursor'))

    async def get_design(self, uuid: str) -> Design:
        raw = await self.get_json('/api/internal/designs/' + uuid)
        return Design.from_api(raw['data'])

    async def submit(self, payload: dict[str, Any]) -> httpx.Response:
        token = next((c.value for c in self.http.cookies.jar if c.name == 'XSRF-TOKEN'), None)
        if not token:
            raise SessionExpired()
        return await self.request('POST', DESIGNER_PATH + '/store', json=payload, headers={
            'X-XSRF-TOKEN': unquote(token), 'X-Inertia': 'true',
            'X-Inertia-Version': self.version or '', 'Origin': BASE_URL,
            'Referer': BASE_URL + DESIGNER_PATH,
        })

    async def download(self, design: Design, directory: Path) -> tuple[list[str], int]:
        if not design.ready:
            raise ArtistlyError('Design is not ready. Use artistly_get_design to check its status.')
        directory.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        total = 0
        # This client deliberately has no Artistly cookies, referer, or auth headers.
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                     timeout=60) as cdn:
            for index, url in enumerate(design.image_urls, 1):
                target = None
                try:
                    async with cdn.stream('GET', url) as response:
                        if response.status_code != 200:
                            raise ArtistlyError(f'Image download returned HTTP {response.status_code}.')
                        content_type = response.headers.get('content-type', '').split(';')[0]
                        extension = {'image/jpeg': '.jpg', 'image/png': '.png',
                                     'image/webp': '.webp'}.get(content_type)
                        if not extension:
                            raise ArtistlyError('Download was not a supported image type.')
                        # Filenames never use upstream UUIDs or caller-supplied paths.
                        target = directory / f'artistly-{uuid4().hex}-{index}{extension}'
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        count = 0
                        prefix = b''
                        with os.fdopen(fd, 'wb') as output:
                            async for chunk in response.aiter_bytes():
                                count += len(chunk)
                                if count > MAX_IMAGE_BYTES:
                                    raise ArtistlyError('Image exceeds the 50 MiB download limit.')
                                prefix = (prefix + chunk)[:16]
                                output.write(chunk)
                        valid = (
                            content_type == 'image/jpeg' and prefix.startswith(b'\xff\xd8\xff')
                            or content_type == 'image/png' and prefix.startswith(b'\x89PNG\r\n\x1a\n')
                            or content_type == 'image/webp' and prefix.startswith(b'RIFF')
                            and prefix[8:12] == b'WEBP'
                        )
                        if not valid:
                            raise ArtistlyError('Downloaded file did not match its image type.')
                        paths.append(str(target.resolve()))
                        total += count
                except (httpx.HTTPError, OSError, ArtistlyError) as exc:
                    if target:
                        target.unlink(missing_ok=True)
                    if paths:
                        raise ArtistlyError(f'Download stopped. Earlier files were saved: {paths}. '
                                            'Check these before downloading again.') from None
                    if isinstance(exc, ArtistlyError):
                        raise
                    raise ArtistlyError('Image download failed; check network and download directory.') from None
        return paths, total
