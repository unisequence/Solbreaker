"""Battle controls hosted by the pinned native CreateRoom controller."""
from pathlib import Path
import struct

from . import dvpl

FORM = 'UI/Screens/Lobby/Training/CreateRoom.yaml.dvpl'
ACTIONS = 'UI/Screens/Lobby/Training/LocalBattleSettings.actions.dvpl'
TITLE = 'SolbreakerBotSetup'
PREFIX = 'SolbreakerBots/1/'
SAVED_RESULT = 127
LOADED_RESULT = 128
GET_TITLE = 'SolbreakerBotGet'
EXTENSION_VA = 0x4B1DE00
EXTENSION_REPLY_VA = 0x4B1DE64

# The stock creator hides its pending overlay before handling join failures.
# Reserve one local result for a successful settings save, then return without
# the room-error dialog. All original failure codes retain their branches.
SAVE_REPLY_VA = 0x1E92B0D
SAVE_REPLY_BEFORE = bytes.fromhex('837d0c0f0f8596000000')
SAVE_REPLY_CAVE_VA = 0x40C88A
SAVE_REPLY_CAVE_BEFORE = b'\xcc' * 53


def _relative_jump(source, target, opcode=b'\xe9'):
    return opcode + struct.pack('<i', target - source - len(opcode) - 4)


SAVE_REPLY_CAVE_AFTER = (
    bytes((0x83, 0x7d, 0x0c, SAVED_RESULT))
    + _relative_jump(SAVE_REPLY_CAVE_VA + 4, 0x1E92BCC, b'\x0f\x84')
    + bytes.fromhex('837d0c0f')
    + _relative_jump(SAVE_REPLY_CAVE_VA + 14, 0x1E92BAD, b'\x0f\x85')
    + _relative_jump(SAVE_REPLY_CAVE_VA + 20, 0x1E92B17))
SAVE_REPLY_LEGACY_CAVE_AFTER = SAVE_REPLY_CAVE_AFTER + b'\xcc' * 28
# settings_init uses the caller's frame and PIC image base, then sends the
# same named bool parameter as GameTypeScreen with showMyRoom=false.
SAVE_REPLY_CAVE_AFTER += bytes.fromhex(
    "c645f0008d45f0508d83e002d403508d837c712e0450e8c2766101c3"
)
SAVE_REPLY_WINDOWS = ((SAVE_REPLY_VA, SAVE_REPLY_BEFORE),
                      (SAVE_REPLY_CAVE_VA, SAVE_REPLY_CAVE_BEFORE))

# Native GameTypeScreen callback. The body sends the same typed creation
# event as TrainingRoomList, with its normal room-creation argument.
# Rebuildable source lives in native/battle_settings_navigation.S.
NAVIGATION_VA = 0x1A5DA50
NAVIGATION_OFFSET = NAVIGATION_VA - 0x400000 - 0xC00
NAVIGATION_BEFORE = bytes.fromhex(
    "558bec6aff68dd6f280364a1000000005083ec3056a180d5140433c5508d45f464a3000000008b89"
    "200100008b01ff501483ec0c8bf08bcc8965f0894dec68cc766e04e808e7c3fec745fc01000000c7"
    "45fcffffffff8d4dc4e812e6c3fec745fc020000008d45c4508bcee8205ac9fec745fcffffffff8d"
    "4dc4e861ecc3fe8b4df464890d00000000595e8be55dc3"
)
NAVIGATION_LEGACY_AFTER = bytes.fromhex(
    "5589e583ec385653516a00e8300b000059e8000000005b81eb66da65018b89200100008b01ff5014"
    "89c6c745f0000000008d4dc86a018d45f050e8a1de40008d45c8508d4ddce84531c9fe8d45f0508d"
    "4ddc518d83a4092f045089f1e89f5cc9fe8d45dc8d8b60c02900516a016a1450e8538fb7008d4dcc"
    "e8536cc4fe5b5e89ec5dc3"
)
NAVIGATION_PREVIOUS_AFTER = bytes.fromhex(
    "5589e583ec385653e8000000005b81eb5dda65018b89200100008b01ff501489c689f1e82bee9afe"
    "c745f0000000008d4dc86a018d45f050e8a3de40008d45c8508d4ddce84731c9fe8d45f0508d4ddc"
    "518d83a4092f045089f1e8a15cc9fe8d45dc8d8b60c02900516a016a1450e8558fb7008d4dcce855"
    "6cc4fe5b5e89ec5dc3"
)
NAVIGATION_AFTER = bytes.fromhex(
    "5589e583ec3856535789cfe8000000005b81eb60da65018b89200100008b01ff501489c689f1e828"
    "ee9afec745f0000000008d4dc86a018d45f050e8a0de40008d45c8508d4ddce84431c9fe8d45f050"
    "8d4ddc518d83a4092f045089f1e89e5cc9fe8d45dc8d8b60c02900516a016a1450e8528fb7008d4d"
    "cce8526cc4fe89f9e82b030c035f5b5e89ec5dc3"
)
NAVIGATION_RELOCATIONS = ((0x43A4C68, 0x3A56), (0x43A4C6A, 0x3A66), (0x43A4C6C, 0x3A8F))


def patch_navigation(data):
    end = NAVIGATION_OFFSET + len(NAVIGATION_BEFORE)
    navigation = data[NAVIGATION_OFFSET:end]
    if navigation not in (NAVIGATION_BEFORE, NAVIGATION_LEGACY_AFTER.ljust(len(NAVIGATION_BEFORE), b'\x90'),
                          NAVIGATION_PREVIOUS_AFTER.ljust(len(NAVIGATION_BEFORE), b'\x90')):
        raise ValueError('Unsupported native battle-settings navigation')
    result = bytearray(data)
    for offset, expected in NAVIGATION_RELOCATIONS:
        expected_value = expected if navigation == NAVIGATION_BEFORE else 0
        if struct.unpack_from('<H', result, offset)[0] != expected_value:
            raise ValueError('Unsupported battle-settings relocation')
        struct.pack_into('<H', result, offset, 0)
    result[NAVIGATION_OFFSET:end] = NAVIGATION_AFTER.ljust(len(NAVIGATION_BEFORE), b"\x90")
    for (va, before), after in zip(SAVE_REPLY_WINDOWS, (
            _relative_jump(SAVE_REPLY_VA, EXTENSION_REPLY_VA).ljust(len(SAVE_REPLY_BEFORE), b'\x90'),
            SAVE_REPLY_CAVE_AFTER)):
        offset = va - 0x400000 - 0xC00
        previous = (_relative_jump(SAVE_REPLY_VA, SAVE_REPLY_CAVE_VA).ljust(len(before), b'\x90')
                    if va == SAVE_REPLY_VA else SAVE_REPLY_LEGACY_CAVE_AFTER)
        if data[offset:offset + len(before)] not in (before, previous, after):
            raise ValueError('Unsupported native battle-settings save callback')
        result[offset:offset + len(before)] = after
    # Append the adapter to the existing owned RX section. The original recon
    # blob stays byte-for-byte intact and keeps its own rebuild check.
    from . import client_recon as recon
    virtual = EXTENSION_VA - recon.BASE - recon.MKRAY_RVA + len(EXTENSION)
    raw = (virtual + 0x1ff) // 0x200 * 0x200
    result.extend(bytes(recon.MKRAY_RAW + raw - len(result)))
    at = EXTENSION_VA - recon.BASE - recon.MKRAY_RVA + recon.MKRAY_RAW
    result[at:at + len(EXTENSION)] = EXTENSION
    struct.pack_into('<I', result, recon.MKRAY_HEADER + 8, virtual)
    struct.pack_into('<I', result, recon.MKRAY_HEADER + 16, raw)
    struct.pack_into('<I', result, 0x1fc, 0x32edc00 + raw - 0x400)
    struct.pack_into('<I', result, 0x230, (recon.MKRAY_RVA + virtual + 0xfff) // 0x1000 * 0x1000)
    return bytes(result)


def _walk(node):
    yield node
    for child in node.get('children', []):
        yield from _walk(child)


def _button(name, label, event, width=64):
    return dict(prototype='StyledButton/StyledButton', name=name,
        size=[float(width), 56.], classes='button button-color-gray',
        components={'SizePolicy': {'horizontalPolicy': 'FixedSize', 'horizontalValue': float(width),
                                  'verticalPolicy': 'FixedSize', 'verticalValue': 56.},
                    'UIInputEventComponent': {'onTouchUpInside': event}},
        children=[{'path': 'Caption', 'bindings': [['UITextComponent.text', label]]}])


def _row(name, caption, buttons, *, conditional=True):
    row = dict(prototype='SettingItem', name=name, children=[
        {'path': 'Container/Caption', 'components': {'UITextComponent': {'text': caption}}},
        {'path': 'Container', 'children': [{
            'class': 'UIControl', 'name': 'LocalControls', 'input': False,
            'components': {'Anchor': {'rightAnchorEnabled': True, 'rightAnchor': 20.,
                                      'vCenterAnchorEnabled': True},
                           'LinearLayout': {'spacing': 8.},
                           'SizePolicy': {'horizontalPolicy': 'PercentOfChildrenSum',
                                          'verticalPolicy': 'PercentOfMaxChild'}},
            'children': buttons}]}])
    if conditional:
        row['bindings'] = [['visible', 'localBotSetup']]
    return row


RULES_CAPTION = ('when localRules == 3 -> localize("Local/BattleSettings/Gravity"), '
                 'localRules == 2 -> localize("Local/BattleSettings/Supremacy"), '
                 'localRules == 1 -> localize("Local/BattleSettings/Capture"), '
                 'localize("Local/BattleSettings/Elimination")')

ROOM_RULES_CAPTION = (RULES_CAPTION.replace('localRules', 'localRoomRules')
                     .replace('/Capture"', '/Encounter"'))
DESCRIPTION_FIELD = 'Container/TextFieldContainer/VerticalBox/Background/TextField'


def _room_rules(root):
    container, = [n for n in _walk(root) if n.get('name') == 'scrollContainerControl']
    fields = {n['name']: n for n in container['children']}
    if 'LocalRoomRules' in fields:
        return False
    local_vars = root['components']['UIDataLocalVarsComponent']['data']
    local_vars.extend((['int', 'localRoomRules', '1'], ['string', 'localRoomNotes', '""']))
    description, = [node for node in fields['Description']['children']
                    if node.get('path') == DESCRIPTION_FIELD]
    description.setdefault('components', {})['UIInputEventComponent'] = {
        'onValueChanged': 'LocalRoomNotesChanged', 'onValueChangedData': '_text'}
    rules = _row('LocalRoomRules', 'Local/BattleSettings/Rules', [
        _button('Choice', ROOM_RULES_CAPTION, 'LocalRoomRulesNext', 360)], conditional=False)
    rules['bindings'] = [['visible', 'not localBotSetup']]
    mode = fields['Mode']
    # The native controller looks up Mode/Container/Select and Name on open.
    mode['bindings'] = [['visible', 'false']]
    container['children'].insert(container['children'].index(mode)+1, rules)
    return True


def _room_bot_hint(description):
    paths = {
        'Container/Caption': 'Local/BattleSettings/RoomBotsCaption',
        'Container/TextFieldContainer/VerticalBox/Background/TextField/Hint':
            'Local/BattleSettings/RoomBotsHint',
    }
    changed = False
    for child in description['children']:
        key = paths.get(child.get('path'))
        if key is None:
            continue
        text = child['components']['UITextComponent']
        if text.get('text') != key:
            text['text'] = key
            changed = True
    return changed


def patch_form(document):
    root, = [n for n in document['Prototypes'] if n.get('name') == 'Content']
    container, = [n for n in _walk(root) if n.get('name') == 'scrollContainerControl']
    fields = {n['name']: n for n in container['children']}
    if set(fields) != {'Title', 'DescriptionSelect', 'Description', 'Map', 'Mode',
                       'Password', 'TeamKill', 'HideEnemyVehicles'}:
        raise ValueError('Unsupported native battle settings form')
    _room_bot_hint(fields['Description'])
    components = root['components']
    if 'UIActionComponent' in components or 'UIDataLocalVarsComponent' in components:
        raise ValueError('Native battle settings already have actions or local state')
    params = components.setdefault('UIDataParamsComponent', {}).setdefault('params', [])
    params += [
        ['int', 'localRecipe', '-1', '-1'],
        ['int', 'localReturnLevels', '2', '2'],
        ['bool', 'localSaved', 'false', 'false']]
    components['UIEventOnDataComponent0'] = {
        'watchedDataExpression': 'localRecipe', 'ifExpression': 'localRecipe >= 0',
        'eventName': 'LocalLoad'}
    components['UIEventOnDataComponent1'] = {
        'watchedDataExpression': 'localSaved', 'ifExpression': 'localSaved',
        'eventName': 'LocalSaved'}
    components['UIActionComponent'] = {'actionsPath': '~res:/'+ACTIONS.removesuffix('.dvpl')}
    components['UIDataLocalVarsComponent'] = {'data': [
        ['bool', 'localBotSetup', 'false'], ['int', 'localAllies', '6'],
        ['int', 'localEnemies', '7'], ['int', 'localDifficulty', '1'],
        ['int', 'localRules', '1']]}
    footer, = [n for n in _walk(root) if n.get('name') == 'CreateRoomButton']
    caption, = [n for n in footer['children'] if n.get('path') == 'Caption']
    footer['bindings'] = [['input', 'not localBotSetup or (localRecipe >= 0 and not localSaved)']]
    caption['bindings'] = [['UITextComponent.text',
        'when localBotSetup -> localize("Local/BattleSettings/Save"), localize("Local/BattleSettings/Create")']]
    for name in ('Title', 'DescriptionSelect', 'Description', 'Mode', 'Password', 'TeamKill',
                 'HideEnemyVehicles'):
        bindings = fields[name].setdefault('bindings', [])
        visible = next((binding for binding in bindings if binding[0] == 'visible'), None)
        if visible is None:
            bindings.append(['visible', 'not localBotSetup'])
        else:
            visible[1] = '(' + visible[1] + ') and not localBotSetup'
    allies = _row('LocalAllies', 'Local/BattleSettings/Allies', [
        _button('Less', '"-"', 'LocalAllyLess'),
        _button('Value', 'str(localAllies)', 'LocalNoop', 176),
        _button('More', '"+"', 'LocalAllyMore')])
    enemies = _row('LocalEnemies', 'Local/BattleSettings/Enemies', [
        _button('Less', '"-"', 'LocalEnemyLess'),
        _button('Value', 'str(localEnemies)', 'LocalNoop', 176),
        _button('More', '"+"', 'LocalEnemyMore')])
    difficulty = _row('LocalDifficulty', 'Local/BattleSettings/Difficulty', [
        _button('Choice', 'when localDifficulty == 0 -> localize("Local/BattleSettings/Easy"), '
            'localDifficulty == 1 -> localize("Local/BattleSettings/Normal"), '
            'localize("Local/BattleSettings/Hard")', 'LocalDifficultyNext', 360)])
    rules = _row('LocalRules', 'Local/BattleSettings/Rules', [
        _button('Choice', RULES_CAPTION, 'LocalRulesNext', 360)])
    container['children'] = [allies, enemies, difficulty, rules, *container['children']]
    _room_rules(root)


def extra_resources():
    actions = '''action LocalLoad()
{
    ChangeData(localBotSetup, true);
    ChangeData(localAllies, localRecipe % 10);
    ChangeData(localEnemies, int(localRecipe / 10) % 10);
    ChangeData(localDifficulty, int(localRecipe / 100) % 10);
    ChangeData(localRules, int(localRecipe / 1000) % 10);
    DirectEvent("LocalSync", ".");
}
action LocalSaved()
{
    Event("SolbreakerSettingsSaved", arg1=localReturnLevels);
}
action LocalSync()
{
    ChangeProperty("**/CreateRoomButton/Caption", "UITextComponent", "text",
        when localBotSetup -> localize("Local/BattleSettings/Save"), localize("Local/BattleSettings/Create"));
    if (localBotSetup)
    {
        ChangeProperty("**/Title/Container/TextFieldContainer/VerticalBox/Background/TextField", "", "text", "@TITLE@");
        ChangeProperty("**/Description/Container/TextFieldContainer/VerticalBox/Background/TextField", "", "text", "@PREFIX@" + str(localAllies)
            + "/" + str(localEnemies) + "/" + str(localDifficulty) + "/" + str(localRules));
    }
}
action LocalAllyLess() { ChangeData(localAllies, max(0, localAllies - 1)); DirectEvent("LocalSync", "."); }
action LocalAllyMore() { ChangeData(localAllies, min(6, localAllies + 1)); DirectEvent("LocalSync", "."); }
action LocalEnemyLess() { ChangeData(localEnemies, max(1, localEnemies - 1)); DirectEvent("LocalSync", "."); }
action LocalEnemyMore() { ChangeData(localEnemies, min(7, localEnemies + 1)); DirectEvent("LocalSync", "."); }
action LocalDifficultyNext()
{
    if (localDifficulty == 2) { ChangeData(localDifficulty, 0); }
    else { ChangeData(localDifficulty, localDifficulty + 1); }
    DirectEvent("LocalSync", ".");
}
action LocalRulesNext()
{
    if (localRules == 3) { ChangeData(localRules, 0); }
    else { ChangeData(localRules, localRules + 1); }
    DirectEvent("LocalSync", ".");
}
action LocalNoop() {}
action LocalRoomNotesChanged(string text)
{
    if (not localBotSetup) { ChangeData(localRoomNotes, text); }
}
action LocalRoomRulesNext()
{
    ChangeData(localRoomRules, (localRoomRules + 1) % 4);
    ChangeProperty("**/Description/Container/TextFieldContainer/VerticalBox/Background/TextField",
        "", "text", (when localRoomRules == 3 -> "[mode gravity] ",
        localRoomRules == 2 -> "[mode supremacy] ", localRoomRules == 0 -> "[mode elimination] ",
        "[mode encounter] ") + localRoomNotes);
}
'''
    return {ACTIONS: dvpl.encode(actions.replace('@TITLE@', TITLE).replace('@PREFIX@', PREFIX).encode())}


STRINGS = {
    'en': {'Settings': 'Battle settings', 'Kind': 'Battle type', 'Room': 'Training room', 'Allies': 'Allied bots',
           'Enemies': 'Enemy bots', 'Difficulty': 'Bot difficulty', 'Rules': 'Battle rules',
           'Easy': 'Easy', 'Normal': 'Normal', 'Hard': 'Hard', 'Capture': 'Base capture',
           'Elimination': 'Elimination', 'Encounter': 'Encounter',
           'Supremacy': 'Supremacy', 'Gravity': 'Gravity Force',
           'Save': 'Save', 'Saved': 'Settings saved', 'Create': 'Create',
           'RoomBotsCaption': 'Description and bots [bots 2:3]',
           'RoomBotsHint': 'Start with [bots 2:3] for teams 1 and 2, then add your notes.'},
    'ru': {'Settings': 'Настройки боя', 'Kind': 'Тип боя', 'Room': 'Тренировочная комната', 'Allies': 'Союзные боты',
           'Enemies': 'Боты противника', 'Difficulty': 'Сложность ботов', 'Rules': 'Правила боя',
           'Easy': 'Лёгкая', 'Normal': 'Обычная', 'Hard': 'Сложная', 'Capture': 'Захват базы',
           'Elimination': 'Перестрелка', 'Encounter': 'Встречный бой',
           'Supremacy': 'Превосходство', 'Gravity': 'Гравитация',
           'Save': 'Сохранить', 'Saved': 'Настройки сохранены', 'Create': 'Создать',
           'RoomBotsCaption': 'Описание и боты [bots 2:3]',
           'RoomBotsHint': 'Начни с [bots 2:3] для команд 1 и 2, затем добавь описание.'}}


def upgrade_form(lab, manifest):
    """Refresh the installed native battle settings form in an idle client."""
    if manifest.get('battle_settings_ui_version') == 5:
        return manifest
    from hashlib import sha256
    import json
    import yaml
    from .client_ui import STRINGS_EN, STRINGS_RU, _StringsLoader
    from .connection import atomic_write

    overrides = dict(manifest.get('ui_overrides', {}))
    if not all(relative in overrides for relative in (FORM, ACTIONS, STRINGS_EN, STRINGS_RU)):
        return manifest
    data = lab/'client/Data'
    staged = {}
    for relative in (FORM, STRINGS_EN, STRINGS_RU):
        target = data/relative
        if target.resolve(strict=True) != target or not target.is_file():
            raise ValueError('Room bot settings require a private UI overlay')
        original = target.read_bytes()
        raw = dvpl.decode(original)
        if relative == FORM:
            if manifest.get('battle_settings_ui_version') == 3:
                source = Path(manifest.get('source_game', manifest['game']))/'Data'/FORM
                document = yaml.safe_load(dvpl.read(source))
                patch_form(document)
                changed = True
            else:
                document = yaml.safe_load(raw)
                changed = False
            root, = [node for node in document['Prototypes'] if node.get('name') == 'Content']
            description, = [node for node in _walk(root) if node.get('name') == 'Description']
            changed = _room_bot_hint(description) or changed
            rules, = [node for node in _walk(root) if node.get('name') == 'LocalRules']
            caption, = [node for node in _walk(rules) if node.get('path') == 'Caption']
            binding = ['UITextComponent.text', RULES_CAPTION]
            if caption['bindings'] != [binding]:
                caption['bindings'] = [binding]
                changed = True
            if any(node.get('name') == 'scrollContainerControl' for node in _walk(root)):
                changed = _room_rules(root) or changed
                room_rules, = [node for node in _walk(root) if node.get('name') == 'LocalRoomRules']
                room_caption, = [node for node in _walk(room_rules) if node.get('path') == 'Caption']
                room_binding = [['UITextComponent.text', ROOM_RULES_CAPTION]]
                if room_caption['bindings'] != room_binding:
                    room_caption['bindings'] = room_binding
                    changed = True
        else:
            document = yaml.load(raw, Loader=_StringsLoader)
            locale = 'en' if relative == STRINGS_EN else 'ru'
            strings = {f'Local/BattleSettings/{key}': STRINGS[locale][key]
                       for key in ('RoomBotsCaption', 'RoomBotsHint', 'Gravity', 'Encounter')}
            changed = any(document.get(key) != value for key, value in strings.items())
            document.update(strings)
        content = (dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                                              width=1 << 30).encode()) if changed else original)
        if changed:
            staged[target] = content
        overrides[relative] = sha256(content).hexdigest()
    actions = data/ACTIONS
    if actions.resolve(strict=True) != actions or not actions.is_file():
        raise ValueError('Battle settings require a private actions file')
    actions_content = extra_resources()[ACTIONS]
    if actions.read_bytes() != actions_content:
        staged[actions] = actions_content
    overrides[ACTIONS] = sha256(actions_content).hexdigest()
    for target, content in staged.items():
        atomic_write(target, content)
    updated = dict(manifest, ui_overrides=overrides, room_bot_hint_version=1,
                   battle_settings_ui_version=5)
    atomic_write(lab/'manifest.json', (json.dumps(updated, indent=2)+'\n').encode())
    return updated

# native/battle_settings_client.S, linked at EXTENSION_VA.
EXTENSION = bytes.fromhex(
    "5589e583ec3056e8b47f23fc89c18b01ff503089c68d83b5df7104508d4de8e8acc9b7fb8d83c6df7104508d4dd0e89dc9b7"
    "fb6a006a016a008d45d0508d45e85068ff0300006a006aff89f1e81f88bffc8d4de8e8c7c8b7fb8d4dd0e8bfc8b7fb5ec9c3"
    "837d0c7f7418817d0c80000000740f837d0c0f0f85304d37fde9954c37fd60e8000000005b81eb88de71048bbe780100008b"
    "45088d93c7df710489f9e87f000000817d0c800000007561b8030000008d93d3df710489f9e8640000008b450831d2b91027"
    "0000f7f185c0744e8d8bf0df71048b1185d2744239d0740583c108ebf18b510401da83ec1889e152e8dbc8b7fb89e0508d8e"
    "44010000e8ed3d37fd89e1e816c8b7fb83c418eb0f6a018d93e5df710489f9e84f00000061e9a84c37fd5589e583ec305657"
    "89ce89d78945d06a018d45d0508d4dd4e80fb5b9fb578d4de8e886c8b7fb8d45d4508d45e85089f1e87767c1fb8d4de8e8bf"
    "c7b7fb8d4dd8e8b767b8fb5f5ec9c35589e583ec2c565789ce89d76a018d4508508d4dd4e8c992bffb578d4de8e840c8b7fb"
    "8d45d4508d45e85089f1e83167c1fb8d4de8e879c7b7fb8d4dd8e87167b8fb5f5ec9c20400536f6c627265616b6572426f74"
    "47657400006c6f63616c526563697065006c6f63616c52657475726e4c6576656c73006c6f63616c53617665640001000000"
    "08e171040200000036e171040300000073e1710404000000a7e1710405000000d5e17104060000000fe27104070000003de2"
    "71040800000074e271040900000099e271040a000000d3e271040b00000006e371040c00000034e371040d0000005ce37104"
    "0e00000081e371040f000000afe3710413000000d4e371041400000008e471041500000033e47104170000005be471041900"
    "000083e471041a000000b5e471041b000000e0e471041e0000000be571041f00000030e571042300000064e5710426000000"
    "89e5710427000000b4e5710428000000e8e571042a0000001ce671042b0000004ae67104530000006fe6710455000000a3e6"
    "710459000000dde67104640000000be771040000000000000000236d6170733a6b6172656c69613a31375f6b6172656c6961"
    "5f6b612f31375f6b6172656c69615f6b612e73633200236d6170733a6465736572745f747261696e3a30325f646573657274"
    "5f747261696e5f64742f30325f6465736572745f747261696e5f64742e73633200236d6170733a65726c656e626572673a30"
    "335f65726c656e626572675f65722f30335f65726c656e626572675f65722e73633200236d6170733a6b6172696572693a32"
    "335f6b6172696572695f6b722f32335f6b6172696572695f6b722e73633200236d6170733a616d69676f7376696c6c653a30"
    "355f616d69676f7376696c6c655f616d2f30355f616d69676f7376696c6c655f616d2e73633200236d6170733a7275646e69"
    "6b693a30365f7275646e696b695f72642f30365f7275646e696b695f72642e73633200236d6170733a6d65647665646b6f76"
    "6f3a30345f6d65647665646b6f766f5f6d642f30345f6d65647665646b6f766f5f6d642e73633200236d6170733a666f7274"
    "3a30375f666f72745f66742f30375f666f72745f66742e73633200236d6170733a68696d6d656c73646f72663a31395f6869"
    "6d6d656c73646f72665f686d2f31395f68696d6d656c73646f72665f686d2e73633200236d6170733a6d6f756e7461696e3a"
    "32315f6d6f756e7461696e5f6d6e742f32315f6d6f756e7461696e5f6d6e742e73633200236d6170733a736176616e6e613a"
    "30395f736176616e6e615f73762f30395f736176616e6e615f73762e73633200236d6170733a706c616e743a31315f706c61"
    "6e745f706e2f31315f706c616e745f706e2e73633200236d6170733a69646c653a30385f69646c655f69642f30385f69646c"
    "655f69642e73633200236d6170733a686f6c6c616e643a31365f686f6c6c616e645f686c2f31365f686f6c6c616e645f686c"
    "2e73633200236d6170733a706f72743a31345f706f72745f70742f31345f706f72745f70742e73633200236d6170733a6d61"
    "6c696e6f766b613a31325f6d616c696e6f766b615f6d612f31325f6d616c696e6f766b615f6d612e73633200236d6170733a"
    "706c6965676f3a31335f706c6965676f5f706c2f31335f706c6965676f5f706c2e73633200236d6170733a63616e616c3a31"
    "385f63616e616c5f636e2f31385f63616e616c5f636e2e73633200236d6170733a6974616c793a32325f6974616c795f6974"
    "2f32325f6974616c795f69742e73633200236d6170733a6d696c626173653a32345f6d696c69626173655f6d6c622f32345f"
    "6d696c69626173655f6d6c622e73633200236d6170733a6c61676f6f6e3a31355f6c61676f6f6e5f6c6e2f31355f6c61676f"
    "6f6e5f6c6e2e73633200236d6170733a63616e796f6e3a32355f63616e796f6e5f63612f32355f63616e796f6e5f63612e73"
    "633200236d6170733a726f636b3a32385f726f636b5f72632f32385f726f636b5f72632e73633200236d6170733a67726f73"
    "73626572673a33305f67726f7373626572675f73682f33305f67726f7373626572675f73682e73633200236d6170733a736b"
    "69743a32395f736b69745f736b2f32395f736b69745f736b2e73633200236d6170733a6c756d6265723a33315f6c756d6265"
    "725f6c6d2f33315f6c756d6265725f6c6d2e73633200236d6170733a66617573743a33325f66617573745f66615f6e696768"
    "742f33325f66617573745f66615f6e696768742e73633200236d6170733a666f726765636974793a33345f666f7267656369"
    "74795f66632f33345f666f726765636974795f66632e73633200236d6170733a6e657074756e653a33335f6e657074756e65"
    "5f6e742f33335f6e657074756e655f6e742e73633200236d6170733a726966743a33355f726966745f72742f33355f726966"
    "745f72742e73633200236d6170733a65726c656e626572675f6f6c643a65726c656e626572675f6f6c642f65726c656e6265"
    "72675f6f6c642e73633200236d6170733a616d69676f7376696c6c655f6f6c643a616d69676f7376696c6c655f6f6c642f61"
    "6d69676f7376696c6c655f6f6c642e73633200236d6170733a736176616e6e615f6f6c643a736176616e6e615f6f6c642f73"
    "6176616e6e615f6f6c642e73633200236d6170733a757365725f6d61703a30365f7275646e696b695f72642f757365725f6d"
    "61702e73633200"
)
