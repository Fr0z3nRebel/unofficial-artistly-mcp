import io
import json

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from test_workflow import NEW, OLD, Upstream, raw_design, signed_state

from artistly_mcp.client import ArtistlyClient
from artistly_mcp.models import ConsistentCharacterInput, CreateCharacterInput
from artistly_mcp.service import ArtistlyService
from artistly_mcp.state import ArtistlyError
from artistly_mcp.uploads import read_upload_image

UPLOAD_ID = '01a16924-bc5a-71c4-8361-864dd0c28c85'
UPLOAD_REF = f'temp/completed/{UPLOAD_ID}.png'


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / 'user-reference.png'
    Image.new('RGB', (16, 16), color='blue').save(path)
    return path


class StyledUpstream(Upstream):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.upload_posts = 0
        self.chunks = []
        self.upload_status = 200
        self.confirm_upload = True
        self.upload_id = UPLOAD_ID
        self.stored_payload = None
        self.requests = []
        self.fail_upload_network = False

    def styled_design(self):
        raw = raw_design(NEW, self.result, 'sitting on a bench')
        raw.update(category='3d & 2d Style Images', tool_used='Consistent Characters',
                   checkpoint='3d & 2d Style Images')
        return raw

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path in ('/ai/consistent-characters', '/ai/ai-design-assistants') and request.method == 'GET':
            return httpx.Response(200, text='<div data-page="{&quot;version&quot;:&quot;styled-version&quot;}"></div>',
                                  headers={'content-type': 'text/html',
                                           'set-cookie': 'XSRF-TOKEN=fresh%3D; Path=/'})
        if path == '/upload/chunk':
            assert request.headers['x-xsrf-token'] == 'fresh='
            assert request.headers['referer'].endswith('/ai/consistent-characters')
            if request.method == 'POST':
                self.upload_posts += 1
                assert b'name="filepond"' in request.content
                assert b'{"color":null}' in request.content
                return httpx.Response(200, text=self.upload_id)
            assert request.method == 'PATCH'
            assert request.url.params['patch'] == UPLOAD_ID
            assert request.headers['content-type'] == 'application/offset+octet-stream'
            assert request.headers['upload-name'] == 'reference.png'
            assert int(request.headers['upload-offset']) == sum(len(c) for c in self.chunks)
            self.chunks.append(request.content)
            if self.fail_upload_network:
                raise httpx.ReadTimeout('upload interrupted', request=request)
            return httpx.Response(self.upload_status, json={'uploaded': self.confirm_upload})
        if path == '/ai/consistent-characters/store':
            self.posts += 1
            self.submitted = True
            self.stored_payload = json.loads(request.content)
            assert self.stored_payload['image'] == UPLOAD_REF
            assert self.stored_payload['checkpoint'] == '3d & 2d Style Images'
            assert self.stored_payload['visibility'] == 'private'
            assert self.stored_payload['quantity'] == 1
            if self.submission_status == 0:
                raise httpx.ReadTimeout('generation outcome uncertain', request=request)
            return httpx.Response(self.submission_status,
                                  headers={'location': 'https://app.artistly.ai/personal-designs'})
        if path == '/api/internal/designs':
            data = [raw_design(OLD)]
            if self.submitted:
                data.insert(0, self.styled_design())
            return httpx.Response(200, json={'data': data})
        if path.startswith('/api/internal/designs/'):
            return httpx.Response(200, json={'data': self.styled_design()})
        return super().__call__(request)


async def test_styled_upload_generate_and_download(settings, image_path):
    state = signed_state(settings)
    upstream = StyledUpstream()
    service = ArtistlyService(settings)
    params = ConsistentCharacterInput(image_path=str(image_path), prompt='sitting on a bench', wait_seconds=0)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, params)
        assert result.status == 'completed'
        assert upstream.stored_payload == params.payload(UPLOAD_REF)
        paths, size = await client.download(result.designs[0], settings.download_dir)
        assert paths and size > 0
    assert b''.join(upstream.chunks) == image_path.read_bytes()
    assert upstream.upload_posts == 1 and upstream.posts == 1
    assert service.state.get_job(result.job_id)['uploaded_image'] == UPLOAD_REF
    assert upstream.download_cookies is None


async def test_chunk_offsets(settings, image_path, monkeypatch):
    monkeypatch.setattr('artistly_mcp.client.UPLOAD_CHUNK_BYTES', 7)
    upstream = StyledUpstream()
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        await client.prepare_generation('/ai/consistent-characters')
        reference = await client.upload_image(read_upload_image(str(image_path)))
    assert reference == UPLOAD_REF
    assert len(upstream.chunks) > 1
    assert b''.join(upstream.chunks) == image_path.read_bytes()
    assert upstream.posts == 0


@pytest.mark.parametrize('failure', ['http', 'confirmation', 'network', 'reference'])
async def test_upload_failure_never_submits_generation(settings, image_path, failure):
    upstream = StyledUpstream()
    if failure == 'http':
        upstream.upload_status = 500
    elif failure == 'confirmation':
        upstream.confirm_upload = False
    elif failure == 'network':
        upstream.fail_upload_network = True
    else:
        upstream.upload_id = '../unexpected-reference'
    service = ArtistlyService(settings)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, ConsistentCharacterInput(
            image_path=str(image_path), prompt='sitting on a bench', wait_seconds=0))
    assert result.status == 'failed'
    assert 'No generation request was sent' in result.message
    assert upstream.posts == 0
    assert upstream.upload_posts == 1
    assert service.state.get_job(result.job_id)['submission_attempted'] is False


async def test_uncertain_submission_does_not_upload_or_submit_again(settings, image_path):
    upstream = StyledUpstream(submission_status=0)
    service = ArtistlyService(settings)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, ConsistentCharacterInput(
            image_path=str(image_path), prompt='sitting on a bench', wait_seconds=0))
        assert result.status == 'awaiting_design'
        image_path.unlink()
        result = await ArtistlyService(settings).get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.upload_posts == 1 and upstream.posts == 1


async def test_restart_after_processing_needs_no_reference_file(settings, image_path):
    upstream = StyledUpstream(result='processing')
    state = signed_state(settings)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, ConsistentCharacterInput(
            image_path=str(image_path), prompt='sitting on a bench', wait_seconds=0))
    assert result.status == 'processing'
    image_path.unlink()
    upstream.result = 'private'
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.upload_posts == 1 and upstream.posts == 1


async def test_interrupted_upload_tracking_never_submits(settings, image_path):
    state = signed_state(settings)
    service = ArtistlyService(settings)
    job = state.create_job({'request': ConsistentCharacterInput(
        image_path=str(image_path), prompt='sitting on a bench').model_dump(),
        'workflow': 'consistent_styled', 'baseline': [], 'created_at': 0,
        'submission_attempted': False})
    upstream = StyledUpstream()
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await service.get_job(client, job['job_id'])
    assert result.status == 'uploading'
    assert not upstream.requests


async def test_active_styled_job_blocks_other_tools(settings, image_path):
    state = signed_state(settings)
    upstream = StyledUpstream(result='processing')
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        service = ArtistlyService(settings)
        result = await service.generate(client, ConsistentCharacterInput(
            image_path=str(image_path), prompt='sitting on a bench', wait_seconds=0))
        with pytest.raises(ArtistlyError, match=result.job_id):
            await service.generate(client, CreateCharacterInput(prompt='another character'))
    assert upstream.posts == 1


async def test_invalid_file_causes_no_network_requests(settings, tmp_path):
    path = tmp_path / 'secret.jpg'
    path.write_text('this is not an image')
    upstream = StyledUpstream()
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        with pytest.raises(ArtistlyError, match='invalid'):
            await ArtistlyService(settings).generate(client, ConsistentCharacterInput(
                image_path=str(path), prompt='a scene'))
    assert not upstream.requests


@pytest.mark.parametrize('format,extension', [('JPEG', 'jpg'), ('PNG', 'png'), ('WEBP', 'webp')])
def test_upload_format_is_detected_from_image_bytes(tmp_path, format, extension):
    path = tmp_path / 'misleading-extension.data'
    Image.new('RGB', (8, 8)).save(path, format=format)
    result = read_upload_image(str(path))
    assert result.extension == extension
    assert result.data == path.read_bytes()


@pytest.mark.parametrize('kind', ['missing', 'empty', 'oversized', 'symlink', 'relative', 'gif', 'truncated'])
def test_invalid_upload_files(tmp_path, kind, image_path):
    path = tmp_path / 'input.jpg'
    if kind == 'empty':
        path.touch()
    elif kind == 'oversized':
        with path.open('wb') as stream:
            stream.truncate(21 * 1024 * 1024)
    elif kind == 'symlink':
        path.symlink_to(image_path)
    elif kind == 'relative':
        path = 'relative-image.png'
    elif kind == 'gif':
        Image.new('RGB', (8, 8)).save(path, format='GIF')
    elif kind == 'truncated':
        buffer = io.BytesIO()
        Image.new('RGB', (100, 100)).save(buffer, format='JPEG')
        path.write_bytes(buffer.getvalue()[:50])
    with pytest.raises(ArtistlyError):
        read_upload_image(str(path))


@pytest.mark.parametrize('kwargs', [
    {'prompt': 'scene'}, {'image_path': '', 'prompt': 'scene'},
    {'image_path': '/image.png', 'prompt': 'scene', 'mode': 'realistic'},
    {'image_path': '/image.png', 'prompt': 'scene', 'mode': 'multi'},
    {'image_path': '/image.png', 'prompt': 'scene', 'library_id': 123},
    {'image_path': '/image.png', 'prompt': 'scene', 'aspect_ratio': 'unsupported'},
])
def test_required_image_and_unsupported_modes(kwargs):
    with pytest.raises(ValidationError):
        ConsistentCharacterInput(**kwargs)


def test_styled_payload_dimensions_and_defaults():
    params = ConsistentCharacterInput(image_path='/provided.png', prompt='a scene', aspect_ratio='16:9')
    payload = params.payload('temp/completed/reference.png')
    assert (payload['width'], payload['height']) == (1344, 768)
    assert payload['dimension'] == '16:9'
    assert payload['lora'] == 'subject'
    assert payload['face_swap'] is False
    assert payload['visibility'] == 'private'
    assert 'image_path' in params.model_json_schema()['required']
