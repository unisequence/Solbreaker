"""Prepare a headless server state without copying or patching a game client."""
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import yaml

from . import dvpl
from .connection import REGIONS, regions_config
from .network import defaults, parse_config
from .resource_source import inspect_game
from .storage import AccountStore
from .training_config import TrainingConfig, save_training_config, training_config_path
from .vehicle_data import load_vehicle
from .build_info import LEGACY_SERVER_NAME


def _local_urls(value, base):
    if isinstance(value, dict):
        return {key: _local_urls(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [_local_urls(item, base) for item in value]
    if isinstance(value, str) and value.startswith(('http://', 'https://')):
        return base + '/unimplemented'
    return value


def initial_regions(game, origin):
    original = yaml.safe_load(dvpl.read(game/'Data/regions_publish.yaml.dvpl'))
    regions = dict(Common=dict(original['Common']), Regions={}, ClanEmblems=original.get('ClanEmblems', {}))
    regions['Common'].update(public='false', useDefaultAccount=False, isWgniV3=False)
    local = _local_urls(original['Regions']['Europe'], origin)
    local.update(realm='us', flag='us', public='false', isWgniV3=False, isGGMAllowed=False,
                 wgnwn=origin, preferredHost='LOCAL', isVisibleOnMap=True,
                 layoutPosition=dict(x=0.52, y=0.39))
    # Native saved logins and account caches use this stable region identifier.
    # The private localization overlays provide the Solbreaker display name.
    regions['Regions'][LEGACY_SERVER_NAME] = local
    return regions


def prepare(game, root, *, http_port=18080, login_port=20016, vehicle='usa/M2_lt',
            username='admin@solbreaker.local'):
    facts = inspect_game(game)
    game = Path(facts['game'])
    root = Path(root).expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise ValueError('Choose a new, absent server state directory')
    if root == game or game in root.parents or root in game.parents:
        raise ValueError('Server state must be separate from game resources')
    parts = vehicle.split('/')
    if len(parts) != 2:
        raise ValueError('Vehicle must use nation/name')
    load_vehicle(game, *parts)
    if re.fullmatch(r'[a-zA-Z0-9_.@+\-]{3,80}', username) is None:
        raise ValueError('Provide an account name of 3..80 supported characters')
    manifest = dict(facts, client=str(root/'client'), http_port=http_port, login_port=login_port,
                    training_vehicle=vehicle, vehicle_progression=True, grant_all_vehicles=True,
                    training_bot=True, account_autocreate=False)
    network = parse_config(defaults(manifest))
    regions = initial_regions(game, network.advertise_http)
    raw = yaml.safe_dump(regions_config(regions, network), sort_keys=False, allow_unicode=True).encode()
    urls = yaml.safe_dump(dict(Location1=network.advertise_http+'/conf',
                               Location2=network.advertise_http+'/conf')).encode()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    password = secrets.token_urlsafe(24)
    salt = secrets.token_bytes(16)
    identity = dict(id=1, username=username, nickname='Commander', salt=salt.hex(),
                    password_hash=AccountStore.password_hash(password, salt).hex())
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.solbreaker-init-', dir=root.parent) as temporary:
        stage = Path(temporary)/'state'
        data = stage/'client/Data'
        data.mkdir(parents=True)
        private = stage/'private'
        private.mkdir(mode=0o700)
        save_training_config(training_config_path(stage), TrainingConfig(mode='bots', allies=6, enemies=7))
        for name in REGIONS:
            (data/name).write_bytes(dvpl.encode(raw))
        (data/'server_config_urls.yaml.dvpl').write_bytes(dvpl.encode(urls))
        (data/'loginapp_wot.pubkey.dvpl').write_bytes(dvpl.encode(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)))
        (stage/'regions.yaml').write_bytes(raw)
        (stage/'network.toml').write_text(network.toml())
        (stage/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        (private/'login.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (private/'account.json').write_text(json.dumps(identity, indent=2)+'\n')
        (private/'initial-login.txt').write_text(f'Username {username}\nPassword {password}\n')
        for path in private.iterdir():
            path.chmod(0o600)
        stage.chmod(0o700)
        os.rename(stage, root)
    return dict(state=str(root), credentials=str(root/'private/initial-login.txt'),
                http=network.advertise_http)
