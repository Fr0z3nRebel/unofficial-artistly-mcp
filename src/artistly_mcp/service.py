import asyncio
import time
from typing import Any

from .client import ArtistlyClient
from .config import Settings
from .models import Design, GenerateInput, JobResult
from .state import ArtistlyError, SessionExpired, State


class ArtistlyService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = State(settings)

    async def generate(self, client: ArtistlyClient, params: GenerateInput) -> JobResult:
        await client.prepare_generation()
        page = await client.list_designs()
        job = self.state.create_job({
            'request': params.model_dump(), 'baseline': [d.uuid for d in page.designs],
            'created_at': time.time(), 'message': None,
        })
        # Persist the intent before POST. Cancellation, timeout, and 5xx leave a resumable job.
        try:
            response = await client.submit(params.payload())
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
        params = GenerateInput(**job['request'])
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
                    if candidate.category != 'AI Image Designer v6':
                        continue
                    detail = await client.get_design(candidate.uuid)
                    width, height = params.payload()['width'], params.payload()['height']
                    if (detail.prompt == params.prompt and detail.width == width
                            and detail.height == height):
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
