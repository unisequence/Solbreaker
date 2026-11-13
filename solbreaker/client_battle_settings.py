"""Battle controls hosted by the pinned native CreateRoom controller."""
from pathlib import Path
import struct

from . import dvpl
from .battle_settings_wire import PREFIX, RULES_SHIFT
from .roster import TEAM_SIZE
from .training_config import MAX_ROOM_MINUTES

FORM = 'UI/Screens/Lobby/Training/CreateRoom.yaml.dvpl'
ROOM = 'UI/Screens/Lobby/Training/Room.yaml.dvpl'
ACTIONS = 'UI/Screens/Lobby/Training/LocalBattleSettings.actions.dvpl'
TITLE = 'SolbreakerBotSetup'
SAVED_RESULT = 127
LOADED_RESULT = 128
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

ROOM_MODE_VALUE = '(when roomModeOverride >= 0 -> roomModeOverride, (when roomInitialMode >= 0 -> roomInitialMode, 1))'
ROOM_LIVES_VALUE = '(when roomLivesOverride >= 0 -> roomLivesOverride, roomInitialLives)'
ROOM_AMMO_VALUE = '(when roomAmmoOverride >= 0 -> roomAmmoOverride, roomInitialAmmo)'
ROOM_MINUTES_VALUE = '(when roomMinutesOverride >= -1 -> roomMinutesOverride, roomInitialMinutes)'
ROOM_BOT_VALUES = {
    team: f'(when roomBots{team}Override >= 0 -> roomBots{team}Override, roomInitialBots{team})'
    for team in (1, 2)
}
ROOM_RULES_CAPTION = (RULES_CAPTION.replace('localRules', ROOM_MODE_VALUE)
                     .replace('/Capture"', '/Encounter"'))
ROOM_SAVE_CAPTION = (
    'when localBotSetup or roomInitialMode >= 0 -> localize("Local/BattleSettings/Save"), '
    'localize("Local/BattleSettings/Create")')
DESCRIPTION_FIELD = 'Container/TextFieldContainer/VerticalBox/Background/TextField'


def _room_rules(root):
    container, = [n for n in _walk(root) if n.get('name') == 'scrollContainerControl']
    fields = {n['name']: n for n in container['children']}
    if 'LocalRoomRules' in fields:
        return False
    rules = _row('LocalRoomRules', 'Local/BattleSettings/Rules', [
        _button('Choice', ROOM_RULES_CAPTION, 'LocalRoomRulesNext', 360)], conditional=False)
    rules['bindings'] = [['visible', 'not localBotSetup']]
    mode = fields['Mode']
    # The native controller looks up Mode/Container/Select and Name on open.
    mode['bindings'] = [['visible', 'false']]
    container['children'].insert(container['children'].index(mode)+1, rules)
    return True


ROOM_TIME_CAPTION = (
    'when localRoomMinutes < 0 -> localize("Local/BattleSettings/ModeDefault"), '
    'localRoomMinutes == 0 -> localize("Local/BattleSettings/NoTimer"), '
    'str(localRoomMinutes) + " " + localize("Local/BattleSettings/Minutes")'
).replace("localRoomMinutes", ROOM_MINUTES_VALUE)


def _room_duration_settings(root):
    container, = [n for n in _walk(root) if n.get('name') == 'scrollContainerControl']
    rows = container['children']
    rows[:] = [row for row in rows if row.get('name') not in
               ('LocalRoomLives', 'LocalRoomAmmo', 'LocalRoomDuration', 'LocalRoomOptions',
                'LocalRoomBots1', 'LocalRoomBots2')]
    room_rules, = [row for row in rows if row.get('name') == 'LocalRoomRules']
    components = root['components']
    local_vars = components['UIDataLocalVarsComponent']['data']
    local_vars[:] = [row for row in local_vars if not row[1].startswith('localRoom') and row[1] not in
                     ('roomModeOverride', 'roomLivesOverride', 'roomAmmoOverride', 'roomMinutesOverride',
                      'roomBots1Override', 'roomBots2Override')]
    room_params = (('roomInitialMode', -1), ('roomInitialLives', 0), ('roomInitialMinutes', -1),
                   ('roomInitialBots1', 0), ('roomInitialBots2', 0), ('roomInitialAmmo', 0))
    params = components['UIDataParamsComponent']['params']
    params[:] = [row for row in params if row[1] not in {name for name, _ in room_params}]
    params.extend([['int', name, str(value), str(value)] for name, value in room_params])
    for index in range(2, 5):
        components.pop(f'UIEventOnDataComponent{index}', None)
    # Each field inherits its saved value until the player changes that field.
    for name in ('localFlags', 'localValue'):
        if not any(row[1] == name for row in local_vars):
            local_vars.append(['int', name, '0'])
    local_vars.extend([
        ['int', 'roomModeOverride', '-1'],
        ['int', 'roomLivesOverride', '-1'],
        ['int', 'roomAmmoOverride', '-1'],
        ['int', 'roomMinutesOverride', '-2'],
        ['int', 'roomBots1Override', '-1'], ['int', 'roomBots2Override', '-1'],
    ])
    footer, = [node for node in _walk(root) if node.get('name') == 'CreateRoomButton']
    caption, = [node for node in footer['children'] if node.get('path') == 'Caption']
    caption['bindings'] = [['UITextComponent.text', ROOM_SAVE_CAPTION]]
    description, = [row for row in rows if row.get('name') == 'Description']
    for node in description['children']:
        if node.get('path') == DESCRIPTION_FIELD:
            node.get('components', {}).pop('UIInputEventComponent', None)

    def label(key):
        return f'localize("Local/BattleSettings/{key}")'

    lives = _row('LocalRoomLives', 'Local/BattleSettings/Lives', [
        _button('Single', label('SingleLife'), 'LocalRoomSingleLife', 162),
        _button('Unlimited', label('Endless'), 'LocalRoomUnlimitedLives', 190)], conditional=False)
    for button, value in zip(lives['children'][1]['children'][0]['children'], (0, 1)):
        button['bindings'] = [['classes', f'when {ROOM_LIVES_VALUE} == {value} -> '
                                '"button button-color-orange", "button button-color-gray"']]
    ammo = _row('LocalRoomAmmo', 'Local/BattleSettings/UnlimitedAmmo', [
        _button('Toggle', f'when {ROOM_AMMO_VALUE} == 1 -> {label("AmmoOn")}, {label("AmmoOff")}',
                'LocalRoomToggleAmmo', 190)], conditional=False)
    ammo['children'][1]['children'][0]['children'][0]['bindings'] = [['classes',
        f'when {ROOM_AMMO_VALUE} == 1 -> "button button-color-orange", "button button-color-gray"']]
    duration = _row('LocalRoomDuration', 'Local/BattleSettings/Duration', [
        _button('Less', '"-"', 'LocalRoomTimeLess', 54),
        _button('Value', ROOM_TIME_CAPTION, 'LocalRoomTimeDefault', 144),
        _button('More', '"+"', 'LocalRoomTimeMore', 54),
        _button('Endless', '"∞"', 'LocalRoomTimeEndless', 84)], conditional=False)
    bots = [
        _row(f'LocalRoomBots{team}', f'Local/BattleSettings/RoomBots{team}', [
            _button('Less', '"-"', f'LocalRoomBots{team}Less', 54),
            _button('Value', f'str({value})', 'LocalNoop', 236),
            _button('More', '"+"', f'LocalRoomBots{team}More', 54)], conditional=False)
        for team, value in ROOM_BOT_VALUES.items()
    ]
    for row in bots:
        row['bindings'] = [['visible', 'not localBotSetup']]
    duration_buttons = duration['children'][1]['children'][0]['children']
    duration_buttons[-1]['bindings'] = [['classes',
        f'when {ROOM_MINUTES_VALUE} == 0 -> "button button-color-orange", "button button-color-gray"']]
    for button in duration_buttons:
        if button['name'] in ('Less', 'More'):
            event = button['components']['UIInputEventComponent']['onTouchUpInside']
            button['components']['UILongTapComponent'] = {
                'pressTimeout': .35, 'repeatDelay': .12, 'longTapEvent': event}
    # The native save callback reads this field separately from the notes.
    mode_tag = ('(when localRoomRules == 3 -> "[mode gravity] ", '
                'localRoomRules == 2 -> "[mode supremacy] ", '
                'localRoomRules == 0 -> "[mode elimination] ", "[mode encounter] ")')
    lives_tag = '(when localRoomLives == 1 -> "[lives unlimited] ", "[lives single] ")'
    ammo_tag = (f'(when {ROOM_AMMO_VALUE} == 1 -> "[ammo unlimited] ", '
                'roomInitialAmmo == 1 -> "[ammo limited] ", "")')
    time_tag = ('(when localRoomMinutes < 0 -> "[time default] ", '
                'localRoomMinutes == 0 -> "[time endless] ", "[time " + str(localRoomMinutes) + "] ")')
    bots_tag = (f'"[bots " + str({ROOM_BOT_VALUES[1]}) + ":" + str({ROOM_BOT_VALUES[2]})'
                ' + "] [notes] "')
    tags = (' + '.join((mode_tag, lives_tag, ammo_tag, time_tag, bots_tag)).replace('localRoomRules', ROOM_MODE_VALUE)
            .replace('localRoomLives', ROOM_LIVES_VALUE).replace('localRoomMinutes', ROOM_MINUTES_VALUE))
    options = dict(name='LocalRoomOptions', visible=False, input=False, bindings=[['text', tags]])
    options['class'] = 'UITextField'
    index = rows.index(room_rules) + 1
    rows[index:index] = [lives, duration, *bots, ammo, options]
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
    footer['bindings'] = [['input', 'not localBotSetup or (localRecipe >= 0 and not localSaved)']]
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
    _room_duration_settings(root)


def extra_resources():
    actions = '''action LocalLoad()
{
    ChangeData(localBotSetup, true);
    ChangeData(localFlags, int(localRecipe / @RULES_BASE@));
    ChangeData(localValue, localRecipe % @RULES_BASE@);
    ChangeData(localAllies, localValue % 10);
    ChangeData(localEnemies, int(localValue / 10) % 10);
    ChangeData(localDifficulty, int(localValue / 100) % 10);
    ChangeData(localRules, int(localValue / 1000) % 10);
    ChangeData(roomLivesOverride, localFlags % 2);
    ChangeData(roomAmmoOverride, int(localFlags / 2) % 2);
    ChangeData(roomMinutesOverride, int(localFlags / 4) - 1);
    DirectEvent("LocalSync", ".");
}
action LocalSaved()
{
    Event("SolbreakerSettingsSaved", arg1=localReturnLevels);
}
action LocalSync()
{
    if (localBotSetup)
    {
        ChangeProperty("**/Title/Container/TextFieldContainer/VerticalBox/Background/TextField", "", "text", "@TITLE@");
        ChangeProperty("**/Description/Container/TextFieldContainer/VerticalBox/Background/TextField", "", "text", "@PREFIX@" + str(localAllies)
            + "/" + str(localEnemies) + "/" + str(localDifficulty) + "/" + str(localRules)
            + "/" + str(@ROOM_LIVES@) + "/" + str(@ROOM_MINUTES@) + "/" + str(@ROOM_AMMO@));
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
action LocalRoomRulesNext() { ChangeData(roomModeOverride, (@ROOM_MODE@ + 1) % 4); }
action LocalRoomSingleLife() { ChangeData(roomLivesOverride, 0); DirectEvent("LocalSync", "."); }
action LocalRoomUnlimitedLives() { ChangeData(roomLivesOverride, 1); DirectEvent("LocalSync", "."); }
action LocalRoomToggleAmmo() { ChangeData(roomAmmoOverride, 1 - @ROOM_AMMO@); DirectEvent("LocalSync", "."); }
action LocalRoomTimeLess()
{
    ChangeData(roomMinutesOverride, when @ROOM_MINUTES@ <= 0 -> 6, max(1, @ROOM_MINUTES@ - 1));
    DirectEvent("LocalSync", ".");
}
action LocalRoomTimeMore()
{
    ChangeData(roomMinutesOverride, when @ROOM_MINUTES@ <= 0 -> 8, min(@MAX_ROOM_MINUTES@, @ROOM_MINUTES@ + 1));
    DirectEvent("LocalSync", ".");
}
action LocalRoomTimeDefault() { ChangeData(roomMinutesOverride, -1); DirectEvent("LocalSync", "."); }
action LocalRoomTimeEndless() { ChangeData(roomMinutesOverride, 0); DirectEvent("LocalSync", "."); }
'''
    actions = actions.replace('@TITLE@', TITLE).replace('@PREFIX@', PREFIX)
    actions = actions.replace('@ROOM_MODE@', ROOM_MODE_VALUE).replace('@ROOM_MINUTES@', ROOM_MINUTES_VALUE)
    actions = actions.replace('@ROOM_AMMO@', ROOM_AMMO_VALUE).replace('@ROOM_LIVES@', ROOM_LIVES_VALUE)
    actions = actions.replace('@RULES_BASE@', str(1 << RULES_SHIFT))
    for team, value in ROOM_BOT_VALUES.items():
        actions += f"""
action LocalRoomBots{team}Less() {{ ChangeData(roomBots{team}Override, max(0, {value} - 1)); }}
action LocalRoomBots{team}More() {{ ChangeData(roomBots{team}Override, min({TEAM_SIZE}, {value} + 1)); }}
"""
    return {ACTIONS: dvpl.encode(actions.replace('@MAX_ROOM_MINUTES@', str(MAX_ROOM_MINUTES)).encode())}


STRINGS = {
    'en': {'Settings': 'Battle settings', 'Kind': 'Battle type', 'Room': 'Training room', 'Allies': 'Allied bots',
           'Enemies': 'Enemy bots', 'Difficulty': 'Bot difficulty', 'Rules': 'Battle rules',
           'Easy': 'Easy', 'Normal': 'Normal', 'Hard': 'Hard', 'Capture': 'Base capture',
           'Elimination': 'Elimination', 'Encounter': 'Encounter',
           'Supremacy': 'Supremacy', 'Gravity': 'Gravity Force',
           'Save': 'Save', 'Saved': 'Settings saved', 'Create': 'Create',
           'Lives': 'Lives per tank', 'Duration': 'Battle duration', 'Minutes': 'min', 'ModeDefault': 'Mode default',
           'SingleLife': 'One life', 'Endless': 'Unlimited', 'NoTimer': 'No limit',
           'UnlimitedAmmo': 'Unlimited ammo', 'AmmoOn': 'On', 'AmmoOff': 'Off',
           'RoomBotsCaption': 'Description',
           'RoomBotsHint': 'Optional', 'RoomBots1': 'Team 1 bots', 'RoomBots2': 'Team 2 bots',
           'RoomHeaderBots': 'Bots', 'RoomHeaderTime': 'Time'},
    'ru': {'Settings': 'Настройки боя', 'Kind': 'Тип боя', 'Room': 'Тренировочная комната', 'Allies': 'Союзные боты',
           'Enemies': 'Боты противника', 'Difficulty': 'Сложность ботов', 'Rules': 'Правила боя',
           'Easy': 'Лёгкая', 'Normal': 'Обычная', 'Hard': 'Сложная', 'Capture': 'Захват базы',
           'Elimination': 'Перестрелка', 'Encounter': 'Встречный бой',
           'Supremacy': 'Превосходство', 'Gravity': 'Гравитация',
           'Save': 'Сохранить', 'Saved': 'Настройки сохранены', 'Create': 'Создать',
           'Lives': 'Жизни на танк', 'Duration': 'Время боя', 'Minutes': 'мин', 'ModeDefault': 'По режиму',
           'SingleLife': 'Одна жизнь', 'Endless': 'Без ограничений', 'NoTimer': 'Без лимита',
           'UnlimitedAmmo': 'Бесконечные снаряды', 'AmmoOn': 'Вкл.', 'AmmoOff': 'Выкл.',
           'RoomBotsCaption': 'Описание',
           'RoomBotsHint': 'Необязательно', 'RoomBots1': 'Боты команды 1', 'RoomBots2': 'Боты команды 2',
           'RoomHeaderBots': 'Боты', 'RoomHeaderTime': 'Время'}}


def patch_room(document):
    """Keep room metadata in the native map and mode row, outside the notes."""
    root, = [node for node in document['Prototypes'] if node.get('name') == 'ScreenContainer']
    params = root['components']['UIDataParamsComponent']['params']
    params.extend(['int', f'roomHeader{name}', str(default), str(default)] for name, default in
                  (('Mode', -1), ('Minutes', -1), ('Bots1', 0), ('Bots2', 0)))
    header, = [node for node in _walk(root) if node.get('name') == 'HorizContainer2']
    mode, = [node for node in _walk(header) if node.get('name') == 'ModeTxt']
    mode['bindings'] = [['UITextComponent.text', ROOM_RULES_CAPTION.replace(
        ROOM_MODE_VALUE, '(when roomHeaderMode < 0 -> 1, roomHeaderMode)')]]
    summary = {
        'class': 'UIStaticText', 'name': 'RoomOptionsSummary',
        'classes': 'text2 text-color-blue', 'input': False,
        'components': {'UITextComponent': {'colorInheritType': 'COLOR_IGNORE_PARENT'},
                       'SizePolicy': {'horizontalPolicy': 'PercentOfContent',
                                      'verticalPolicy': 'PercentOfContent'}},
        'bindings': [['visible', 'roomHeaderMode >= 0'],
                     ['UITextComponent.text',
                      'localize("Local/BattleSettings/RoomHeaderTime") + " " + '
                      '(when roomHeaderMinutes < 0 -> localize("Local/BattleSettings/ModeDefault"), '
                      'roomHeaderMinutes == 0 -> localize("Local/BattleSettings/NoTimer"), '
                      'str(roomHeaderMinutes) + " " + localize("Local/BattleSettings/Minutes")) + '
                      '"  •  " + localize("Local/BattleSettings/RoomHeaderBots") + " " + '
                      'str(roomHeaderBots1) + ":" + str(roomHeaderBots2)']]
    }
    header['children'].append(summary)


def upgrade_form(lab, manifest):
    """Refresh the installed native battle settings form in an idle client."""
    if manifest.get('battle_settings_ui_version') == 13:
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
    has_room_form = False
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
            has_room_form = any(node.get('name') == 'scrollContainerControl' for node in _walk(root))
            if has_room_form:
                changed = _room_rules(root) or changed
                changed = _room_duration_settings(root) or changed
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
                       for key in ('RoomBotsCaption', 'RoomBotsHint', 'Gravity', 'Encounter',
                                   'Lives', 'Duration', 'Minutes', 'ModeDefault', 'SingleLife', 'Endless', 'NoTimer', 'RoomBots1', 'RoomBots2',
                                   'RoomHeaderBots', 'RoomHeaderTime', 'UnlimitedAmmo', 'AmmoOn', 'AmmoOff')}
            changed = any(document.get(key) != value for key, value in strings.items())
            document.update(strings)
        content = (dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                                              width=1 << 30).encode()) if changed else original)
        if changed:
            staged[target] = content
        overrides[relative] = sha256(content).hexdigest()
    if has_room_form and ROOM not in overrides:
        room = data/ROOM
        if room.parent.resolve(strict=True) != room.parent:
            raise ValueError('Room header requires a private UI overlay')
        game = Path(manifest.get('source_game') or manifest['game'])
        document = yaml.safe_load(dvpl.read(game/'Data'/ROOM))
        patch_room(document)
        content = dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                                             width=1 << 30).encode())
        staged[room] = content
        overrides[ROOM] = sha256(content).hexdigest()
    actions = data/ACTIONS
    if actions.resolve(strict=True) != actions or not actions.is_file():
        raise ValueError('Battle settings require a private actions file')
    actions_content = extra_resources()[ACTIONS]
    if actions.read_bytes() != actions_content:
        staged[actions] = actions_content
    overrides[ACTIONS] = sha256(actions_content).hexdigest()
    for target, content in staged.items():
        atomic_write(target, content, replace_symlink=target == data/ROOM)
    updated = dict(manifest, ui_overrides=overrides, room_bot_hint_version=1,
                   battle_settings_ui_version=13)
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

# Updated adapter installed by client_sandbox after the pinned patch chain.
SHARED_RULES_EXTENSION = bytes.fromhex(
    "5589e583ec3056e8b47f23fc89c18b01ff503089c68d83badf7104508d4de8e8acc9b7fb8d83cbdf7104508d4dd0e89dc9b7fb6a006a01"
    "6a008d45d0508d45e85068ff0300006a006aff89f1e81f88bffc8d4de8e8c7c8b7fb8d4dd0e8bfc8b7fb5ec9c3837d0c7f7418817d0c80"
    "000000740f837d0c0f0f85304d37fde9954c37fd60e8000000005b81eb88de71048bbe780100008b45088d93ccdf710489f9e884000000"
    "817d0c800000007566b8030000008d93d8df710489f9e8690000008b450831d225ffff1f00b910270000f7f185c0744e8d8b001a72048b"
    "1185d2744239d0740583c108ebf18b510401da83ec1889e152e8d6c8b7fb89e0508d8e44010000e8e83d37fd89e1e811c8b7fb83c418eb"
    "0f6a018d93eadf710489f9e84f00000061e9a34c37fd5589e583ec30565789ce89d78945d06a018d45d0508d4dd4e80ab5b9fb578d4de8"
    "e881c8b7fb8d45d4508d45e85089f1e87267c1fb8d4de8e8bac7b7fb8d4dd8e8b267b8fb5f5ec9c35589e583ec2c565789ce89d76a018d"
    "4508508d4dd4e8c492bffb578d4de8e83bc8b7fb8d45d4508d45e85089f1e82c67c1fb8d4de8e874c7b7fb8d4dd8e86c67b8fb5f5ec9c2"
    "0400536f6c627265616b6572426f7456322100006c6f63616c526563697065006c6f63616c52657475726e4c6576656c73006c6f63616c"
    "536176656400"
)
SHARED_RULES_MAPS = bytes.fromhex(
    "01000000481b720402000000761b720403000000b31b720404000000e71b720405000000151c7204060000004f1c7204070000007d1c72"
    "0408000000b41c720409000000d91c72040a000000131d72040b000000461d72040c000000741d72040d0000009c1d72040e000000c11d"
    "72040f000000ef1d720413000000141e720414000000481e720415000000731e7204170000009b1e720419000000c31e72041a000000f5"
    "1e72041b000000201f72041e0000004b1f72041f000000701f720423000000a41f720426000000c91f720427000000f41f720428000000"
    "282072042a0000005c2072042b0000008a20720453000000af20720455000000e3207204590000001d217204640000004b217204650000"
    "007521720466000000ad21720467000000dd2172046800000021227204690000005d2272046a0000008f2272040000000000000000236d"
    "6170733a6b6172656c69613a31375f6b6172656c69615f6b612f31375f6b6172656c69615f6b612e73633200236d6170733a6465736572"
    "745f747261696e3a30325f6465736572745f747261696e5f64742f30325f6465736572745f747261696e5f64742e73633200236d617073"
    "3a65726c656e626572673a30335f65726c656e626572675f65722f30335f65726c656e626572675f65722e73633200236d6170733a6b61"
    "72696572693a32335f6b6172696572695f6b722f32335f6b6172696572695f6b722e73633200236d6170733a616d69676f7376696c6c65"
    "3a30355f616d69676f7376696c6c655f616d2f30355f616d69676f7376696c6c655f616d2e73633200236d6170733a7275646e696b693a"
    "30365f7275646e696b695f72642f30365f7275646e696b695f72642e73633200236d6170733a6d65647665646b6f766f3a30345f6d6564"
    "7665646b6f766f5f6d642f30345f6d65647665646b6f766f5f6d642e73633200236d6170733a666f72743a30375f666f72745f66742f30"
    "375f666f72745f66742e73633200236d6170733a68696d6d656c73646f72663a31395f68696d6d656c73646f72665f686d2f31395f6869"
    "6d6d656c73646f72665f686d2e73633200236d6170733a6d6f756e7461696e3a32315f6d6f756e7461696e5f6d6e742f32315f6d6f756e"
    "7461696e5f6d6e742e73633200236d6170733a736176616e6e613a30395f736176616e6e615f73762f30395f736176616e6e615f73762e"
    "73633200236d6170733a706c616e743a31315f706c616e745f706e2f31315f706c616e745f706e2e73633200236d6170733a69646c653a"
    "30385f69646c655f69642f30385f69646c655f69642e73633200236d6170733a686f6c6c616e643a31365f686f6c6c616e645f686c2f31"
    "365f686f6c6c616e645f686c2e73633200236d6170733a706f72743a31345f706f72745f70742f31345f706f72745f70742e7363320023"
    "6d6170733a6d616c696e6f766b613a31325f6d616c696e6f766b615f6d612f31325f6d616c696e6f766b615f6d612e73633200236d6170"
    "733a706c6965676f3a31335f706c6965676f5f706c2f31335f706c6965676f5f706c2e73633200236d6170733a63616e616c3a31385f63"
    "616e616c5f636e2f31385f63616e616c5f636e2e73633200236d6170733a6974616c793a32325f6974616c795f69742f32325f6974616c"
    "795f69742e73633200236d6170733a6d696c626173653a32345f6d696c69626173655f6d6c622f32345f6d696c69626173655f6d6c622e"
    "73633200236d6170733a6c61676f6f6e3a31355f6c61676f6f6e5f6c6e2f31355f6c61676f6f6e5f6c6e2e73633200236d6170733a6361"
    "6e796f6e3a32355f63616e796f6e5f63612f32355f63616e796f6e5f63612e73633200236d6170733a726f636b3a32385f726f636b5f72"
    "632f32385f726f636b5f72632e73633200236d6170733a67726f7373626572673a33305f67726f7373626572675f73682f33305f67726f"
    "7373626572675f73682e73633200236d6170733a736b69743a32395f736b69745f736b2f32395f736b69745f736b2e73633200236d6170"
    "733a6c756d6265723a33315f6c756d6265725f6c6d2f33315f6c756d6265725f6c6d2e73633200236d6170733a66617573743a33325f66"
    "617573745f66615f6e696768742f33325f66617573745f66615f6e696768742e73633200236d6170733a666f726765636974793a33345f"
    "666f726765636974795f66632f33345f666f726765636974795f66632e73633200236d6170733a6e657074756e653a33335f6e65707475"
    "6e655f6e742f33335f6e657074756e655f6e742e73633200236d6170733a726966743a33355f726966745f72742f33355f726966745f72"
    "742e73633200236d6170733a65726c656e626572675f6f6c643a65726c656e626572675f6f6c642f65726c656e626572675f6f6c642e73"
    "633200236d6170733a616d69676f7376696c6c655f6f6c643a616d69676f7376696c6c655f6f6c642f616d69676f7376696c6c655f6f6c"
    "642e73633200236d6170733a736176616e6e615f6f6c643a736176616e6e615f6f6c642f736176616e6e615f6f6c642e73633200236d61"
    "70733a757365725f6d61703a30365f7275646e696b695f72642f757365725f6d61702e73633200236d6170733a7275646e696b695f6576"
    "656e696e673a30365f7275646e696b695f72642f7275646e696b695f6576656e696e672e73633200236d6170733a7275646e696b695f66"
    "6f673a30365f7275646e696b695f72642f7275646e696b695f666f672e73633200236d6170733a616d69676f7376696c6c655f6576656e"
    "696e673a30355f616d69676f7376696c6c655f616d2f616d69676f7376696c6c655f6576656e696e672e73633200236d6170733a616d69"
    "676f7376696c6c655f666f673a30355f616d69676f7376696c6c655f616d2f616d69676f7376696c6c655f666f672e73633200236d6170"
    "733a6974616c795f6576656e696e673a32325f6974616c795f69742f6974616c795f6576656e696e672e73633200236d6170733a697461"
    "6c795f666f673a32325f6974616c795f69742f6974616c795f666f672e73633200"
)
