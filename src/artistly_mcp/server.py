from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .client import ArtistlyClient
from .config import Settings
from .models import (
    ConsistentCharacterInput,
    CreateCharacterInput,
    DesignPage,
    DownloadResult,
    GenerateInput,
    JobResult,
    Reference,
    SessionStatus,
)
from .service import ArtistlyService
from .state import ArtistlyError

READ = {'readOnlyHint': True, 'destructiveHint': False,
        'idempotentHint': True, 'openWorldHint': True}
WRITE = {'readOnlyHint': False, 'destructiveHint': False,
         'idempotentHint': False, 'openWorldHint': True}


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    service = ArtistlyService(settings)
    mcp = FastMCP('artistly_mcp', instructions=(
        'Artistly Create From Prompt, Character Creator, and styled Consistent Character tools. '
        'Generate once, then resume '
        'using the returned job_id. '
        'Never automatically repeat a generation after timeout or uncertain outcome. '
        'Use artistly_session_status first. Session credentials are configured locally with '
        '`artistly-mcp login`; never request passwords or cookies in chat. '
        'Other Artistly generation functions are not supported yet.'
    ))

    @mcp.tool(annotations=READ)
    async def artistly_session_status() -> SessionStatus:
        """Check the saved Artistly login with a read request. Gives reconnect instructions."""
        try:
            async with ArtistlyClient(service.state) as client:
                await client.list_designs()
            return SessionStatus(authenticated=True, message='Artistly login is working.')
        except ArtistlyError as exc:
            return SessionStatus(authenticated=False, message=str(exc))

    @mcp.tool(annotations=WRITE)
    async def artistly_generate_image(params: GenerateInput) -> JobResult:
        """Generate 1–4 images with Artistly Create From Prompt. Defaults: private, Fast, square.

        Returns a durable job_id plus status and available designs. If incomplete, use
        artistly_get_design with that job_id. Do not repeat generation to check progress.
        Only one local job may be active. Public visibility must be explicitly requested.
        """
        try:
            async with ArtistlyClient(service.state) as client:
                return await service.generate(client, params)
        except ArtistlyError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=WRITE)
    async def artistly_create_character(params: CreateCharacterInput) -> JobResult:
        """Create 1–4 character images using AI Design Assistants → Character Creator.

        Artistly automatically expands the description. Images are private and 1024×1024.
        Returns a durable job_id; use artistly_get_design and artistly_download_design to
        resume and save results. Does not train a reusable Consistent Characters model.
        Avoid manual Character Creator generations while this job is active: prompts are
        rewritten, so correlation uses new IDs, category, checkpoint, and creation time.
        If multiple results could match, the job stops as ambiguous rather than guessing.
        Never repeat creation just to check progress.
        """
        try:
            async with ArtistlyClient(service.state) as client:
                return await service.generate(client, params)
        except ArtistlyError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=WRITE)
    async def artistly_consistent_character(params: ConsistentCharacterInput) -> JobResult:
        """Generate a new scene with the same user-provided character using 3D & 2D Style Images.

        Requires a user-provided local image_path (JPEG, PNG, or WebP; maximum 20 MiB and
        40 megapixels), prompt, and optional aspect_ratio. Uploads that image to Artistly.
        Always creates one private image. Only mode='styled' is supported; Realistic Images
        and Multi-consistent Characters are unavailable. Library selection is not supported.
        Returns a durable job_id; resume with artistly_get_design and save with
        artistly_download_design. Never repeat this tool call just to check progress.
        """
        try:
            async with ArtistlyClient(service.state) as client:
                return await service.generate(client, params)
        except ArtistlyError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ)
    async def artistly_list_designs(
        folder_id: Annotated[int | None, Field(gt=0, description='Existing folder ID; omit for all.')] = None,
        cursor: Annotated[str | None, Field(max_length=4000, description='Opaque next_cursor from prior call.')] = None,
        tool_used: Annotated[str | None, Field(max_length=200, description='Optional exact Artistly tool label.')] = None,
    ) -> DesignPage:
        """List one upstream page of personal designs and next_cursor. No image creation.

        Includes original CDN URLs when ready. Use get_design for full prompt and status.
        """
        try:
            async with ArtistlyClient(service.state) as client:
                return await client.list_designs(folder_id, cursor, tool_used)
        except ArtistlyError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ)
    async def artistly_get_design(
        reference: Reference,
        wait_seconds: Annotated[float, Field(ge=0, le=45, description='Wait for a local job; zero checks once.')] = 0,
    ) -> JobResult:
        """Resume a local job_id or fetch an Artistly design UUID. Never submits a generation.

        Job results include status, design UUIDs, original image URLs, dimensions, and errors.
        Waiting applies to local jobs; a design UUID is fetched once.
        """
        try:
            async with ArtistlyClient(service.state) as client:
                return await service.resolve(client, reference, wait_seconds)
        except ArtistlyError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=WRITE)
    async def artistly_download_design(reference: Reference) -> DownloadResult:
        """Download all original images for a ready design UUID or completed local job_id.

        Saves new files in ARTISTLY_DOWNLOAD_DIR (default ~/Downloads/Artistly), returning
        absolute paths and byte count. No arbitrary URLs or destination paths accepted.
        Calling again creates new copies. Does not modify Artistly designs.
        """
        try:
            async with ArtistlyClient(service.state) as client:
                result = await service.resolve(client, reference)
                if result.status != 'completed':
                    raise ArtistlyError('Generation is not completed; check artistly_get_design first.')
                paths: list[str] = []
                total = 0
                for design in result.designs:
                    saved, size = await client.download(design, settings.download_dir)
                    paths.extend(saved)
                    total += size
                return DownloadResult(paths=paths, total_bytes=total)
        except ArtistlyError as exc:
            raise ToolError(str(exc)) from None

    return mcp
