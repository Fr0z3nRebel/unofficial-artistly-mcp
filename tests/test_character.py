import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from test_workflow import NEW, OLD, OTHER, Upstream, raw_design, signed_state

from artistly_mcp.client import ArtistlyClient
from artistly_mcp.models import CreateCharacterInput, GenerateInput
from artistly_mcp.service import ArtistlyService
from artistly_mcp.state import ArtistlyError


class CharacterUpstream(Upstream):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests = []
        self.wrong_checkpoint = False
        self.old_timestamp = False

    def character(self, uuid=NEW):
        design = raw_design(uuid, self.result, prompt='Artistly expanded this character description.')
        design.update(category='Character Creator', tool_used='AI Design Assistants',
                      checkpoint='other' if self.wrong_checkpoint else 'character_creator',
                      created_at=(datetime.now(UTC) - timedelta(days=1) if self.old_timestamp
                                  else datetime.now(UTC)).isoformat())
        return design

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == '/ai/ai-design-assistants' and request.method == 'GET':
            return httpx.Response(200, text='<div data-page="{&quot;version&quot;:&quot;character-version&quot;}"></div>',
                                  headers={'content-type': 'text/html',
                                           'set-cookie': 'XSRF-TOKEN=fresh%3D; Path=/',
                                           'date': datetime.now(UTC).strftime('%a, %d %b %Y %H:%M:%S GMT')})
        if path == '/ai/ai-design-assistants/store' and request.method == 'POST':
            self.posts += 1
            self.submitted = True
            payload = json.loads(request.content)
            assert payload == CreateCharacterInput(prompt='a polar bear').payload()
            assert request.headers['referer'].endswith('/ai/ai-design-assistants')
            assert request.headers['x-xsrf-token'] == 'fresh='
            assert request.headers['x-inertia-version'] == 'character-version'
            if self.submission_status == 0:
                raise httpx.ReadTimeout('uncertain', request=request)
            return httpx.Response(self.submission_status,
                                  headers={'location': 'https://app.artistly.ai/personal-designs'})
        if path == '/api/internal/designs':
            designs = [raw_design(OLD)]
            if self.submitted:
                designs.insert(0, self.character())
                if self.ambiguous:
                    designs.insert(0, self.character(OTHER))
            return httpx.Response(200, json={'data': designs, 'meta': {'next_cursor': None}})
        if path.startswith('/api/internal/designs/'):
            return httpx.Response(200, json={'data': self.character(path.rsplit('/', 1)[1])})
        return super().__call__(request)


def test_character_payload_matches_observed_request():
    params = CreateCharacterInput(prompt='a polar bear')
    assert params.payload() == {
        'checkpointId': 1, 'checkpoint': 'character_creator', 'should_extend_prompt': True,
        'width': 1024, 'height': 1024, 'aspect_ratio': '1:1', 'quantity': 1,
        'source_image': None, 'seed': 0, 'tool_used': 'AI Design Assistants',
        'folder_id': None, 'category': 'Character Creator', 'image': '',
        'positive_prompt': 'a polar bear', 'dimension': '1:1', 'checkpoint_id': 1,
        'visibility': 'private',
    }
    assert 'quality' not in params.model_json_schema()['properties']
    assert 'workflow' not in params.model_json_schema()['properties']


async def test_character_expanded_prompt_correlation_and_download(settings):
    state = signed_state(settings)
    service = ArtistlyService(settings)
    upstream = CharacterUpstream()
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await service.generate(client, CreateCharacterInput(prompt='a polar bear', wait_seconds=0))
        assert result.status == 'completed'
        assert result.designs[0].prompt != 'a polar bear'
        assert result.designs[0].checkpoint == 'character_creator'
        paths, size = await client.download(result.designs[0], settings.download_dir)
        assert paths and size > 0
    assert upstream.posts == 1
    assert upstream.download_cookies is None
    assert service.state.get_job(result.job_id)['workflow'] == 'character'


async def test_character_resume_after_restart(settings):
    state = signed_state(settings)
    upstream = CharacterUpstream(result='processing')
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, CreateCharacterInput(prompt='a polar bear', wait_seconds=0))
    assert result.status == 'processing'
    upstream.result = 'private'
    restarted = ArtistlyService(settings)
    async with ArtistlyClient(restarted.state, httpx.MockTransport(upstream)) as client:
        result = await restarted.get_job(client, result.job_id)
    assert result.status == 'completed'
    assert upstream.posts == 1


async def test_character_ambiguous_results_never_guess(settings):
    state = signed_state(settings)
    upstream = CharacterUpstream()
    upstream.ambiguous = True
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, CreateCharacterInput(prompt='a polar bear', wait_seconds=0))
    assert result.status == 'ambiguous'
    assert not result.designs


@pytest.mark.parametrize('attribute', ['wrong_checkpoint', 'old_timestamp'])
async def test_character_rejects_wrong_metadata(settings, attribute):
    state = signed_state(settings)
    upstream = CharacterUpstream()
    setattr(upstream, attribute, True)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        result = await ArtistlyService(settings).generate(client, CreateCharacterInput(prompt='a polar bear', wait_seconds=0))
    assert result.status == 'awaiting_design'
    assert not result.designs


async def test_image_and_character_share_active_job_lock(settings):
    state = signed_state(settings)
    upstream = CharacterUpstream(result='processing')
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        service = ArtistlyService(settings)
        first = await service.generate(client, CreateCharacterInput(prompt='a polar bear', wait_seconds=0))
        with pytest.raises(ArtistlyError, match=first.job_id):
            await service.generate(client, GenerateInput(prompt='another image', wait_seconds=0))
    assert upstream.posts == 1


async def test_uncertain_character_submission_resumes_without_retry(settings):
    state = signed_state(settings)
    upstream = CharacterUpstream(submission_status=0)
    service = ArtistlyService(settings)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        first = await service.generate(client, CreateCharacterInput(prompt='a polar bear', wait_seconds=0))
        assert first.status == 'awaiting_design'
        resumed = await service.get_job(client, first.job_id)
    assert resumed.status == 'completed'
    assert upstream.posts == 1


async def test_legacy_image_job_without_workflow_key_still_resumes(settings):
    state = signed_state(settings)
    upstream = Upstream(result='processing')
    service = ArtistlyService(settings)
    async with ArtistlyClient(state, httpx.MockTransport(upstream)) as client:
        first = await service.generate(client, GenerateInput(prompt='a dog', wait_seconds=0))
        old_job = state.get_job(first.job_id)
        old_job.pop('workflow')
        state.save_job(old_job)
        upstream.result = 'private'
        result = await ArtistlyService(settings).get_job(client, first.job_id)
    assert result.status == 'completed'
    assert upstream.posts == 1


@pytest.mark.parametrize('kwargs', [{'prompt': ''}, {'prompt': 'bear', 'quantity': 0},
                                   {'prompt': 'bear', 'quantity': 5},
                                   {'prompt': 'bear', 'visibility': 'public'},
                                   {'prompt': 'bear', 'aspect_ratio': '16:9'}])
def test_character_validation(kwargs):
    with pytest.raises(ValidationError):
        CreateCharacterInput(**kwargs)
