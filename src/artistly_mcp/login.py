import asyncio
import time

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .client import BASE_URL, DESIGNER_PATH, PageParser
from .config import Settings
from .state import ArtistlyError, State


async def login(settings: Settings, channel: str | None = None, timeout: int = 300) -> None:
    """User completes sign-in in a dedicated browser. Passwords are never read or saved here."""
    state = State(settings)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=False, channel=channel)
        except PlaywrightError:
            raise ArtistlyError('Could not open the login browser. Run `python -m playwright install '
                                'chromium`, or use `artistly-mcp login --channel chrome`.') from None
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(BASE_URL + '/login')
            print('Sign in to Artistly in the new browser window. Waiting for authentication...')
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise ArtistlyError('Login window closed. Run artistly-mcp login again.')
                response = await context.request.get(
                    BASE_URL + '/api/internal/designs?folder_id=all',
                    headers={'Accept': 'application/json'}, max_redirects=0,
                )
                if response.status == 200 and 'application/json' in response.headers.get('content-type', ''):
                    body = await response.json()
                    if isinstance(body, dict) and isinstance(body.get('data'), list):
                        designer = await context.request.get(BASE_URL + DESIGNER_PATH)
                        parser = PageParser()
                        parser.feed(await designer.text())
                        cookies = await context.cookies(BASE_URL)
                        state.save_session({'cookies': cookies,
                                            'inertia_version': parser.page.get('version')})
                        print('Artistly session saved. You can now start the MCP server.')
                        return
                await asyncio.sleep(2)
            raise ArtistlyError('Login timed out. Run artistly-mcp login again when ready.')
        except PlaywrightError:
            raise ArtistlyError('Login browser or network connection closed. Run artistly-mcp login again.') from None
        finally:
            await browser.close()
