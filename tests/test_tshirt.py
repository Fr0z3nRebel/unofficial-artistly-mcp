import json

import httpx
import pytest
from pydantic import ValidationError
from test_workflow import NEW, OLD, OTHER, Upstream, raw_design, signed_state

from artistly_mcp.client import ArtistlyClient
from artistly_mcp.models import GenerateInput, TShirtDesignInput
from artistly_mcp.service import ArtistlyService
from artistly_mcp.state import ArtistlyError

PROMPT = 'A centered vector illustration of an astronaut cat with lettering "SPACE COFFEE"'


class TShirtUpstream(Upstream):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.payload = None
        self.wrong_checkpoint = False
        self.changed_prompt = False

    def design(self, uuid=NEW):
        raw = raw_design(uuid, self.result, 'A stale template example' if self.changed_prompt else PROMPT)
        raw.update(category='T-Shirt Designs', tool_used='AI Image Designer v6',
                   checkpoint='other' if self.wrong_checkpoint else 'T-Shirt Designs',
                   width=self.payload['width'], height=self.payload['height'])
        return raw

    def __call__(self, request):
        if request.method == 'POST':
            self.payload = json.loads(request.content)
            assert request.url.path == '/ai/image-designer-v6/store'
            assert self.payload['quality'] == 'highQuality'
            assert self.payload['category'] == self.payload['checkpoint'] == 'T-Shirt Designs'
            assert self.payload['prompt'] == self.payload['positive_prompt'] == PROMPT
            assert self.payload['should_enhance_prompt'] is False
            assert self.payload['negative_prompt'] == ''
            assert self.payload['prefix'] == self.payload['suffix'] == ''
            assert self.payload['lora'] is None
            assert not self.payload['image_url']
            return super().__call__(request)
        if request.url.path == '/api/internal/designs':
            designs = [raw_design(OLD)]
            if self.submitted:
                designs.insert(0, self.design())
                if self.ambiguous or self.payload['quantity'] == 2:
                    designs.insert(0, self.design(OTHER))
            return httpx.Response(200, json={'data': designs})
        if request.url.path.startswith('/api/internal/designs/'):
            return httpx.Response(200, json={'data': self.design(request.url.path.rsplit('/', 1)[1])})
        return super().__call__(request)


@pytest.mark.parametrize('aspect_ratio,dimensions', [('1:1', (1024, 1024)), ('4:5', (896, 1088)),
                                                    ('16:9', (1344, 768))])
async def test_tshirt_high_quality_workflow_and_download(settings, aspect_ratio, dimensions):
    upstream = TShirtUpstream()
    service = ArtistlyService(settings)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, TShirtDesignInput(
            prompt=PROMPT, aspect_ratio=aspect_ratio, wait_seconds=0))
        assert result.status == 'completed'
        assert (result.designs[0].width, result.designs[0].height) == dimensions
        paths, size = await client.download(result.designs[0], settings.download_dir)
        assert paths and size > 0
    assert service.state.get_job(result.job_id)['workflow'] == 'tshirt'
    assert upstream.posts == 1
    assert upstream.download_cookies is None


async def test_tshirt_job_resumes_after_restart(settings):
    upstream = TShirtUpstream(result='processing')
    state = signed_state(settings)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TShirtDesignInput(prompt=PROMPT, wait_seconds=0))
    assert result.status == 'processing'
    upstream.result = 'private'
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.posts == 1


async def test_tshirt_uncertain_submission_never_retries(settings):
    upstream = TShirtUpstream(submission_status=0)
    service = ArtistlyService(settings)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, TShirtDesignInput(prompt=PROMPT, wait_seconds=0))
        assert result.status == 'awaiting_design'
        result = await service.get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.posts == 1


@pytest.mark.parametrize('attribute', ['wrong_checkpoint', 'changed_prompt'])
async def test_tshirt_wrong_result_never_matches(settings, attribute):
    upstream = TShirtUpstream()
    setattr(upstream, attribute, True)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TShirtDesignInput(prompt=PROMPT, wait_seconds=0))
    assert result.status == 'awaiting_design'
    assert not result.designs


async def test_tshirt_ambiguous_results_stop(settings):
    upstream = TShirtUpstream()
    upstream.ambiguous = True
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TShirtDesignInput(prompt=PROMPT, wait_seconds=0))
    assert result.status == 'ambiguous'
    assert not result.designs


async def test_tshirt_two_image_batch(settings):
    upstream = TShirtUpstream()
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TShirtDesignInput(
            prompt=PROMPT, quantity=2, wait_seconds=0))
    assert result.status == 'completed'
    assert len(result.designs) == 2
    assert upstream.posts == 1


async def test_tshirt_active_job_blocks_other_generation(settings):
    upstream = TShirtUpstream(result='processing')
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        service = ArtistlyService(settings)
        first = await service.generate(client, TShirtDesignInput(prompt=PROMPT, wait_seconds=0))
        with pytest.raises(ArtistlyError, match=first.job_id):
            await service.generate(client, GenerateInput(prompt='another image'))
    assert upstream.posts == 1


@pytest.mark.parametrize('kwargs', [
    {'prompt': ''}, {'prompt': ' '}, {'prompt': 'x', 'aspect_ratio': 'invalid'},
    {'prompt': 'x', 'quantity': 0}, {'prompt': 'x', 'quantity': 5},
    {'prompt': 'x', 'quality': 'fast'}, {'prompt': 'x', 'quality': 'high_quality'},
    {'prompt': 'x', 'template_prompt': 'stale example'},
    {'prompt': 'x', 'enhance_prompt': True},
])
def test_tshirt_validation_and_no_quality_override(kwargs):
    with pytest.raises(ValidationError):
        TShirtDesignInput(**kwargs)


def test_tshirt_defaults_and_folder():
    params = TShirtDesignInput(prompt=PROMPT, folder_id=12)
    payload = params.payload()
    assert payload['folder_id'] == 12
    assert payload['visibility'] == 'private'
    assert payload['quantity'] == 1
    assert payload['quality'] == 'highQuality'
    assert 'quality' not in params.model_json_schema()['properties']
