"""Finish setup inside the project virtual environment."""
import argparse
import ctypes
import json
from pathlib import Path
import shlex
import subprocess
import sys

from solbreaker.bootstrap import prepare
from solbreaker.connection import configure, connection_profile, atomic_write
from solbreaker.network import defaults, ipv4, parse_config, PRIVATE
from solbreaker.resource_source import inspect_game
from solbreaker.storage import account_access
from solbreaker.setup_progress import report

ROOT = Path(__file__).resolve().parents[1]


def verify_native():
    from solbreaker import sim_native, impact_native, armor_native
    for module in (sim_native, impact_native, armor_native):
        if module.load_library() is None:
            raise ValueError(f'Native library missing or incompatible: {module.__name__}')
    from solbreaker.rapier_dynamic import library_path
    path = library_path()
    library = ctypes.CDLL(str(path))
    for name in ('create', 'close', 'step', 'activate', 'sync', 'set_mass', 'deactivate', 'destroy'):
        getattr(library, 'tfs_rapier_'+name)
    from solbreaker.native_physics import load_library
    load_library()
    print('Five native libraries loaded successfully', flush=True)


def finish(options):
    report('Проверяем версию ресурсов сервера…')
    game, state = Path(options['game']), Path(options['state'])
    facts = inspect_game(game)
    host = ipv4(options['host'])
    if not options['skip_build']:
        report('Собираем нативные библиотеки физики…')
        subprocess.run([sys.executable, ROOT/'tools/build_native.py', '--jobs', str(options['jobs'])], check=True)
    report('Проверяем нативные библиотеки…')
    verify_native()
    if state.exists():
        manifest = json.loads((state/'manifest.json').read_text())
        if (not options['resume'] or manifest.get('game') != facts['game']
                or manifest.get('exe_sha256') != facts['exe_sha256']):
            raise ValueError('Existing state belongs to another game installation')
        from solbreaker.network import load_network
        network = load_network(state)
        if (network.listen_http.host != host or network.listen_http.port != options['http_port']
                or network.listen_login.port != options['login_port']):
            raise ValueError('Resume requires the existing host and ports. Use configure-network to change them')
    else:
        report('Создаём сервер, ключи и начальный аккаунт…')
        prepare(game, state, http_port=options['http_port'], login_port=options['login_port'])
        with account_access(state/'private'):
            manifest = json.loads((state/'manifest.json').read_text())
            document = defaults(manifest)
            document['listen'] = dict(http=f"{host}:{options['http_port']}",
                                     login=f"{host}:{options['login_port']}",
                                     base=f"{host}:{options['login_port']+1}")
            document['advertise'] = dict(http=f"http://{host}:{options['http_port']}",
                                        login=f"{host}:{options['login_port']}",
                                        base=f"{host}:{options['login_port']+1}")
            document['services']['http'] = document['advertise']['http']
            if host != '127.0.0.1':
                document['access']['client_networks'] = [str(network) for network in PRIVATE]
            configure(state, parse_config(document))
    profile = state/'connection.json'
    atomic_write(profile, (json.dumps(connection_profile(state), indent=2)+'\n').encode())
    if options['client']:
        from solbreaker.client_setup import prepare as prepare_client
        result = prepare_client(game, Path(options['client']), profile,
                                windows=options['windows_client'], proton=options['proton'], steam=options['steam'])
        print(json.dumps(result), flush=True)
    installation = state/'installation.json'
    metadata = json.loads(installation.read_text()) if installation.exists() else {}
    if options['client']:
        metadata['client'] = str(Path(options['client']).absolute())
    atomic_write(installation, (json.dumps(metadata, indent=2)+'\n').encode())
    print('\nSetup complete', flush=True)
    command = (['powershell.exe', '-NoProfile', '-File', str(ROOT/'start-server.ps1'), '-State', str(state)]
               if sys.platform == 'win32' else [str(ROOT/'start-server.sh'), '--state', str(state),
                                              '--log-file', str(state/'server.log')])
    print('Server command: '+(subprocess.list2cmdline(command) if sys.platform == 'win32' else shlex.join(command)))
    print('Connection profile: '+str(profile))
    print('Initial account credentials: '+str(state/'private/initial-login.txt'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--options', required=True)
    try:
        finish(json.loads(parser.parse_args().options))
    except (OSError, ValueError, AttributeError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Setup failed: {error}\n')
