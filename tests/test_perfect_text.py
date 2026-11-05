import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError
from test_workflow import NEW, OLD, OTHER, Upstream, raw_design, signed_state

from artistly_mcp.client import ArtistlyClient
from artistly_mcp.models import TextImageInput
from artistly_mcp.service import ArtistlyService

PROMPT = 'A cafe sign reading "GOOD MORNING"'


class PerfectTextUpstream(Upstream):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.payload = None
        self.wrong_checkpoint = False

    def design(self, uuid=NEW):
        enhanced = self.payload and self.payload['should_enhance_prompt']
        raw = raw_design(uuid, self.result, 'Enhanced cafe scene with "GOOD MORNING"' if enhanced else PROMPT)
        raw.update(category='Perfect Text in Image Designer', tool_used='AI Image Designer v6',
                   checkpoint='other' if self.wrong_checkpoint else 'Perfect Text in Image Designer',
                   created_at=datetime.now(UTC).isoformat())
        return raw

    def __call__(self, request):
        if request.method == 'POST':
            self.payload = json.loads(request.content)
            assert request.url.path == '/ai/image-designer-v6/store'
            assert self.payload['category'] == 'Perfect Text in Image Designer'
            assert self.payload['checkpoint'] == 'Perfect Text in Image Designer'
            assert self.payload['positive_prompt'] == PROMPT
            assert self.payload['quantity'] == 1
            assert self.payload['quality'] == 'fast'
            return super().__call__(request)
        if request.url.path == '/api/internal/designs':
            data = [raw_design(OLD)]
            if self.submitted:
                data.insert(0, self.design())
                if self.ambiguous:
                    data.insert(0, self.design(OTHER))
            return httpx.Response(200, json={'data': data})
        if request.url.path.startswith('/api/internal/designs/'):
            return httpx.Response(200, json={'data': self.design(request.url.path.rsplit('/', 1)[1])})
        return super().__call__(request)


@pytest.mark.parametrize('enhance', [True, False])
async def test_perfect_text_workflow_and_download(settings, enhance):
    upstream = PerfectTextUpstream()
    service = ArtistlyService(settings)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, TextImageInput(
            prompt=PROMPT, enhance_prompt=enhance, wait_seconds=0))
        assert result.status == 'completed'
        assert upstream.payload['should_enhance_prompt'] is enhance
        assert (result.designs[0].prompt != PROMPT) is enhance
        saved, size = await client.download(result.designs[0], settings.download_dir)
        assert saved and size > 0
    assert upstream.posts == 1
    assert service.state.get_job(result.job_id)['workflow'] == 'perfect_text'
    assert upstream.download_cookies is None


async def test_perfect_text_restart(settings):
    state = signed_state(settings)
    upstream = PerfectTextUpstream(result='processing')
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TextImageInput(prompt=PROMPT, wait_seconds=0))
    assert result.status == 'processing'
    upstream.result = 'private'
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.posts == 1


async def test_uncertain_text_submission_is_not_retried(settings):
    upstream = PerfectTextUpstream(submission_status=0)
    service = ArtistlyService(settings)
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, TextImageInput(prompt=PROMPT, wait_seconds=0))
        assert result.status == 'awaiting_design'
        result = await service.get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.posts == 1


async def test_ambiguous_enhanced_text_designs_stop(settings):
    upstream = PerfectTextUpstream()
    upstream.ambiguous = True
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TextImageInput(prompt=PROMPT, wait_seconds=0))
    assert result.status == 'ambiguous'
    assert not result.designs


@pytest.mark.parametrize('enhance', [True, False])
async def test_wrong_text_checkpoint_is_not_matched(settings, enhance):
    upstream = PerfectTextUpstream()
    upstream.wrong_checkpoint = True
    async with ArtistlyClient(signed_state(settings), httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, TextImageInput(
            prompt=PROMPT, enhance_prompt=enhance, wait_seconds=0))
    assert result.status == 'awaiting_design'
    assert not result.designs


def test_text_payload_defaults_and_supported_settings():
    params = TextImageInput(prompt=PROMPT, aspect_ratio='4:5', folder_id=12, enhance_prompt=False)
    payload = params.payload()
    assert payload['prompt'] == payload['positive_prompt'] == PROMPT
    assert (payload['width'], payload['height']) == (896, 1088)
    assert payload['folder_id'] == 12
    assert payload['visibility'] == 'private'
    schema = params.model_json_schema()['properties']
    assert 'quality' not in schema and 'quantity' not in schema


@pytest.mark.parametrize('kwargs', [{'prompt': ''}, {'prompt': 'x', 'quantity': 4},
                                   {'prompt': 'x', 'quality': 'fast'},
                                   {'prompt': 'x', 'aspect_ratio': 'invalid'}])
def test_text_input_validation(kwargs):
    with pytest.raises(ValidationError):
        TextImageInput(**kwargs)
