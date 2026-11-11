"""Refresh the local WSL endpoints while retaining server identity and accounts."""
import argparse
import json
from pathlib import Path

from solbreaker.client_lock import client_configuration_access
from solbreaker.connection import atomic_write, configure, connection_profile, connect_client
from solbreaker.network import ipv4, load_network, parse_config
from solbreaker.storage import account_access


def refresh(state, host):
    state = Path(state).resolve(strict=True)
    host = ipv4(host)
    metadata = json.loads((state/'installation.json').read_text())
    with account_access(state/'private'):
        network = load_network(state)
        document = network.document()
        if (not network.advertise_http.startswith('http://')
                or network.listen_http.host != network.advertise_http.split('/')[2].split(':')[0]):
            raise ValueError('Automatic WSL refresh requires an installer-managed local HTTP profile')
        for name in ('http', 'login', 'base'):
            port = getattr(network, 'listen_'+name).port
            document['listen'][name] = f'{host}:{port}'
            document['advertise'][name] = f'http://{host}:{port}' if name == 'http' else f'{host}:{port}'
        document['services']['http'] = document['advertise']['http']
        from solbreaker.network import PRIVATE
        document['access']['client_networks'] = [str(value) for value in PRIVATE]
        configure(state, parse_config(document))
        profile = state/'connection.json'
        atomic_write(profile, (json.dumps(connection_profile(state), indent=2)+'\n').encode())
        if metadata.get('client'):
            client = Path(metadata['client'])
            with client_configuration_access(client):
                connect_client(client, profile)
    print('WSL server and client endpoints updated to '+host)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--host', required=True)
    args = parser.parse_args()
    try:
        refresh(args.state, args.host)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Network refresh failed: {error}\n')
