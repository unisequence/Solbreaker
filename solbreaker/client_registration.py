"""Native registration through the pinned client's native controllers."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml

from . import dvpl
from .client_lock import client_configuration_access
from .connection import atomic_write, ensure_lab
from .platform_runtime import require_executable_idle


ROOT = 'UI/Screens3/PreLogin/'
FORM = ROOT + 'NewAccount.yaml.dvpl'
ACTIONS = ROOT + 'NewAccount.actions.dvpl'
COUNTRY = ROOT + 'CountryChoosing.yaml.dvpl'
COUNTRY_ANIMATION = ROOT + 'SolbreakerRegistrationCountry.animation.dvpl'
PRELOGIN = ROOT + 'PreLogin.yaml.dvpl'
PRELOGIN_ACTIONS = ROOT + 'PreLogin.actions.dvpl'
VERSION = 'solbreaker-email-registration-v5'


def _controls(document):
    if isinstance(document, dict):
        yield document
        for value in document.values():
            yield from _controls(value)
    elif isinstance(document, list):
        for value in document:
            yield from _controls(value)


def resources(game):
    source = Path(game)/'Data'
    form = yaml.safe_load(dvpl.read(source/FORM))
    email, = [node for node in _controls(form) if node.get('name') == 'Email']
    email['components']['UIDataParamsComponent']['args']['maxLength'] = '80'
    actions = dvpl.read(source/ACTIONS)
    country = yaml.safe_load(dvpl.read(source/COUNTRY))
    prelogin = yaml.safe_load(dvpl.read(source/PRELOGIN))
    welcome_actions = [action for node in _controls(prelogin)
                       for action in node.get('eventActions', [])
                       if action == ['NEW_GAME_PRESSED', 'NEW_GAME_PRESSED', '']]
    if len(welcome_actions) != 1:
        raise ValueError('Unknown welcome registration event mapping')
    welcome_actions[0][1] = 'SOLBREAKER_CREATE_ACCOUNT'
    welcome_script = dvpl.read(source/PRELOGIN_ACTIONS) + b'''
action SOLBREAKER_CREATE_ACCOUNT
{
    Event("LOGIN_PRESSED");
    Wait(0.6);
    Event("NEW_ACCOUNT_PRESSED");
}
'''
    screen, = country['Prototypes']
    screen['components']['UIAnimationComponent'] = dict(
        animations='_INIT,~res:/UI/Screens3/PreLogin/SolbreakerRegistrationCountry.animation')
    screen['components']['UIOpacityComponent'] = dict(opacity=0.0)
    animation = b'animation CONFIRM_COUNTRY\n{\n    Event("ON_CONFIRM_COUNTRY");\n}\n'
    return {COUNTRY_ANIMATION: dvpl.encode(animation),
            PRELOGIN: dvpl.encode(yaml.safe_dump(prelogin, sort_keys=False, allow_unicode=True).encode()),
            PRELOGIN_ACTIONS: dvpl.encode(welcome_script),
            ACTIONS: dvpl.encode(actions),
            COUNTRY: dvpl.encode(yaml.safe_dump(country, sort_keys=False, allow_unicode=True).encode()),
            FORM: dvpl.encode(yaml.safe_dump(form, sort_keys=False, allow_unicode=True).encode())}


def _private_directory(path, game):
    if not path.is_symlink():
        if not path.is_dir():
            raise ValueError(f'Missing private client directory: {path}')
        return
    source = path.resolve(strict=True)
    if not source.is_relative_to(game):
        raise ValueError('Client resource directory points outside its pinned game')
    previous = path.readlink()
    temporary = Path(tempfile.mkdtemp(prefix='.registration-', dir=path.parent))
    try:
        for item in source.iterdir():
            (temporary/item.name).symlink_to(item, target_is_directory=item.is_dir())
        path.unlink()
        try:
            os.rename(temporary, path)
        except OSError:
            path.symlink_to(previous, target_is_directory=True)
            raise
    finally:
        if temporary.exists():
            for item in temporary.iterdir():
                item.unlink()
            temporary.rmdir()


def install(lab):
    with client_configuration_access(lab):
        lab, manifest = ensure_lab(lab)
        from .resource_source import inspect_game
        game = Path(inspect_game(manifest['game'])['game'])
        executable = lab/'client/wotblitz.exe'
        require_executable_idle(executable)
        if (executable.is_symlink() or os.path.samefile(executable, game/'wotblitz.exe')
                or hashlib.sha256(executable.read_bytes()).hexdigest() != manifest.get('client_exe_sha256')):
            raise ValueError('Registration requires a separate recorded prepared client')
        patches = resources(game)
        previous = manifest.get('registration_client', {}).get('outputs', {})
        for relative, data in patches.items():
            path = lab/'client/Data'/relative
            if relative == COUNTRY_ANIMATION and not path.exists():
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            supported = {hashlib.sha256(data).hexdigest(), previous.get(relative)}
            if relative != COUNTRY_ANIMATION:
                supported.add(hashlib.sha256((game/'Data'/relative).read_bytes()).hexdigest())
            if actual not in supported:
                raise ValueError(f'Unknown private registration resource: {relative}')
        for relative, data in patches.items():
            path = lab/'client/Data'/relative
            parent = lab/'client/Data'
            for part in Path(relative).parts[:-1]:
                parent /= part
                _private_directory(parent, game)
            descriptor, temporary = tempfile.mkstemp(prefix='.registration-', dir=path.parent)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(data)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        manifest['registration_client'] = dict(version=VERSION,
            outputs={name: hashlib.sha256(data).hexdigest() for name, data in patches.items()})
        atomic_write(lab/'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode())
        return dict(client=str(lab), registration=VERSION)
