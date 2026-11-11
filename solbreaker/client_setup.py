"""Prepare an independent client from the owner's pinned game and public profile."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .connection import REGIONS, atomic_write, connect_client
from .resource_copy import activate_no_replace, copy_resource_tree, resource_tree_facts
from .resource_source import inspect_game
from .setup_progress import report, report_copy


def prepare(game, destination, profile, *, windows=False, proton=None, steam=None):
    report('Проверяем версию и файлы игры…')
    facts = inspect_game(game)
    game = Path(facts['game'])
    destination = Path(destination).expanduser().absolute()
    profile = Path(profile).resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError('Choose an absent client directory')
    if windows and len(str(destination/'client'/'wotblitz.exe')) >= 260:
        raise ValueError('Choose a shorter client path. The pinned game supports paths shorter than 260 characters')
    if destination == game or game in destination.parents or destination in game.parents:
        raise ValueError('Client directory must be separate from game resources')
    if not windows:
        if proton is None or steam is None:
            raise ValueError('Linux client preparation requires --proton and --steam')
        proton = Path(proton).expanduser().resolve(strict=True)
        steam = Path(steam).expanduser().resolve(strict=True)
        if not proton.is_file() or not steam.is_dir():
            raise ValueError('Provide an installed Proton executable and Steam directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_facts = resource_tree_facts(game) if windows else None
    if windows and shutil.disk_usage(destination.parent).free < source_facts['allocated_bytes'] + (1 << 30):
        raise ValueError('Client copy requires the full game size plus 1 GiB of free space')
    from . import client_patch, client_recon, client_tls, windows_profile_patch
    from . import replay_patch, stats_patch, client_ui, replay_ui, scene_quality
    from .client_registration import install as install_registration
    with tempfile.TemporaryDirectory(prefix='.solbreaker-client-', dir=destination.parent) as temporary:
        stage = Path(temporary)/'installation'
        stage.mkdir(mode=0o700)
        client = stage/'client'
        if windows:
            report('Копируем ресурсы игры. Это может занять несколько минут…')
            copy_resource_tree(game, client, progress=report_copy)
        else:
            report('Подключаем ресурсы отдельного клиента…')
            client.mkdir()
            for item in game.iterdir():
                if item.name not in ('Data', 'wotblitz.exe'):
                    (client/item.name).symlink_to(item, target_is_directory=item.is_dir())
            (client/'Data').mkdir()
            for item in (game/'Data').iterdir():
                if item.name not in (*REGIONS, 'server_config_urls.yaml.dvpl', 'loginapp_wot.pubkey.dvpl'):
                    (client/'Data'/item.name).symlink_to(item, target_is_directory=item.is_dir())
        # Management resources are independent files for both resource policies.
        for name in (*REGIONS, 'server_config_urls.yaml.dvpl', 'loginapp_wot.pubkey.dvpl'):
            shutil.copyfile(game/'Data'/name, client/'Data'/name)
        (stage/'private').mkdir(mode=0o700)
        (stage/'documents').mkdir()
        report('Настраиваем клиент, интерфейс и подключение…')
        patched = client_patch.install_picker(game/'wotblitz.exe', client/'wotblitz.exe')
        from . import dvpl
        import yaml
        from .bootstrap import initial_regions
        regions = initial_regions(game, 'http://127.0.0.1:18080')
        (stage/'regions.yaml').write_text(yaml.safe_dump(regions, sort_keys=False, allow_unicode=True))
        manifest = dict(facts, client=str(client), documents=str(stage/'documents'),
                        client_exe_sha256=patched, client_only=True, resource_policy='copy' if windows else 'links',
                        runtime='windows-native' if windows else 'linux-proton', http_port=18080, login_port=20016)
        manifest_path = stage/'manifest.json'
        atomic_write(manifest_path, (json.dumps(manifest, indent=2)+'\n').encode())
        connect_client(stage, profile)
        client_patch.upgrade_damage_popup_timing(stage)
        client_patch.upgrade_marker_ray(stage)
        client_patch.upgrade_hold_fire(stage)
        replay_patch.upgrade(stage)
        stats_patch.upgrade(stage)
        client_recon.upgrade(stage)
        manifest = json.loads(manifest_path.read_text())
        image = windows_profile_patch.patch((client/'wotblitz.exe').read_bytes())
        client_patch._write_client(client/'wotblitz.exe', image)
        manifest.update(client_exe_sha256=hashlib.sha256(image).hexdigest(),
                        windows_profile=windows_profile_patch.METADATA)
        atomic_write(manifest_path, (json.dumps(manifest, indent=2)+'\n').encode())
        client_tls.upgrade(stage)
        client_ui.install_ui_guards(stage)
        replay_ui.install(stage)
        scene_quality.install(stage)
        install_registration(stage)
        client_tls.configure_environment({}, stage, json.loads(manifest_path.read_text()))
        if windows and resource_tree_facts(game) != source_facts:
            raise ValueError('Game resources changed during client preparation')
        atomic_write(client/'steam_appid.txt', b'444200\n')
        if windows:
            (stage/'start-client.cmd').write_bytes(
                b'@echo off\r\nset SteamAppId=444200\r\nset SteamGameId=444200\r\ncd /d "%~dp0client"\r\nstart "" "wotblitz.exe"\r\n')
        else:
            import shlex
            script = ('#!/usr/bin/env bash\nset -euo pipefail\n'
                      'cd -- "$(dirname -- "${BASH_SOURCE[0]}")"\n'
                      'export STEAM_COMPAT_DATA_PATH="$PWD/runtime/proton"\n'
                      f'export STEAM_COMPAT_CLIENT_INSTALL_PATH={shlex.quote(str(steam))}\n'
                      'export SteamAppId=444200 SteamGameId=444200\n'
                      'mkdir -p "$STEAM_COMPAT_DATA_PATH"\ncd client\n'
                      f'exec {shlex.quote(str(proton))} run "$PWD/wotblitz.exe"\n')
            (stage/'start-client.sh').write_text(script)
            (stage/'start-client.sh').chmod(0o755)
        manifest = json.loads(manifest_path.read_text())
        manifest.update(client=str(destination/'client'), documents=str(destination/'documents'))
        if proton is not None:
            manifest['proton'] = str(proton)
        atomic_write(manifest_path, (json.dumps(manifest, indent=2)+'\n').encode())
        report('Сохраняем подготовленный клиент…')
        activate_no_replace(stage, destination)
    return dict(client=str(destination), runtime=manifest['runtime'],
                executable_sha256=manifest['client_exe_sha256'])
