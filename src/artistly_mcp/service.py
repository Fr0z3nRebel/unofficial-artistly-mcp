import asyncio
import time
from datetime import datetime
from typing import Any

from .client import ArtistlyClient
from .config import Settings
from .models import ConsistentCharacterInput, CreateCharacterInput, Design, GenerateInput, JobResult
from .state import ArtistlyError, SessionExpired, State
from .uploads import read_upload_image


class ArtistlyService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = State(settings)

    async def generate(self, client: ArtistlyClient,
                       params: GenerateInput | CreateCharacterInput | ConsistentCharacterInput) -> JobResult:
        image = None
        if isinstance(params, ConsistentCharacterInput):
            image = await asyncio.to_thread(read_upload_image, params.image_path)
        await client.prepare_generation(params.workflow.page_path)
        page = await client.list_designs()
        job = self.state.create_job({
            'request': params.model_dump(), 'baseline': [d.uuid for d in page.designs],
            'workflow': params.workflow.key,
            'created_at': client.server_time or time.time(), 'message': None,
        })
        payload = params.payload()
        if image is not None:
            job.update(status='uploading', submission_attempted=False)
            self.state.save_job(job)
            try:
                reference = await client.upload_image(image)
            except ArtistlyError as exc:
                job.update(status='failed', message='Image upload failed. No generation request '
                           'was sent. ' + str(exc))
                self.state.save_job(job)
                return JobResult(job_id=job['job_id'], status='failed', message=job['message'])
            job['uploaded_image'] = reference
            payload['image'] = reference
        job.update(status='pending', submission_attempted=True)
        self.state.save_job(job)
        # Persist the intent before POST. Cancellation, timeout, and 5xx leave a resumable job.
        try:
            response = await client.submit(payload, params.workflow.page_path)
        except ArtistlyError:
            job.update(status='awaiting_design', message='Submission outcome is uncertain. '
                       'Resume this job; do not generate again.')
            self.state.save_job(job)
            return JobResult(job_id=job['job_id'], status=job['status'], message=job['message'])
        if response.status_code in (401, 403, 409, 419, 422, 429) or (
            response.is_redirect and '/login' in response.headers.get('location', '')
        ):
            job.update(status='failed', message=f'Submission rejected (HTTP {response.status_code}). '
                       'No automatic retry was made.')
            self.state.save_job(job)
            if response.status_code in (401, 419) or '/login' in response.headers.get('location', ''):
                raise SessionExpired()
            return JobResult(job_id=job['job_id'], status='failed', message=job['message'])
        job.update(status='awaiting_design', message=None)
        if response.status_code not in (200, 201, 202, 302, 303):
            job['message'] = f'Submission returned HTTP {response.status_code}; outcome uncertain. '
            job['message'] += 'Resume this job; no automatic retry was made.'
        self.state.save_job(job)
        try:
            return await self.get_job(client, job['job_id'], params.wait_seconds)
        except ArtistlyError as exc:
            return JobResult(job_id=job['job_id'], status=job['status'],
                             message='Submission was attempted. Resume this job. ' + str(exc))

    async def refresh_job(self, client: ArtistlyClient, job: dict[str, Any]) -> JobResult:
        if job['status'] in ('failed', 'abandoned'):
            return JobResult(job_id=job['job_id'], status=job['status'], message=job.get('message'))
        # Old MVP jobs have no workflow key and remain image jobs.
        if job.get('submission_attempted') is False:
            return JobResult(job_id=job['job_id'], status='uploading', message=(
                'No generation submission was recorded. The image upload may still be running. '
                'If its process was interrupted, inspect Personal Designs before releasing the '
                'local job with artistly-mcp abandon-job. Tracking never restarts uploads.'
            ))
        input_type = {'character': CreateCharacterInput, 'consistent_styled': ConsistentCharacterInput}.get(
            job.get('workflow'), GenerateInput)
        params = input_type(**job['request'])
        designs: list[Design] = []
        if job['design_uuids']:
            designs = [await client.get_design(uuid) for uuid in job['design_uuids']]
        else:
            baseline = set(job['baseline'])
            cursor = None
            candidates: list[Design] = []
            # Bound discovery; pagination is newest-first in the observed Artistly response.
            for _ in range(5):
                page = await client.list_designs(cursor=cursor)
                for candidate in page.designs:
                    if candidate.uuid in baseline:
                        continue
                    if candidate.category != params.workflow.category:
                        continue
                    detail = await client.get_design(candidate.uuid)
                    width, height = params.payload()['width'], params.payload()['height']
                    matches = detail.width == width and detail.height == height
                    if params.workflow.rewrites_prompt:
                        # Character Creator replaces the prompt. Require exact workflow metadata
                        # and a creation time at/after preflight rather than fuzzy prompt matching.
                        try:
                            created_at = datetime.fromisoformat(detail.created_at or '').timestamp()
                        except ValueError:
                            created_at = 0
                        matches = (matches and detail.tool_used == params.workflow.tool_used
                                   and detail.checkpoint == params.workflow.checkpoint
                                   and created_at >= job['created_at'] - 2)
                    else:
                        matches = matches and detail.prompt == params.prompt
                        if isinstance(params, ConsistentCharacterInput):
                            matches = (matches and detail.checkpoint == params.workflow.checkpoint
                                       and detail.tool_used == params.workflow.tool_used)
                    if matches:
                        candidates.append(detail)
                if any(d.uuid in baseline for d in page.designs) or not page.next_cursor:
                    break
                cursor = page.next_cursor
            # Artistly may represent quantity as multiple designs or multiple images in one design.
            count = sum(max(1, len(d.image_urls)) for d in candidates)
            if count > params.quantity:
                job.update(status='ambiguous', message='Multiple new designs match this request. '
                           'Inspect Personal Designs; do not resubmit. Local job remains active.')
            elif count == params.quantity:
                designs = candidates
                job['design_uuids'] = [d.uuid for d in designs]
                job['status'] = 'processing'
        if designs:
            if any(d.error or d.status in ('failed', 'error') for d in designs):
                job.update(status='failed', message='Artistly reported a generation failure. '
                           'No automatic retry was made.')
            elif all(d.ready for d in designs):
                job.update(status='completed', message=None)
            else:
                job['status'] = 'processing'
        self.state.save_job(job)
        return JobResult(job_id=job['job_id'], status=job['status'], designs=designs,
                         message=job.get('message'))

    async def get_job(self, client: ArtistlyClient, job_id: str, wait_seconds: float = 0) -> JobResult:
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await self.refresh_job(client, self.state.get_job(job_id))
            if result.status in ('completed', 'failed', 'abandoned', 'ambiguous'):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            await asyncio.sleep(min(3, remaining))

    async def resolve(self, client: ArtistlyClient, reference: str, wait_seconds: float = 0) -> JobResult:
        if reference.startswith('job_'):
            return await self.get_job(client, reference, wait_seconds)
        design = await client.get_design(reference)
        return JobResult(job_id=reference, status='completed' if design.ready else design.status,
                         designs=[design], message=design.error)
