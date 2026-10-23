import argparse
import asyncio
import json
import sys

from .client import ArtistlyClient
from .config import Settings
from .login import login
from .state import ArtistlyError, State


async def status(settings: Settings) -> None:
    state = State(settings)
    async with ArtistlyClient(state) as client:
        await client.list_designs()
    print('Artistly login is working.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Local Artistly MCP server and login setup.')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('serve', help='Run the MCP server over stdio (default).')
    auth = sub.add_parser('login', help='Open a dedicated browser for user sign-in or reconnection.')
    auth.add_argument('--channel', choices=['chrome', 'msedge'], default=None)
    auth.add_argument('--timeout', type=int, default=300)
    sub.add_parser('status', help='Check the saved session.')
    sub.add_parser('jobs', help='Show the 20 most recent local job IDs and states.')
    abandon = sub.add_parser('abandon-job', help='Release a stuck local job; does not cancel Artistly generation.')
    abandon.add_argument('job_id')
    abandon.add_argument('--confirm', action='store_true', required=True,
                         help='Confirm you checked Personal Designs; this does not cancel remote work.')
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        if args.command == 'login':
            asyncio.run(login(settings, args.channel, args.timeout))
        elif args.command == 'status':
            asyncio.run(status(settings))
        elif args.command == 'jobs':
            print(json.dumps(State(settings).list_jobs(), indent=2))
        elif args.command == 'abandon-job':
            state = State(settings)
            job = state.get_job(args.job_id)
            if job['status'] == 'completed':
                raise ArtistlyError('This job is already completed and does not need release.')
            job.update(status='abandoned', message='Manually released locally; remote work was not cancelled.')
            state.save_job(job)
            print(job['message'])
        else:
            from .server import create_server
            create_server(settings).run(transport='stdio')
    except ArtistlyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
