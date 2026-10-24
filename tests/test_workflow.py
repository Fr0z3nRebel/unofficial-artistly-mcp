import json
import stat
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from artistly_mcp.client import ArtistlyClient
from artistly_mcp.config import Settings
from artistly_mcp.models import Design, GenerateInput
from artistly_mcp.server import create_server
from artistly_mcp.service import ArtistlyService
from artistly_mcp.state import ArtistlyError, SessionExpired, State

OLD = '01a16672-3b2b-7123-bade-6ff1ff3a1fc5'
NEW = '01a16678-2731-723d-a7d2-ed26a6362f0f'
OTHER = '01a16678-2731-723d-a7d2-ed26a6362f10'
IMAGE = 'https://cdn.artistly.ai/test/image.jpg'


def signed_state(settings: Settings) -> State:
    state = State(settings)
    state.save_session({'cookies': [
        {'name': 'artistly_session', 'value': 'test-session', 'domain': 'app.artistly.ai'},
        {'name': 'XSRF-TOKEN', 'value': 'token%3D', 'domain': 'app.artistly.ai'},
    ], 'inertia_version': 'old'})
    return state


def raw_design(uuid: str = NEW, status: str = 'private', prompt: str = 'a dog') -> dict:
    return {'uuid': uuid, 'id': 42, 'status': status, 'positive_prompt': prompt,
            'width': 1024, 'height': 1024, 'category': 'AI Image Designer v6',
            'images': json.dumps([IMAGE] if status == 'private' else ['https://placehold.co/processing'])}


class Upstream:
    def __init__(self, result: str = 'private', submission_status: int = 302):
        self.submitted = False
        self.posts = 0
        self.result = result
        self.submission_status = submission_status
        self.download_cookies = None
        self.ambiguous = False
        self.bad_image = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == 'cdn.artistly.ai':
            self.download_cookies = request.headers.get('cookie')
            return httpx.Response(200, content=b'<html>wrong</html>' if self.bad_image else b'\xff\xd8\xfftest-jpeg',
                                  headers={'content-type': 'image/jpeg'})
        if path == '/ai/image-designer-v6':
            return httpx.Response(200, text='<div data-page="{&quot;version&quot;:&quot;new-version&quot;}"></div>',
                                  headers={'content-type': 'text/html',
                                           'set-cookie': 'XSRF-TOKEN=fresh%3D; Path=/'})
        if request.method == 'POST':
            self.posts += 1
            self.submitted = True
            assert request.headers['x-xsrf-token'] == 'fresh='
            assert request.headers['x-inertia-version'] == 'new-version'
            assert json.loads(request.content)['visibility'] == 'private'
            if self.submission_status == 0:
                raise httpx.ReadTimeout('uncertain', request=request)
            return httpx.Response(self.submission_status,
                                  headers={'location': 'https://app.artistly.ai/personal-designs'})
        if path == '/api/internal/designs':
            designs = [raw_design(OLD)]
            if self.submitted:
                designs.insert(0, raw_design(NEW, self.result))
                if self.ambiguous:
                    designs.insert(0, raw_design(OTHER, self.result))
            return httpx.Response(200, json={'data': designs, 'meta': {'next_cursor': 'next-page'}})
        if path.startswith('/api/internal/designs/'):
            return httpx.Response(200, json={'data': raw_design(path.rsplit('/', 1)[1], self.result)})
        raise AssertionError(str(request.url))


async def test_complete_workflow_and_no_cookie_download(settings):
    state = signed_state(settings)
    service = ArtistlyService(settings)
    upstream = Upstream()
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
        assert result.status == 'completed'
        assert result.designs[0].uuid == NEW
        paths, size = await client.download(result.designs[0], settings.download_dir)
        assert Path(paths[0]).read_bytes().startswith(b'\xff\xd8\xff')
        assert size > 0
    assert upstream.posts == 1
    assert upstream.download_cookies is None
    assert service.state.get_job(result.job_id)['status'] == 'completed'


async def test_restart_resumes_without_resubmission(settings):
    state = signed_state(settings)
    upstream = Upstream(result='processing')
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
    assert result.status == 'processing'
    upstream.result = 'private'
    restarted = ArtistlyService(settings)
    async with ArtistlyClient(restarted.state, httpx.MockTransport(upstream)) as client:
        resumed = await restarted.get_job(client, result.job_id)
    assert resumed.status == 'completed'
    assert upstream.posts == 1


@pytest.mark.parametrize('status', [0, 500])
async def test_uncertain_post_is_never_retried(settings, status):
    state = signed_state(settings)
    upstream = Upstream(submission_status=status)
    service = ArtistlyService(settings)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
        if status == 0:
            assert result.status == 'awaiting_design'
        resumed = await service.get_job(client, result.job_id)
    assert resumed.status == 'completed'
    assert upstream.posts == 1


async def test_active_job_blocks_other_process(settings):
    state = signed_state(settings)
    upstream = Upstream(result='processing')
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        first = await ArtistlyService(settings).generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
        with pytest.raises(ArtistlyError, match=first.job_id):
            await ArtistlyService(settings).generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
    assert upstream.posts == 1


async def test_ambiguous_correlation_does_not_guess(settings):
    state = signed_state(settings)
    upstream = Upstream()
    upstream.ambiguous = True
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
    assert result.status == 'ambiguous'
    assert not result.designs
    assert upstream.posts == 1


async def test_quantity_two_matches_two_designs(settings):
    state = signed_state(settings)
    upstream = Upstream()
    upstream.ambiguous = True
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, GenerateInput(prompt='a dog', quantity=2, wait_seconds=0))
    assert result.status == 'completed'
    assert len(result.designs) == 2


@pytest.mark.parametrize('status', [401, 419, 429])
async def test_auth_expiry_and_rate_limit_no_post(settings, status):
    state = signed_state(settings)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status)

    async with ArtistlyClient(state, httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtistlyError):
            await ArtistlyService(settings).generate(client, GenerateInput(prompt='a dog'))
    assert all(r.method == 'GET' for r in requests)


async def test_missing_login_and_reconnect_same_state(settings):
    state = State(settings)
    with pytest.raises(SessionExpired):
        async with ArtistlyClient(state):
            pass
    signed_state(settings)
    async with ArtistlyClient(state, httpx.MockTransport(Upstream())) as client:
        assert (await client.list_designs()).designs


async def test_cursor_forwarding(settings):
    state = signed_state(settings)

    def handler(request):
        assert request.url.params['cursor'] == 'opaque-cursor'
        assert request.url.params['folder_id'] == '123'
        assert request.url.params['tool_used'] == 'AI Image Designer v6'
        return httpx.Response(200, json={'data': [], 'meta': {'next_cursor': None}})

    async with ArtistlyClient(state, httpx.MockTransport(handler)) as client:
        page = await client.list_designs(123, 'opaque-cursor', 'AI Image Designer v6')
    assert page.next_cursor is None


async def test_bad_image_is_removed(settings):
    state = signed_state(settings)
    upstream = Upstream()
    upstream.bad_image = True
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        with pytest.raises(ArtistlyError, match='did not match'):
            await client.download(Design.from_api(raw_design()), settings.download_dir)
    assert list(settings.download_dir.iterdir()) == []


def test_untrusted_image_urls_are_not_downloadable():
    raw = raw_design()
    raw['images'] = json.dumps(['http://127.0.0.1/secret', 'https://cdn.artistly.ai.evil.test/image',
                                'https://elsewhere.test/image', 'https://user:pass@cdn.artistly.ai/x',
                                'https://cdn.artistly.ai:8443/x', 'https://[malformed/x'])
    design = Design.from_api(raw)
    assert not design.ready
    assert design.image_urls == []


def test_private_session_permissions_and_terminal_job_state(settings):
    state = signed_state(settings)
    assert stat.S_IMODE(state.session_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.state_dir.stat().st_mode) == 0o700
    job = state.create_job({'request': {}, 'baseline': []})
    stale = dict(job)
    job['status'] = 'completed'
    state.save_job(job)
    stale['status'] = 'processing'
    state.save_job(stale)
    assert state.get_job(job['job_id'])['status'] == 'completed'


@pytest.mark.parametrize('kwargs', [{'prompt': ' '}, {'prompt': 'dog', 'quantity': 5},
                                   {'prompt': 'dog', 'aspect_ratio': 'invalid'},
                                   {'prompt': 'dog', 'wait_seconds': -1}])
def test_generation_validation(kwargs):
    with pytest.raises(ValidationError):
        GenerateInput(**kwargs)


def test_high_quality_wire_value():
    assert GenerateInput(prompt='dog', quality='high_quality').payload()['quality'] == 'highQuality'


async def test_mcp_tool_catalog_and_missing_session(settings):
    server = create_server(settings)
    tools = await server.list_tools()
    assert len(tools) == 7
    for tool in tools:
        assert tool.outputSchema
        assert tool.annotations.destructiveHint is False
    # FastMCP invokes the actual registered implementation and validates its output schema.
    result = await server.call_tool('artistly_session_status', {})
    text = str(result)
    assert 'authenticated' in text and 'login' in text


@pytest.mark.parametrize('status', [409, 422, 429])
async def test_rejected_submission_releases_job_and_never_retries(settings, status):
    state = signed_state(settings)
    upstream = Upstream(submission_status=status)
    service = ArtistlyService(settings)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
    assert result.status == 'failed'
    assert upstream.posts == 1
    assert service.state.get_job(result.job_id)['status'] == 'failed'


async def test_reconnected_session_loaded_by_same_server(settings, monkeypatch):
    monkeypatch.setattr('artistly_mcp.server.ArtistlyClient',
                        lambda state: ArtistlyClient(state, httpx.MockTransport(Upstream())))
    server = create_server(settings)
    first = await server.call_tool('artistly_session_status', {})
    assert 'False' in str(first) or 'false' in str(first)
    signed_state(settings)
    # The existing registered tool sees credentials written by the independent login process.
    second = await server.call_tool('artistly_session_status', {})
    assert 'True' in str(second) or 'true' in str(second)
