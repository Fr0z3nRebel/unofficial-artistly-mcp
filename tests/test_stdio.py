import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_handshake_discovery_and_tool_calls(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=['-m', 'artistly_mcp', 'serve'],
        env={**os.environ, 'ARTISTLY_STATE_DIR': str(tmp_path / 'state'),
             'ARTISTLY_DOWNLOAD_DIR': str(tmp_path / 'downloads')},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == 'artistly_mcp'
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                'artistly_session_status', 'artistly_generate_image', 'artistly_list_designs',
                'artistly_get_design', 'artistly_download_design',
                'artistly_create_character',
                'artistly_consistent_character',
            }
            status = await session.call_tool('artistly_session_status', {})
            assert not status.isError
            assert status.structuredContent['authenticated'] is False
            invalid = await session.call_tool('artistly_get_design', {'reference': '../secret'})
            assert invalid.isError
            missing = await session.call_tool('artistly_list_designs', {})
            assert missing.isError
            assert 'login' in missing.content[0].text
            missing_image = await session.call_tool('artistly_consistent_character', {
                'params': {'prompt': 'a scene'},
            })
            assert missing_image.isError
            assert 'image_path' in missing_image.content[0].text
