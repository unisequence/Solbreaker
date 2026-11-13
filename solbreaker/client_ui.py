"""Small UI overrides for services that the local server does not implement.

Only the lab overlay is writable. The pinned game and the Steam installation
stay intact; generated game resources are never part of the repository.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml

from . import dvpl, client_branding, client_provisions, client_recon, client_bot_mode, client_battle_settings


BASE_HEADER = "UI/Screens3/Lobby/Common/Headers/BaseHeader.yaml.dvpl"
BASE_LOBBY = "UI/Screens3/BaseLobby.yaml.dvpl"
BASE_LOBBY_ACTIONS = "UI/Screens3/BaseLobby.actions.dvpl"
STATS_ITEM = "UI/Screens3/Lobby/StatsInfoPanelItem.yaml.dvpl"
INVENTORY_PANEL = "UI/Screens3/Lobby/InventoryPanel/InventoryPanel.yaml.dvpl"
INVENTORY_ACTIONS = "UI/Screens3/Lobby/InventoryPanel/InventoryPanel.actions.dvpl"
HANGAR = "UI/Screens3/Lobby/Hangar/Hangar.yaml.dvpl"
HANGAR_ACTIONS = "UI/Screens3/Lobby/Hangar/Hangar.actions.dvpl"
BATTLE_BUTTON = "UI/Screens3/Lobby/Hangar/BattleButton.yaml.dvpl"
TANKS_PANEL = "UI/Screens3/Lobby/Hangar/TanksPanel/TanksPanel.yaml.dvpl"
OWNED_TANK_CELL = "UI/Screens3/Lobby/Hangar/TanksPanel/OwnedTankCell.yaml.dvpl"
INVENTORY = "UI/Screens3/Lobby/Inventory/Inventory.yaml.dvpl"
SIDEBAR = "UI/Screens3/Lobby/Common/Sidebar/SidebarHangarMenu.yaml.dvpl"
ACCOUNT_OPTIONS = "UI/Screens/Common/Settings/PrimaryTab/PrimaryOptionsPage.yaml.dvpl"
KEYBOARD_OPTIONS = "UI/Screens/Common/Settings/KeyboardMouseControlOptionsPage.yaml.dvpl"
GAME_TYPES = "UI/Screens/Lobby/Hangar/GameTypeScreen.yaml.dvpl"
GAME_TYPE_CARD = "UI/Screens/Lobby/Hangar/GameTypeSelector.yaml.dvpl"
AVATAR_RATING = "UI/Screens3/Lobby/Hangar/PlayerAvatarWithRating.yaml.dvpl"
CALIBRATION_POPUP = "UI/Screens3/Lobby/Hangar/CalibrationPopup.yaml.dvpl"
NOTIFICATION_7X7 = "UI/Screens3/Lobby/Hangar/Notification7x7Enabled.yaml.dvpl"
SKIP_7X7 = "UI/Screens3/Lobby/Hangar/SolbreakerSkip7x7.animation.dvpl"
LOGIN_ENTRY = 'UI/Screens3/PreLogin/Entry.yaml.dvpl'
SERVER_SELECTION = 'UI/Screens3/PreLogin/ServerSelection.yaml.dvpl'
SERVER_MAP_ELEMENT = 'UI/Screens3/PreLogin/ServerMapElement.yaml.dvpl'
HIDDEN_SECTIONS = ("crew_skills", "clans", "tournaments")
LOCAL_MAP_STRING_PREFIX = "#maps:user_map:"
UI_RESOURCES = (BASE_HEADER, BASE_LOBBY, BASE_LOBBY_ACTIONS, STATS_ITEM, INVENTORY_PANEL, INVENTORY_ACTIONS,
                HANGAR, HANGAR_ACTIONS, BATTLE_BUTTON, TANKS_PANEL, OWNED_TANK_CELL, INVENTORY,
                SIDEBAR, ACCOUNT_OPTIONS, KEYBOARD_OPTIONS, GAME_TYPES, GAME_TYPE_CARD,
                AVATAR_RATING, CALIBRATION_POPUP, NOTIFICATION_7X7,
                LOGIN_ENTRY, SERVER_SELECTION, SERVER_MAP_ELEMENT,
                client_battle_settings.FORM, client_battle_settings.ROOM, *client_recon.MARKER_OPACITY)
STRINGS_EN = "Strings/en.yaml.dvpl"
STRINGS_RU = "Strings/ru.yaml.dvpl"
SANDBOX_BINDINGS_VERSION = 8
SANDBOX_BINDING_STRINGS = {
    STRINGS_EN: {'Title': 'Solbreaker Sandbox',
                 'Boost': 'Toggle boost', 'Flight': 'Toggle flight',
                 'Missile': 'Toggle missile camera', 'Target': 'Place training target',
                 'Reload': 'Toggle fast reload',
                 'Jump': 'Gravity Force thruster',
                 'Boost0': 'Boost off', 'Boost1': 'Boost on',
                 'Fly0': 'Flight off', 'Fly1': 'Flight on',
                 'Cam0': 'Missile camera off', 'Cam1': 'Missile camera armed',
                 'Load0': 'Fast reload off', 'Load1': 'Fast reload on (0.5 s)',
                 'Spawn': 'Target placed', 'TP': 'Teleport to aim point',
                 'Moved': 'Teleported'},
    STRINGS_RU: {'Title': 'Песочница Solbreaker',
                 'Boost': 'Включить или выключить форсаж', 'Flight': 'Взлететь или сесть',
                 'Missile': 'Переключить камеру ПТУР', 'Target': 'Поставить мишень на Полигоне',
                 'Reload': 'Быстрая перезарядка',
                 'Jump': 'Прыгнуть в «Гравитации»',
                 'Boost0': 'Форсаж выключен', 'Boost1': 'Форсаж включён',
                 'Fly0': 'Полёт выключен', 'Fly1': 'Полёт включён',
                 'Cam0': 'Камера ПТУР выключена', 'Cam1': 'Камера ПТУР включится при пуске',
                 'Load0': 'Быстрая перезарядка выключена', 'Load1': 'Быстрая перезарядка включена (0,5 с)',
                 'Spawn': 'Мишень установлена', 'TP': 'Телепорт в точку прицеливания',
                 'Moved': 'Танк перемещён'},
}
LOCAL_TRAINING_MAP_STRINGS = {
    STRINGS_EN: {"#maps:lagoon:15_lagoon_ln/15_lagoon_ln.sc2": "Lagoon"},
    STRINGS_RU: {"#maps:lagoon:15_lagoon_ln/15_lagoon_ln.sc2": "Лагуна"},
}
LOCAL_PROVISION_STRINGS = {
    STRINGS_EN: {'#artefacts:desert_power/name': 'Desert Kit',
                 '#artefacts:desert_power/descr': 'Effects are not implemented yet.',
                 '#artefacts:safety_set/descr': 'Effects are not implemented yet.'},
    STRINGS_RU: {'#artefacts:desert_power/name': 'Пустынный комплект',
                 '#artefacts:desert_power/descr': 'Эффекты пока не реализованы.',
                 '#artefacts:safety_set/descr': 'Эффекты пока не реализованы.'},
}
LOCAL_MODE_STRINGS = {
    STRINGS_EN: {'Local PoC': 'Solbreaker',
                 'Local/BotsTitle': 'Battle with bots',
                 'Local/BotsDescription': 'Fight alongside allied bots against enemy bots.',
                 'Local/TargetTitle': 'Training ground',
                 'Local/TargetDescription': 'Drive freely and practice against stationary targets.'},
    STRINGS_RU: {'Local PoC': 'Solbreaker',
                 'Local/BotsTitle': 'Бой с ботами',
                 'Local/BotsDescription': 'Бой вместе с союзными ботами против команды противника.',
                 'Local/TargetTitle': 'Полигон',
                 'Local/TargetDescription': 'Свободная езда и стрельба по неподвижным мишеням.'},
}
# Local Accelerated Aiming rule (consumables.AIMING_RATE_FACTOR) replaces the
# Reticle Calibration texts. No duration: timer devices may extend it and the
# native HUD shows the live one.
CONSUMABLE_STRINGS = {
    STRINGS_EN: {
        "consumables/fireControlSystem/Name": "Accelerated Aiming",
        "consumables/fireControlSystem/Description": "+30% to turret traverse speed and gun elevation speed.",
    },
    STRINGS_RU: {
        "consumables/fireControlSystem/Name": "Ускоренное наведение",
        "consumables/fireControlSystem/Description":
            "+30% к скорости поворота башни и вертикального наведения орудия.",
    },
}


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


class _StringsLoader(getattr(yaml, "CBaseLoader", yaml.BaseLoader)):
    """Scalars stay verbatim text; a duplicate mapping key is an error."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise ValueError("Unsupported string table key")
                if key in seen:
                    raise ValueError(f"Duplicate string table key: {key}")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _sandbox_binding_strings(relative):
    return {f'Local/Sb{key}': value
            for key, value in SANDBOX_BINDING_STRINGS[relative].items()}


def _weather_map_strings(document, relative):
    from .map_catalog import WEATHER_MAPS

    suffixes = {'evening': 'Закат', 'fog': 'Туман'} if relative == STRINGS_RU else {
        'evening': 'Sunset', 'fog': 'Fog'}
    directories = {'rudniki': '06_rudniki_rd', 'amigosville': '05_amigosville_am',
                   'italy': '22_italy_it'}
    for key, (_, base, preset) in WEATHER_MAPS.items():
        directory = directories[base]
        name = document.get(f'#maps:{base}:{directory}/{directory}.sc2')
        if name:
            document[f'#maps:{key}:{directory}/{key}.sc2'] = f'{name} — {suffixes[preset]}'


def _sandbox_controls(document):
    page, = (node for node in document['Prototypes'] if node['name'] == 'KeyboardMouseControlOptionsPage')
    scroll, = (node for node in page['children'] if node['name'] == 'ScrollView')
    container, = (node for node in scroll['children'] if node['name'] == 'scrollContainerControl')
    controls = container['children']
    bindings, = (node for node in controls if node['name'] == 'KeyBindings')

    title = {'class': 'UIStaticText', 'name': 'SolbreakerSandboxControls', 'input': False,
             'size': [1024., 38.], 'classes': 'pane-bg', 'components': {
                 'Anchor': {'leftAnchorEnabled': True, 'leftAnchor': 5.,
                            'rightAnchorEnabled': True, 'rightAnchor': 5.},
                 'Background': {'leftRightStretchCap': 5.},
                 'StyleSheet': {'styles': '~res:/UI/Screens3/Background.style.yaml'},
                 'UITextComponent': {'text': 'Local/SbTitle', 'fontPath': '~res:/Fonts/WarHeliosCondCBold.ttf',
                                     'fontSize': 22., 'color': [1., 1., 1., 1.],
                                     'colorInheritType': 'COLOR_IGNORE_PARENT',
                                     'align': ['HCENTER', 'VCENTER']}}}
    controls[:] = [node for node in controls if node['name'] != title['name']]
    controls.insert(controls.index(bindings), title)


def patched_strings(source, relative, *, navigation_enabled=True):
    document = yaml.load(dvpl.read(source), Loader=_StringsLoader)
    if not isinstance(document, dict):
        raise ValueError("Unsupported string table")
    for key, text in CONSUMABLE_STRINGS[relative].items():
        if not isinstance(document.get(key), str):
            raise ValueError(f"Missing string table key: {key}")
        document[key] = text
    # The pinned training catalog exposes Lagoon without either locale label.
    for key, text in LOCAL_TRAINING_MAP_STRINGS.get(relative, {}).items():
        document.setdefault(key, text)
    document.update(LOCAL_PROVISION_STRINGS.get(relative, {}))
    document.update(LOCAL_MODE_STRINGS.get(relative, {}))
    document.update(_sandbox_binding_strings(relative))
    _weather_map_strings(document, relative)
    locale = 'ru' if relative == STRINGS_RU else 'en'
    document.update({'Local/BattleSettings/'+key: value
                     for key, value in client_battle_settings.STRINGS[locale].items()})
    if navigation_enabled:
        document['#training_rooms:CreationScreen/Header'] = client_battle_settings.STRINGS[locale]['Settings']
    # Every scalar was loaded as text, so the dump quotes anything that would
    # otherwise resolve to another type; all other keys keep their values.
    return dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                                      width=1 << 30).encode())


def patched_resource(source, relative, *, navigation_enabled=True):
    if relative in CONSUMABLE_STRINGS:
        return patched_strings(source, relative, navigation_enabled=navigation_enabled)
    if relative not in UI_RESOURCES:
        raise ValueError(f"Unsupported UI resource: {relative}")
    if relative == BASE_LOBBY_ACTIONS:
        text = dvpl.read(source).decode('utf-8')
        return dvpl.encode((text + '''
action SolbreakerSettingsSaved(int arg1)
{
    Event("BACK_EVENT");
    Wait(0.2);
    Event("BACK_EVENT");
    Wait(0.2);
    if (arg1 == 3) { Event("BACK_EVENT"); }
    ChangeData(localSettingsSavedToast, true);
    Wait(3.0);
    ChangeData(localSettingsSavedToast, false);
}
''').encode('utf-8'))
    if relative == HANGAR_ACTIONS:
        text = dvpl.read(source).decode('utf-8')
        return dvpl.encode(client_bot_mode.patch_hangar_actions(text).encode('utf-8'))
    if relative == INVENTORY_ACTIONS:
        text = dvpl.read(source).decode('utf-8')
        before = 'Event("ON_INVENTORY_TAB_SELECT", arg1=selected);'
        if text.count(before) != 1:
            raise ValueError("Unsupported inventory selection action")
        # Hidden tabs must also be skipped by keyboard/controller selection.
        after = ('if (selected == InventoryTabType.CREW)\n'
                 '    {\n'
                 '        Event("ON_INVENTORY_TAB_SELECT", arg1=InventoryTabType.PROGRESS);\n'
                 '    }\n'
                 '    else\n'
                 '    {\n'
                 '        Event("ON_INVENTORY_TAB_SELECT", arg1=selected);\n'
                 '    }')
        return dvpl.encode(text.replace(before, after).encode('utf-8'))
    document = yaml.safe_load(dvpl.read(source))
    if relative == KEYBOARD_OPTIONS:
        _sandbox_controls(document)
    elif relative == BASE_LOBBY:
        root, = [n for n in document['Prototypes'] if n.get('name') == 'BaseLobby']
        root['components']['UIDataLocalVarsComponent']['data'].append(
            ['bool', 'localSettingsSavedToast', 'false'])
        root['children'].append({
            'class': 'UIControl', 'name': 'LocalSettingsSavedToast', 'input': False,
            'size': [420., 56.],
            'components': {
                'Background': {'drawType': 'DRAW_FILL', 'color': [0., 0., 0., .85]},
                'UITextComponent': {'text': 'Local/BattleSettings/Saved',
                    'fontPath': '~res:/Fonts/WarHeliosCondCBold.ttf', 'fontSize': 22.,
                    'color': [1., 1., 1., 1.], 'align': ['HCENTER', 'VCENTER']},
                'Anchor': {'hCenterAnchorEnabled': True, 'topAnchorEnabled': True,
                           'topAnchor': 150.}},
            'bindings': [['visible', 'localSettingsSavedToast']]})
    elif relative == BATTLE_BUTTON:
        client_bot_mode.patch_battle_button(document)
    elif relative == TANKS_PANEL:
        client_bot_mode.patch_tanks_panel(document)
    elif relative == OWNED_TANK_CELL:
        client_bot_mode.patch_owned_cell(document)
    elif relative in client_recon.MARKER_OPACITY:
        if not isinstance(document, dict) or document.get('Header') != {'version': 161}:
            raise ValueError('Unsupported recon marker resource: ' + relative)
        client_recon.add_marker_opacity(document, relative)
    elif relative == ACCOUNT_OPTIONS:
        # Keep the native account/settings controls; remove only the branding
        # block. Its parent layout reclaims the unused vertical space.
        node = document["Prototypes"][0]
        if document.get("Header") != {"version": 161} or node.get("name") != "PrimaryOptionsPage":
            raise ValueError("Unsupported account options prototype")
        for name in ("Content", "ScrollArea", "scrollContainerControl"):
            matches = [child for child in node.get("children", []) if child.get("name") == name]
            if len(matches) != 1:
                raise ValueError(f"Unsupported account options child: {name}")
            node = matches[0]
        container = node
        left, = [child for child in container["children"] if child.get("name") == "Left"]
        player, = [child for child in left["children"] if child.get("name") == "PlayerData"]
        for parent, name in ((player, "CompleteAccount"), (left, "SocialNetwork"), (left, "AccountInfo")):
            matches = [child for child in parent.get("children", []) if child.get("name") == name]
            if len(matches) != 1:
                raise ValueError(f"Unsupported local account block: {name}")
            parent["children"].remove(matches[0])
        node, = [child for child in container["children"] if child.get("name") == "Right"]
        logos = [child for child in node.get("children", []) if child.get("name") == "Logo"]
        if len(logos) != 1 or {child.get("name") for child in logos[0].get("children", [])} != {"DefaultLogo", "NeteaseLogo"}:
            raise ValueError("Unsupported account branding block")
        node["children"].remove(logos[0])
    elif relative == BASE_HEADER:
        expression = "when not isTutorialActive and showShopButton -> shopButton, null"
        matches = [item for item in walk_dicts(document) if item.get("button") == expression]
        if len(matches) != 1:
            raise ValueError("Unsupported BaseHeader shop binding")
        matches[0]["button"] = "null"
    elif relative == STATS_ITEM:
        prototype = document["Prototypes"][0]
        if prototype["name"] != "StatsPanelItem":
            raise ValueError("Unsupported currency panel prototype")
        bindings = [item for item in prototype["bindings"] if item[0] == "input"]
        if len(bindings) != 1:
            raise ValueError("Unsupported currency input binding")
        bindings[0][1] = "false"
        for name in ("UIInputEventComponent", "UIVirtualActionEventComponent0",
                     "UIShortcutEventComponent", "Focus", "UILongTapComponent"):
            prototype["components"].pop(name, None)
        plus = [item for item in walk_dicts(document) if "isPlusTopPaneVisible" in item]
        if len(plus) != 2:
            raise ValueError("Unsupported currency plus bindings")
        for item in plus:
            item["isPlusTopPaneVisible"] = "false"
    elif relative in (HANGAR, INVENTORY):
        matches = [item for item in walk_dicts(document)
                   if item.get("crewFragmentVisible") == "lobby.crewFragmentVisible"]
        if len(matches) != 1:
            raise ValueError("Unsupported inventory crew visibility binding")
        matches[0]["crewFragmentVisible"] = "false"
        if relative == HANGAR:
            client_bot_mode.patch_hangar(document)
    elif relative == INVENTORY_PANEL:
        # Remove the actual switch-bar element, not just its icon. The shared
        # panel is used both in the hangar and inside equipment screens.
        parents = [item for item in walk_dicts(document)
                   if any(child.get("name") == "Crew" for child in item.get("children", []))]
        if len(parents) != 1:
            raise ValueError("Unsupported inventory crew tab")
        parent = parents[0]
        crew, = [child for child in parent["children"] if child.get("name") == "Crew"]
        if crew.get("prototype") != "Item/Item":
            raise ValueError("Unsupported crew tab prototype")
        parent["children"].remove(crew)
    elif relative == SIDEBAR:
        for name in ("Clans", "Tournaments"):
            matches = [item for item in walk_dicts(document) if item.get("name") == name]
            if len(matches) != 1:
                raise ValueError(f"Unsupported sidebar item: {name}")
            component = matches[0]["components"]["UIChildComponent0"]
            if component["args"]["switchId"] != f"int(eMenuType.{name})":
                raise ValueError(f"Unsupported sidebar switch: {name}")
            # Empty wrappers have zero height; no live button or focus target.
            component["condition"] = "false"
        rooms = [item for item in walk_dicts(document)
                 if item.get("name") == "TrainingRooms"]
        if len(rooms) != 1:
            raise ValueError("Unsupported training rooms sidebar item")
        component = rooms[0]["components"]["UIChildComponent0"]
        if component["args"]["switchId"] != "int(eMenuType.TrainingRooms)":
            raise ValueError("Unsupported training rooms sidebar switch")
        # Local training rooms are available without tutorial progression.
        component["condition"] = "true"
    elif relative == GAME_TYPE_CARD:
        root, = document['Prototypes']
        if root.get('name') != 'GameType':
            raise ValueError('Unsupported game type card')
        root.setdefault('bindings', []).append(['visible',
            'mode.gameMode == eGameMode.Regular or mode.gameMode == eGameMode.Rating'])
        client_bot_mode.patch_card(document)
    elif relative == GAME_TYPES:
        panes = [item for item in walk_dicts(document) if item.get('name') == 'Pane'
                 and 'UIRepeatComponent' in item.get('components', {})]
        if len(panes) != 1:
            raise ValueError('Unsupported game type picker')
        panes[0]['components']['LinearLayout']['skipInvisible'] = True
        panes[0]['components']['UIRepeatComponent']['args']['hasBadge'] = 'false'
        checkbox, = [x for x in walk_dicts(document) if x.get('name') == 'CheckboxesContainer']
        checkbox['bindings'] = [['visible', 'false']]
        descriptions, = [x for x in walk_dicts(document) if x.get('name') == 'DescriptionContainer']
        descriptions['bindings'] = [['visible', 'true']]
        if navigation_enabled:
            descriptions['components']['Anchor']['bottomAnchor'] = 110.
            document['ImportedPackages'].append('~res:/UI/Screens/Common/StyledButton.yaml')
            info, = [x for x in walk_dicts(document) if x.get('name') == 'ButtonInfo']
            info.clear()
            info.update({
                'prototype': 'StyledButton/StyledButton', 'name': 'ButtonInfo',
                'classes': 'button button-color-gray',
                'bindings': [['visible', client_bot_mode.SELECTED_BOT_MODE]],
                'components': {
                    'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True,
                               'vCenterAnchor': 264.},
                    'SizePolicy': {'horizontalPolicy': 'FixedSize', 'horizontalValue': 360.,
                                   'verticalPolicy': 'FixedSize', 'verticalValue': 56.},
                    'UIInputEventComponent': {'onTouchUpInside': 'INFO_PRESSED'}},
                'children': [{'path': 'Caption', 'components': {
                    'UITextComponent': {'text': 'Local/BattleSettings/Settings'}}}]})
        else:
            info, = [x for x in walk_dicts(document) if x.get('name') == 'ButtonInfo']
            info['visible'] = False
    elif relative == AVATAR_RATING:
        root, = document['Prototypes']
        values = root['components']['UIDataLocalBindingsComponent']['data']
        matches = [row for row in values if row[:2] == ['bool', 'isRatingVisible']]
        if len(matches) != 1:
            raise ValueError('Unsupported avatar rating binding')
        # Mode 7 is a local bot battle, not competitive ranking/calibration.
        matches[0][2] = 'false'
    elif relative == CALIBRATION_POPUP:
        root, = document['Controls']
        slave, = [x for x in root['children'] if x.get('name') == 'Slave']
        slave['visible'] = False
        slave['bindings'] = [['visible', 'false']]
    elif relative in (LOGIN_ENTRY, SERVER_SELECTION, SERVER_MAP_ELEMENT):
        expressions = {
            'preLoginData.lastLogin.region': 'localize(preLoginData.lastLogin.region)',
            'preLoginData.serverNameTransitionData.nameFirst': 'localize(preLoginData.serverNameTransitionData.nameFirst)',
            'preLoginData.serverNameTransitionData.nameSecond': 'localize(preLoginData.serverNameTransitionData.nameSecond)',
            'capitalize(descr.name)': 'capitalize(localize(descr.name))',
        }
        changed = 0
        for node in walk_dicts(document):
            for binding in node.get('bindings', []):
                if binding[0] == 'UITextComponent.text' and binding[1] in expressions:
                    binding[1] = expressions[binding[1]]
                    changed += 1
        expected = 2 if relative == SERVER_SELECTION else 1
        if changed != expected:
            raise ValueError('Unsupported server display name bindings')
    elif relative == NOTIFICATION_7X7:
        root, = document['Prototypes']
        if root.get('name') != '7x7Enabled' or 'CLOSE_NOTIFICATION' not in root['components']['UIDataParamsComponent']['events']:
            raise ValueError('Unsupported 7x7 notification')
        root['components']['UIAnimationComponent'] = {
            'animations': '_INIT,~res:/'+SKIP_7X7.removesuffix('.dvpl')}
        root['components']['UIOpacityComponent'] = {'opacity': 0.0}
    elif relative == client_battle_settings.FORM and navigation_enabled:
        client_battle_settings.patch_form(document)
    elif relative == client_battle_settings.ROOM and navigation_enabled:
        client_battle_settings.patch_room(document)
    return dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode())


def local_directory(path, pinned_game):
    """Split one directory symlink into a writable directory of pinned links."""
    if not path.is_symlink():
        if not path.is_dir():
            raise ValueError(f"Missing lab resource directory: {path}")
        return
    source = path.resolve(strict=True)
    if not source.is_relative_to(pinned_game):
        raise ValueError(f"Resource directory is outside the pinned game: {path}")
    old_link = path.readlink()
    temporary = Path(tempfile.mkdtemp(prefix=".local-ui-", dir=path.parent))
    for item in source.iterdir():
        (temporary / item.name).symlink_to(item, target_is_directory=item.is_dir())
    path.unlink()
    try:
        os.rename(temporary, path)
    except OSError:
        path.symlink_to(old_link)
        raise


def install_ui_guards(lab):
    lab = lab.resolve(strict=True)
    manifest_path = lab / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    game = Path(manifest["game"]).resolve(strict=True)
    # Prepare every resource before changing the existing overlay.
    patches = {name: patched_resource(game / "Data" / name, name)
               for name in (*UI_RESOURCES, *CONSUMABLE_STRINGS)}
    from .server_identity import server_name
    for relative in CONSUMABLE_STRINGS:
        document = yaml.load(dvpl.decode(patches[relative]), Loader=_StringsLoader)
        document.update({'Local PoC': server_name(manifest), 'Solbreaker': server_name(manifest)})
        patches[relative] = dvpl.encode(yaml.safe_dump(
            document, sort_keys=False, allow_unicode=True, width=1 << 30).encode())
    patches[SKIP_7X7] = dvpl.encode(b'animation CONFIRM_7X7\n{\n    Event("CLOSE_NOTIFICATION");\n}\n')
    from .recon_config import requested_enabled
    from .client_tls import base_hash
    navigation_enabled = (requested_enabled()
                          or base_hash(manifest.get('client_exe_sha256')) == client_recon.OUTPUT_SHA256)
    if GAME_TYPES in patches and not navigation_enabled:
        patches[GAME_TYPES] = patched_resource(game/'Data'/GAME_TYPES, GAME_TYPES,
                                               navigation_enabled=False)
    provision_source = Path(manifest.get('source_game', game)).resolve(strict=True) / 'Data' / client_provisions.RESOURCE
    if provision_source.is_file():
        patches[client_provisions.RESOURCE] = client_provisions.patched_resource(provision_source)
    patches.update(client_branding.patches())
    patches.update(client_battle_settings.extra_resources())
    # Custom-vehicle game views can expose client/Data/3d as their source.
    # Branding must always rebuild from the immutable pinned installation,
    # otherwise the second launch consumes its own already-patched scenes.
    branding_source = Path(manifest.get("source_game", game)).resolve(strict=True)
    patches.update(client_branding.scene_patches(branding_source))
    patches.update(client_branding.descriptor_patches(branding_source))
    # The map picker is driven by the profile's maps.yaml. Preserve its local
    # display name when rebuilding the pinned localization overlays at launch.
    for relative in CONSUMABLE_STRINGS:
        target = lab / "client/Data" / relative
        if not target.is_file():
            continue
        current = yaml.load(dvpl.read(target), Loader=_StringsLoader)
        local_map_strings = {key: value for key, value in current.items()
                             if isinstance(key, str)
                             and key.startswith(LOCAL_MAP_STRING_PREFIX)
                             and isinstance(value, str)}
        if not local_map_strings:
            continue
        document = yaml.load(dvpl.decode(patches[relative]), Loader=_StringsLoader)
        document.update(local_map_strings)
        patches[relative] = dvpl.encode(yaml.safe_dump(
            document, sort_keys=False, allow_unicode=True, width=1 << 30).encode())
    changed = False
    overrides = dict(manifest.get("ui_overrides", {}))
    # Materialize the root before walking children: a real child directory
    # reached through client/Data -> source/Data is not itself a symlink.
    # Without this split, replacing a child would write into the input tree.
    local_directory(lab / 'client/Data', game)
    for relative, content in patches.items():
        parent = lab / "client/Data"
        for component in Path(relative).parts[:-1]:
            parent /= component
            local_directory(parent, Path(manifest.get('source_game', game)))
        target = parent / Path(relative).name
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            temporary = target.with_name(target.name + ".local-new")
            temporary.write_bytes(content)
            os.replace(temporary, target)
            changed = True
        overrides[relative] = hashlib.sha256(content).hexdigest()
    if (changed or manifest.get("ui_overrides") != overrides
            or manifest.get('sandbox_bindings_version') != SANDBOX_BINDINGS_VERSION
            or manifest.get("hidden_ui_sections") != list(HIDDEN_SECTIONS)):
        manifest["ui_overrides"] = overrides
        manifest['sandbox_bindings_version'] = SANDBOX_BINDINGS_VERSION
        manifest["shop_ui_disabled"] = True
        manifest["hidden_ui_sections"] = list(HIDDEN_SECTIONS)
        temporary = manifest_path.with_suffix(".new")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(temporary, manifest_path)
    return {"shop_ui_disabled": True, "hidden_sections": list(HIDDEN_SECTIONS),
            "changed": changed, "files": list(patches)}


def install_sandbox_bindings(lab, manifest):
    """Add the key reference to an already prepared, idle client."""
    if manifest.get('sandbox_bindings_version') == SANDBOX_BINDINGS_VERSION:
        return manifest
    from .connection import atomic_write

    data = lab/'client/Data'
    if not all((data/relative).is_file() for relative in (KEYBOARD_OPTIONS, *SANDBOX_BINDING_STRINGS)):
        return manifest
    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    overrides = dict(manifest.get('ui_overrides', {}))
    patches = {KEYBOARD_OPTIONS: patched_resource(data/KEYBOARD_OPTIONS, KEYBOARD_OPTIONS)}
    for relative in SANDBOX_BINDING_STRINGS:
        strings = yaml.load(dvpl.read(data/relative), Loader=_StringsLoader)
        strings.update(_sandbox_binding_strings(relative))
        _weather_map_strings(strings, relative)
        patches[relative] = dvpl.encode(yaml.safe_dump(strings, sort_keys=False, allow_unicode=True,
                                                       width=1 << 30).encode())
    for relative, content in patches.items():
        parent = data
        for component in Path(relative).parts[:-1]:
            parent /= component
            local_directory(parent, source)
            if parent.resolve(strict=True) != parent:
                raise ValueError(f'Expected a private client UI directory: {parent}')
        target = data/relative
        if target.is_symlink() or target.read_bytes() != content:
            atomic_write(target, content, replace_symlink=True)
        overrides[relative] = hashlib.sha256(content).hexdigest()
    updated = dict(manifest, ui_overrides=overrides, sandbox_bindings_version=SANDBOX_BINDINGS_VERSION)
    atomic_write(lab/'manifest.json', (json.dumps(updated, indent=2)+'\n').encode())
    return updated


def install_training_maps(lab, manifest):
    from .connection import atomic_write
    from .map_catalog import MAP_CATALOG_RESOURCE, expose_training_maps

    data = lab/'client/Data'
    target = data/MAP_CATALOG_RESOURCE
    if not target.is_file():
        return
    document = yaml.safe_load(dvpl.read(target))
    if not expose_training_maps(document):
        return
    local_directory(data, Path(manifest.get('source_game', manifest['game'])))
    atomic_write(target, dvpl.encode(yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True).encode()), replace_symlink=True)


def install_server_name(lab):
    """Update prepared locale overlays before launch, retaining their other labels."""
    from .connection import ensure_lab, atomic_write
    from .server_identity import server_name
    lab, manifest = ensure_lab(lab)
    name = server_name(manifest)
    if manifest.get('display_server_name') == name:
        return
    patches = {}
    for relative in CONSUMABLE_STRINGS:
        target = lab/'client/Data'/relative
        document = yaml.load(dvpl.read(target), Loader=_StringsLoader)
        if document.get('Local PoC') == name and document.get('Solbreaker') == name:
            continue
        document.update({'Local PoC': name, 'Solbreaker': name})
        patches[relative] = dvpl.encode(yaml.safe_dump(
            document, sort_keys=False, allow_unicode=True, width=1 << 30).encode())
    for relative, content in patches.items():
        parent = lab/'client/Data'
        local_directory(parent, Path(manifest['game']))
        for component in Path(relative).parts[:-1]:
            parent /= component
            local_directory(parent, Path(manifest.get('source_game', manifest['game'])))
        target = lab/'client/Data'/relative
        temporary = target.with_name(target.name+'.local-new')
        temporary.write_bytes(content)
        os.replace(temporary, target)
        manifest.setdefault('ui_overrides', {})[relative] = hashlib.sha256(content).hexdigest()
    manifest['display_server_name'] = name
    atomic_write(lab/'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode())
