"""Prepare, configure and run the Solbreaker server."""
import argparse
import asyncio
import json
import logging
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog='solbreaker', description='Solbreaker tank server')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Prepare fresh server state from owned game resources')
    init.add_argument('--game', type=Path, required=True)
    init.add_argument('--state', type=Path, default=Path('data'))
    init.add_argument('--http-port', type=int, default=18080)
    init.add_argument('--login-port', type=int, default=20016)
    init.add_argument('--vehicle', default='usa/M2_lt')
    init.add_argument('--username', default='admin@solbreaker.local')
    serve = commands.add_parser('serve', help='Run the headless game server')
    serve.add_argument('--stop-on-stdin', action='store_true', help=argparse.SUPPRESS)
    serve.add_argument('--state', type=Path, default=Path('data'))
    serve.add_argument('--log-file', type=Path)
    serve.add_argument('--diagnostic-log', nargs='?', const='auto', type=Path)
    configure = commands.add_parser('configure-network', help='Apply a network TOML to idle state')
    configure.add_argument('--state', type=Path, default=Path('data'))
    configure.add_argument('--config', type=Path, required=True)
    lan = commands.add_parser('configure-lan', help='Share an idle server through a selected IPv4 network')
    lan.add_argument('--state', type=Path, default=Path('data'))
    lan.add_argument('--address', required=True)
    lan.add_argument('--subnet', required=True)
    identity = commands.add_parser('configure-server', help='Set the display name of an idle server')
    identity.add_argument('--state', type=Path, default=Path('data'))
    identity.add_argument('--name', required=True)
    export = commands.add_parser('export-connection', help='Export the public client connection profile')
    export.add_argument('--state', type=Path, default=Path('data'))
    export.add_argument('--output', type=Path, required=True)
    client = commands.add_parser('prepare-client-registration', help='Prepare native email, nickname and password registration')
    client.add_argument('--lab', type=Path, required=True)
    prepare_client = commands.add_parser('prepare-client', help='Prepare a separate client from owned game resources')
    prepare_client.add_argument('--game', type=Path, required=True)
    prepare_client.add_argument('--output', type=Path, required=True)
    preparation_profile = prepare_client.add_mutually_exclusive_group(required=True)
    preparation_profile.add_argument('--connection', type=Path)
    preparation_profile.add_argument('--url')
    prepare_client.add_argument('--windows', action='store_true')
    prepare_client.add_argument('--proton', type=Path)
    prepare_client.add_argument('--steam', type=Path)
    connect = commands.add_parser('connect-client', help='Import a public server profile into an idle prepared client')
    connect.add_argument('--lab', type=Path, required=True)
    connect.add_argument('--connection', type=Path, required=True)
    for mode in ('gui', 'tui'):
        interface = commands.add_parser(mode, help='Open the Solbreaker management interface')
        interface.add_argument('--config', type=Path, default=Path('runtime/manager.json'))
    launch = commands.add_parser('launch-client', help='Launch an idle prepared client with its private profile')
    launch.add_argument('--lab', type=Path, required=True)
    launch.add_argument('--connection', type=Path)
    launch.add_argument('--auto-login', action=argparse.BooleanOptionalAction, default=None)
    backup = commands.add_parser('backup-state', help='Back up offline accounts, progress and server keys')
    backup.add_argument('--state', type=Path, default=Path('data'))
    backup.add_argument('--output', type=Path, required=True)
    importing = commands.add_parser('import-connection', help='Import a server connection file or fetch it by URL')
    importing.add_argument('--lab', type=Path, required=True)
    origin = importing.add_mutually_exclusive_group(required=True)
    origin.add_argument('--connection', type=Path)
    origin.add_argument('--url')
    args = parser.parse_args(argv)
    try:
        if args.command in ('gui', 'tui'):
            try:
                if args.command == 'gui':
                    from .desktop import run
                else:
                    from .terminal import run
            except ImportError as error:
                parser.exit(1, f'Install interface dependencies with python tools/install_dependencies.py --profile {args.command}. {error}\n')
            return run(args.config)
        elif args.command == 'launch-client':
            from .management_tasks import launch_client
            if args.connection:
                from .management_tasks import import_connection
                import_connection(args.lab, profile=args.connection)
            result = launch_client(args.lab, auto_login=args.auto_login)
            if result not in (0, 1):
                parser.exit(1, f'Client exited with code {result}\n')
            return
        elif args.command == 'backup-state':
            from .management_tasks import backup_state
            result = backup_state(args.state, args.output)
        elif args.command == 'import-connection':
            from .management_tasks import import_connection
            result = import_connection(args.lab, profile=args.connection, url=args.url)
        elif args.command == 'init':
            from .bootstrap import prepare
            result = prepare(args.game, args.state, http_port=args.http_port, login_port=args.login_port,
                             vehicle=args.vehicle, username=args.username)
        elif args.command == 'configure-lan':
            from .connection import configure_lan
            result = configure_lan(args.state, args.address, args.subnet)
        elif args.command == 'configure-server':
            from .server_identity import configure_name
            result = configure_name(args.state, args.name)
        elif args.command == 'configure-network':
            from .storage import account_access
            from .connection import configure_file
            with account_access(args.state/'private'):
                result = configure_file(args.state, args.config)
        elif args.command == 'export-connection':
            from .connection import export_connection
            result = export_connection(args.state, args.output)
        elif args.command == 'connect-client':
            from .client_lock import client_configuration_access
            from .platform_runtime import require_executable_idle
            from .connection import connect_client
            from .client_tls import configure_environment
            with client_configuration_access(args.lab):
                require_executable_idle(args.lab.resolve()/'client/wotblitz.exe')
                result = connect_client(args.lab, args.connection)
                manifest = json.loads((args.lab/'manifest.json').read_text())
                configure_environment({}, args.lab, manifest)
        elif args.command == 'prepare-client':
            from .client_setup import prepare
            import tempfile
            from .management_tasks import fetch_profile
            with tempfile.TemporaryDirectory(prefix='solbreaker-connection-') as temporary:
                profile = args.connection
                if args.url:
                    profile = Path(temporary)/'connection.json'
                    profile.write_bytes(fetch_profile(args.url))
                result = prepare(args.game, args.output, profile, windows=args.windows,
                                 proton=args.proton, steam=args.steam)
        elif args.command == 'prepare-client-registration':
            from .client_registration import install
            result = install(args.lab)
        else:
            handlers = [logging.StreamHandler()]
            if args.log_file:
                args.log_file.parent.mkdir(parents=True, exist_ok=True)
                handlers.append(logging.FileHandler(args.log_file, encoding='utf-8'))
            logging.basicConfig(level=logging.INFO, format='%(message)s', handlers=handlers, force=True)
            from . import diagnostics
            from .server import serve_cli
            try:
                path = diagnostics.directory(args.state.resolve(), args.diagnostic_log)
                if path is not None:
                    diagnostics.install(path)
                asyncio.run(serve_cli(args.state.resolve(), stop_on_stdin=args.stop_on_stdin))
            finally:
                diagnostics.close()
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f'Error: {error}\n')
    except KeyboardInterrupt:
        return
