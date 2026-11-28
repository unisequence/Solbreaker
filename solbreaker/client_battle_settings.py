"""Battle controls hosted by the pinned native CreateRoom controller."""
from pathlib import Path
import struct

from . import big_boss, client_battle_setup, client_practice_picker, dvpl
from .battle_settings_wire import PREFIX, RULES_SHIFT
from .roster import LARGE_TEAM_SIZE, TEAM_SIZE
from .training_config import MAX_ROOM_MINUTES

FORM = 'UI/Screens/Lobby/Training/CreateRoom.yaml.dvpl'
TRAINING_SCREEN = 'UI/Screens/Lobby/Training/Training.yaml.dvpl'
SOLO_SCREEN = 'UI/Screens/Lobby/Training/SolbreakerBattleSettings.yaml.dvpl'
ROOM = 'UI/Screens/Lobby/Training/Room.yaml.dvpl'
ACTIONS = 'UI/Screens/Lobby/Training/LocalBattleSettings.actions.dvpl'
BATTLE_MENU = 'UI/Screens/Battle/GameMenuDialog.yaml.dvpl'
BOSS_ACTIONS = 'UI/Screens/Battle/SolbreakerBoss.actions.dvpl'

def _percent(value):
    return f'{value:.0%}'


# Picker values come from the same rules as combat.
BOSS_CARDS = (
    ('boss', 'Boss', 'Босс', 'Wave · Megashot', 'Волна · Мегавыстрел', (
        ('Max HP', 'Прочность', f'×{big_boss.HEALTH_MULTIPLIER}'),
        ('Reload speed', 'Перезарядка', f'×{big_boss.RELOAD_RATE:g}'),
        ('Blast radius', 'Радиус волны', f'{big_boss.BLAST_RADIUS:g}', 'm', 'м'),
    )),
    ('graviton', 'Graviton', 'Гравитон', 'Gravity Trap', 'Гравиловушка', (
        ('Pull radius', 'Радиус поля', tuple(f'{value:g}' for value in big_boss.TRAP_RADIUS), 'm', 'м'),
        ('Duration', 'Действие', f'{big_boss.TRAP_DURATION:g}', 's', 'с'),
        ('Absorption', 'Поглощение', tuple(map(_percent, big_boss.TRAP_SHIELD))),
    )),
    ('kamikaze', 'Bomber', 'Бомбардир', 'Blast', 'Взрыв', (
        ('Blast radius', 'Радиус взрыва', f'{big_boss.BOMB_RADIUS:g}', 'm', 'м'),
        ('Damage (% HP)', 'Урон от HP', tuple(map(_percent, big_boss.BOMB_DAMAGE))),
        ('Mobility', 'Мобильность', tuple('+' + _percent(value - 1) for value in big_boss.BOMB_MOBILITY)),
    )),
    ('healer', 'Regenerator', 'Регенератор', 'Healing Aura · Shield', 'Аура лечения · Щит', (
        ('Heal radius', 'Радиус лечения', f'{big_boss.HEAL_RADIUS:g}', 'm', 'м'),
        ('Ally HP / pulse', 'Лечение за тик', '+' + _percent(big_boss.HEAL_FRACTION)),
        ('Shield duration', 'Действие щита', tuple(f'{value:g}' for value in big_boss.SHIELD_DURATION), 's', 'с'),
    )),
    ('killer', 'Catalyst', 'Катализатор', 'Poisoned Shells', 'Ядовитые снаряды', (
        ('Damage bonus', 'Бонус урона', tuple('+' + _percent(value - 1) for value in big_boss.CATALYST_DAMAGE)),
        ('Poison per tick', 'Урон яда за тик', tuple(map(_percent, big_boss.POISON_DAMAGE))),
        ('Poison ticks', 'Импульсы яда', str(big_boss.POISON_TICKS)),
    )),
)


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
STANDALONE_NAVIGATION = _relative_jump(NAVIGATION_VA, 0x4b1e00e).ljust(
    len(NAVIGATION_BEFORE), b'\x90')


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
        children=[{'path': 'Caption', 'bindings': [['UITextComponent.text', label]],
                   'components': {'UITextComponent': {'fitting': 'FITTING_REDUCE'}}}])


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


def _counter_row(name, caption, value, less_event, more_event, *, conditional=True):
    """Use one visual control for bot counts in solo and room battles."""
    return _row(name, caption, [
        _button('Less', '"-"', less_event),
        _button('Value', f'str({value})', 'LocalNoop', 176),
        _button('More', '"+"', more_event),
    ], conditional=conditional)


def _difficulty_row(name, value, event, *, conditional=True):
    return _row(name, 'Local/BattleSettings/Difficulty', [
        _button('Choice', f'when {value} == 0 -> localize("Local/BattleSettings/Easy"), '
            f'{value} == 1 -> localize("Local/BattleSettings/Normal"), '
            'localize("Local/BattleSettings/Hard")', event, 360)
    ], conditional=conditional)


RULES_CAPTION = ('when localRules == 6 -> localize("Local/BattleSettings/Uprising"), '
                 'localRules == 5 -> localize("Local/BattleSettings/MadGames"), '
                 'localRules == 4 -> localize("Local/BattleSettings/BigBoss"), '
                 'localRules == 3 -> localize("Local/BattleSettings/Gravity"), '
                 'localRules == 2 -> localize("Local/BattleSettings/Supremacy"), '
                 'localRules == 1 -> localize("Local/BattleSettings/Capture"), '
                 'localize("Local/BattleSettings/Elimination")')

ROOM_MODE_VALUE = '(when roomModeOverride >= 0 -> roomModeOverride, (when roomInitialMode >= 0 -> roomInitialMode, 1))'
ROOM_LIVES_VALUE = '(when roomLivesOverride >= 0 -> roomLivesOverride, roomInitialLives)'
ROOM_AMMO_VALUE = '(when roomAmmoOverride >= 0 -> roomAmmoOverride, roomInitialAmmo)'
ROOM_TEAM_VALUE = '(when roomTeamOverride >= 0 -> roomTeamOverride, roomInitialLargeTeams)'
TEAM_VALUE = f'(when localBotSetup -> localLargeTeams, {ROOM_TEAM_VALUE})'
ROOM_DIFFICULTY_VALUE = '(when roomDifficultyOverride >= 0 -> roomDifficultyOverride, roomInitialDifficulty)'
ROOM_BOT_LIMIT = f'(when {ROOM_TEAM_VALUE} == 1 -> 29, {TEAM_SIZE})'
ROOM_MINUTES_VALUE = '(when roomMinutesOverride >= -1 -> roomMinutesOverride, roomInitialMinutes)'
ROOM_BOT_VALUES = {
    team: f'min({ROOM_BOT_LIMIT}, (when roomBots{team}Override >= 0 -> roomBots{team}Override, roomInitialBots{team}))'
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
        _button('Choice', ROOM_RULES_CAPTION, 'LocalOpenModePicker', 360)], conditional=False)
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
                'LocalRoomBots1', 'LocalRoomBots2', 'LocalRoomTeams', 'LocalRoomDifficulty',
                'LocalSoloBotHeading')]
    room_rules, = [row for row in rows if row.get('name') == 'LocalRoomRules']
    components = root['components']
    local_vars = components['UIDataLocalVarsComponent']['data']
    local_vars[:] = [row for row in local_vars if not row[1].startswith('localRoom') and row[1] not in
                     ('roomModeOverride', 'roomLivesOverride', 'roomAmmoOverride', 'roomMinutesOverride',
                      'roomBots1Override', 'roomBots2Override', 'roomTeamOverride',
                      'roomDifficultyOverride')]
    room_params = (('roomInitialMode', -1), ('roomInitialLives', 0), ('roomInitialMinutes', -1),
                   # A newly created training room starts from the same bot
                   # roster shown by the regular bot-battle form.
                   ('roomInitialBots1', 6), ('roomInitialBots2', 7), ('roomInitialAmmo', 0),
                   ('roomInitialLargeTeams', 0), ('roomInitialDifficulty', 1))
    params = components['UIDataParamsComponent']['params']
    params[:] = [row for row in params if row[1] not in {name for name, _ in room_params}]
    params.extend([['int', name, str(value), str(value)] for name, value in room_params])
    for index in range(2, 5):
        components.pop(f'UIEventOnDataComponent{index}', None)
    # Each field inherits its saved value until the player changes that field.
    for name in ('localFlags', 'localValue'):
        if not any(row[1] == name for row in local_vars):
            local_vars.append(['int', name, '0'])
    # Upgrades start from an existing form, not patch_form(). Declare the
    # new solo state there too or the action script cannot resolve it.
    if not any(row[1] == 'localLargeTeams' for row in local_vars):
        local_vars.append(['int', 'localLargeTeams', '0'])
    local_vars.extend([
        ['int', 'roomModeOverride', '-1'],
        ['int', 'roomTeamOverride', '-1'],
        ['int', 'roomLivesOverride', '-1'],
        ['int', 'roomAmmoOverride', '-1'],
        ['int', 'roomDifficultyOverride', '-1'],
        ['int', 'roomMinutesOverride', '-2'],
        ['int', 'roomBots1Override', '-1'], ['int', 'roomBots2Override', '-1'],
    ])
    footer, = [node for node in _walk(root) if node.get('name') == 'CreateRoomButton']
    caption, = [node for node in footer['children'] if node.get('path') == 'Caption']
    caption['bindings'] = [['UITextComponent.text', ROOM_SAVE_CAPTION]]
    bindings = footer.setdefault('bindings', [])
    bindings[:] = [binding for binding in bindings if binding[0] != 'Sound.touchUpInside']
    bindings.append(['Sound.touchUpInside', 'when localBotSetup -> "", "GUI/buttons/open"'])
    description, = [row for row in rows if row.get('name') == 'Description']
    for node in description['children']:
        if node.get('path') == DESCRIPTION_FIELD:
            node.get('components', {}).pop('UIInputEventComponent', None)

    def label(key):
        return f'localize("Local/BattleSettings/{key}")'

    lives = _row('LocalRoomLives', 'Local/BattleSettings/Lives', [
        _button('Single', label('SingleLife'), 'LocalRoomSingleLife', 162),
        _button('Unlimited', label('Endless'), 'LocalRoomUnlimitedLives', 190)], conditional=False)
    mode_value = f'(when localBotSetup -> localRules, {ROOM_MODE_VALUE})'
    lives['bindings'] = [['visible', f'{mode_value} != 4 and {mode_value} != 6']]
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
    teams = _row('LocalRoomTeams', 'Local/BattleSettings/TeamSize', [
        _button('Toggle', f'when {TEAM_VALUE} == 1 -> {label("ThirtyTanks")}, {label("StandardTeams")}',
                'LocalRoomToggleTeams', 190)], conditional=False)
    bots = [_counter_row(f'LocalRoomBots{team}',
                         f'Local/BattleSettings/{"Allies" if team == 1 else "Enemies"}', value,
                         f'LocalRoomBots{team}Less', f'LocalRoomBots{team}More', conditional=False)
            for team, value in ROOM_BOT_VALUES.items()]
    for row in bots:
        row['bindings'] = [['visible', 'not localBotSetup']]
    difficulty = _difficulty_row('LocalRoomDifficulty', ROOM_DIFFICULTY_VALUE,
                                 'LocalRoomDifficultyNext', conditional=False)
    difficulty['bindings'] = [['visible', 'not localBotSetup']]
    duration_buttons = duration['children'][1]['children'][0]['children']
    duration_buttons[-1]['bindings'] = [['classes',
        f'when {ROOM_MINUTES_VALUE} == 0 -> "button button-color-orange", "button button-color-gray"']]
    for button in duration_buttons:
        if button['name'] in ('Less', 'More'):
            event = button['components']['UIInputEventComponent']['onTouchUpInside']
            button['components']['UILongTapComponent'] = {
                'pressTimeout': .35, 'repeatDelay': .12, 'longTapEvent': event}
    # The native save callback reads this field separately from the notes.
    mode_tag = ('(when localRoomRules == 6 -> "[mode uprising] ", '
                'localRoomRules == 5 -> "[mode mad_games] ", '
                'localRoomRules == 4 -> "[mode big_boss] ", '
                'localRoomRules == 3 -> "[mode gravity] ", '
                'localRoomRules == 2 -> "[mode supremacy] ", '
                'localRoomRules == 0 -> "[mode elimination] ", "[mode encounter] ")')
    lives_tag = '(when localRoomLives == 1 -> "[lives unlimited] ", "[lives single] ")'
    ammo_tag = (f'(when {ROOM_AMMO_VALUE} == 1 -> "[ammo unlimited] ", '
                'roomInitialAmmo == 1 -> "[ammo limited] ", "")')
    time_tag = ('(when localRoomMinutes < 0 -> "[time default] ", '
                'localRoomMinutes == 0 -> "[time endless] ", "[time " + str(localRoomMinutes) + "] ")')
    bots_tag = (f'"[bots " + str({ROOM_BOT_VALUES[1]}) + ":" + str({ROOM_BOT_VALUES[2]})'
                ' + "] [notes] "')
    teams_tag = f'(when {ROOM_TEAM_VALUE} == 1 -> "[teams 15] ", "")'
    difficulty_tag = (f'(when {ROOM_DIFFICULTY_VALUE} == 0 -> "[difficulty easy] ", '
                      f'{ROOM_DIFFICULTY_VALUE} == 2 -> "[difficulty hard] ", '
                      '"[difficulty normal] ")')
    tags = (' + '.join((mode_tag, lives_tag, ammo_tag, time_tag, teams_tag,
                        difficulty_tag, bots_tag)).replace('localRoomRules', ROOM_MODE_VALUE)
            .replace('localRoomLives', ROOM_LIVES_VALUE).replace('localRoomMinutes', ROOM_MINUTES_VALUE))
    options = dict(name='LocalRoomOptions', visible=False, input=False, bindings=[['text', tags]])
    options['class'] = 'UITextField'
    bot_names = ('LocalAllies', 'LocalEnemies', 'LocalDifficulty', 'LocalRules')
    bot_controls = [row for row in rows if row.get('name') in bot_names]
    for row in bot_controls:
        if row['name'] in ('LocalAllies', 'LocalEnemies'):
            for button in row['children'][1]['children'][0]['children']:
                if button['name'] in ('Less', 'More'):
                    bindings = button.setdefault('bindings', [])
                    bindings[:] = [binding for binding in bindings if binding[0] != 'input']
    rows[:] = [row for row in rows if row.get('name') not in (*bot_names, 'LocalRoomRules')]
    heading = {'class': 'UIStaticText', 'name': 'LocalSoloBotHeading', 'input': False,
               'classes': 'header4 text-color-white',
               'bindings': [['visible', 'localBotSetup']],
               'components': {
                   'SizePolicy': {'horizontalPolicy': 'PercentOfParent', 'verticalPolicy': 'FixedSize',
                                  'verticalValue': 50.},
                   'UITextComponent': {'text': 'Local/BotsTitle', 'align': ['HCENTER', 'VCENTER']},
               }}
    map_row, = [row for row in rows if row.get('name') == 'Map']
    index = rows.index(map_row) + 1
    # Both paths expose the common controls in the same order.
    rows[index:index] = [heading, *bot_controls, *bots, difficulty, room_rules,
                         duration, lives, ammo, teams, options]
    return True


def _order_settings_rows(root):
    """Keep every shared tile in the same place in solo and room forms."""
    container, = [node for node in _walk(root) if node.get('name') == 'scrollContainerControl']
    rows = container['children']
    names = (
        # Room-only metadata stays above the shared settings block and remains
        # hidden in the ordinary bot-battle form.
        'Title', 'DescriptionSelect', 'Description',
        'SolSetupPresetRow', 'SolSetupLineupRow', 'SolRoomLineupRow', 'Map',
        'LocalSoloBotHeading', 'LocalAllies', 'LocalRoomBots1', 'LocalEnemies',
        'LocalRoomBots2', 'LocalDifficulty', 'LocalRoomDifficulty', 'LocalRules',
        'LocalRoomRules', 'LocalRoomDuration', 'LocalRoomLives', 'LocalRoomAmmo',
        'LocalRoomTeams', 'LocalRoomOptions',
    )
    by_name = {row.get('name'): row for row in rows}
    ordered = [by_name[name] for name in names if name in by_name]
    claimed = {id(row) for row in ordered}
    rows[:] = [*ordered, *(row for row in rows if id(row) not in claimed)]


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
        ['int', 'localRules', '1'], ['int', 'localLargeTeams', '0']]}
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
    allies = _counter_row('LocalAllies', 'Local/BattleSettings/Allies', 'localAllies',
                          'LocalAllyLess', 'LocalAllyMore')
    enemies = _counter_row('LocalEnemies', 'Local/BattleSettings/Enemies', 'localEnemies',
                           'LocalEnemyLess', 'LocalEnemyMore')
    difficulty = _difficulty_row('LocalDifficulty', 'localDifficulty', 'LocalDifficultyNext')
    rules = _row('LocalRules', 'Local/BattleSettings/Rules', [
        _button('Choice', RULES_CAPTION, 'LocalOpenModePicker', 360)])
    container['children'] = [allies, enemies, difficulty, rules, *container['children']]
    _room_rules(root)
    _room_duration_settings(root)
    _mode_picker(root)
    client_battle_setup.patch_form(document, root)
    _order_settings_rows(root)


MODE_KEYS = ('Elimination', 'Encounter', 'Supremacy', 'Gravity', 'BigBoss', 'MadGames', 'Uprising')
MODE_ART = ('regular', 'regular', 'regular', 'moongame', 'bossmode', 'madgames', 'hellgames')


def _mode_picker(root):
    """Show a preview over the same form while preserving its draft settings."""
    local_vars = root['components']['UIDataLocalVarsComponent']['data']
    local_vars[:] = [row for row in local_vars if row[1] not in ('modePickerOpen', 'modePreview')]
    local_vars.extend([['bool', 'modePickerOpen', 'false'], ['int', 'modePreview', '0']])
    root['children'][:] = [node for node in root['children'] if node.get('name') != 'LocalModePicker']
    for row in _walk(root):
        if row.get('name') in ('LocalRules', 'LocalRoomRules'):
            choice, = (node for node in _walk(row) if node.get('name') == 'Choice')
            choice['components']['UIInputEventComponent']['onTouchUpInside'] = 'LocalOpenModePicker'
    for child in root['children']:
        visible = next((binding for binding in child.get('bindings', []) if binding[0] == 'visible'), None)
        if visible is None:
            child.setdefault('bindings', []).append(['visible', 'not modePickerOpen'])
        elif 'modePickerOpen' not in visible[1]:
            visible[1] = f'({visible[1]}) and not modePickerOpen'

    def label(name, key, style, top, height):
        return _boss_label(name, key, style, top, height=height)

    def place(control, x, y):
        control['position'] = [float(x), float(y)]
        return control

    def artwork(art, x, y, width, height):
        return {
            'class': 'UIControl', 'name': 'Artwork', 'input': False,
            'position': [x, y], 'size': [width, height],
            'components': {'UITextureComponent': {
                'spritePath': f'~res:/Gfx/UI/Hangar/GameTypes/battle-type_{art}',
                'drawType': 'DRAW_SCALE_TO_RECT'}},
        }

    panels = []
    neighbors = []
    tabs = []
    tab_gap = 6
    tab_width = min(150, (820 - tab_gap * (len(MODE_KEYS)-1)) // len(MODE_KEYS))
    tabs_left = (840 - len(MODE_KEYS)*tab_width - (len(MODE_KEYS)-1)*tab_gap) / 2
    for index, (key, art) in enumerate(zip(MODE_KEYS, MODE_ART)):
        title = label('Title', key, 'header3 text-color-white', 286., 42.)
        if key == 'Encounter':
            title['bindings'][0][1] = ('when localBotSetup -> localize("Local/BattleSettings/Capture"), '
                                       + title['bindings'][0][1])
        description = label('ModeSummary', f'ModeDescription{key}', 'text2-small text-color-white', 328., 40.)
        panels.append({
            'class': 'UIControl', 'name': f'ModeCard{index}', 'input': True,
            'position': [277., 67.], 'size': [286., 374.],
            'bindings': [['visible', f'modePreview == {index}']],
            'components': {
                'Background': {'drawType': 'DRAW_FILL', 'color': [.08, .11, .13, 1.]},
                'UIInputEventComponent': {'onTouchUpInside': 'LocalApplyModePicker'},
                'Sound': {'touchUpInside': 'GUI/buttons/choose'},
            },
            'children': [
                artwork(art, 18., 6., 250., 328.),
                {'class': 'UIControl', 'name': 'CaptionShade', 'input': False,
                 'position': [0., 278.], 'size': [286., 96.],
                 'components': {'Background': {'drawType': 'DRAW_FILL', 'color': [.04, .055, .07, .95]}}},
                title, description,
            ],
        })
        for offset, side, x in ((-1, 'Previous', 63.), (1, 'Next', 621.)):
            neighbor_title = label('Title', key, 'header3 text-color-white', 204., 29.)
            neighbor_title['bindings'][0][1] = title['bindings'][0][1]
            neighbors.append({
                'class': 'UIControl', 'name': f'{side}Preview{index}', 'input': True,
                'position': [x, 113.], 'size': [156., 233.],
                'bindings': [['visible', f'modePreview == {(index - offset) % len(MODE_KEYS)}']],
                'components': {'UIInputEventComponent': {'onTouchUpInside': f'Local{side}Mode'}},
                'children': [
                    artwork(art, 0., 0., 156., 205.),
                    {'class': 'UIControl', 'name': 'Shade', 'input': False,
                     'size': [156., 233.], 'components': {
                         'Background': {'drawType': 'DRAW_FILL', 'color': [.035, .05, .065, .65]}}},
                    neighbor_title,
                ],
            })
        tab_title = ('localize("Local/BattleSettings/ModeTabMadGames")'
                     if key == 'MadGames' else title['bindings'][0][1])
        tab = _button(f'ModeTab{index}', tab_title, f'LocalPreviewMode{index}', tab_width)
        tab['children'][0]['components'] = {'UITextComponent': {'fitting': 'FITTING_REDUCE'}}
        tab['bindings'] = [['classes', f'when modePreview == {index} -> '
                            '"button button-color-orange", "button button-color-gray"']]
        tabs.append(place(tab, tabs_left + (tab_width + tab_gap) * index, 461))

    root['children'].append({
        'class': 'UIControl', 'name': 'LocalModePicker', 'input': False,
        'bindings': [['visible', 'modePickerOpen']],
        'components': {
            'IgnoreLayout': {},
            'Anchor': {f'{edge}AnchorEnabled': True for edge in ('left', 'right', 'top', 'bottom')},
            'Background': {'drawType': 'DRAW_FILL', 'color': [.045, .06, .07, 1.]},
            'UIShortcutEventComponent': {'shortcuts':
                'LocalCloseModePicker, Escape; LocalApplyModePicker, Enter; '
                'LocalPreviousMode, Left; LocalNextMode, Right', 'escape': 'LocalCloseModePicker'},
        },
        'children': [{
            'class': 'UIControl', 'name': 'Carousel', 'input': False, 'size': [840., 614.],
            'components': {
                'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True},
                'Background': {'drawType': 'DRAW_FILL', 'color': [.09, .12, .14, 1.]},
            },
            'children': [
                label('Heading', 'Rules', 'header2 text-color-white', 12., 48.), *neighbors, *panels,
                place(_button('Previous', '"<"', 'LocalPreviousMode', 48), 228, 204),
                place(_button('Next', '">"', 'LocalNextMode', 48), 564, 204),
                *tabs,
                place(_button('Cancel', 'localize("Local/BattleSettings/ModeCancel")',
                              'LocalCloseModePicker', 190), 221, 539),
                place(_button('Select', 'localize("Local/BattleSettings/ModeSelect")',
                              'LocalApplyModePicker', 190), 429, 539),
            ],
        }],
    })


def standalone_screen(source):
    import yaml

    document = yaml.safe_load(dvpl.read(source))
    screen, = (control for control in document['Controls'] if control['name'] == 'Screen')
    screen['children'].append({
        'path': 'Header/menuButton',
        'input': True,
        'components': {'UIInputEventComponent': {'onTouchUpInside': 'BACK_EVENT'}},
    })
    return dvpl.encode(yaml.safe_dump(document, sort_keys=False).encode())


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
    ChangeData(localLargeTeams, int(localFlags / 256) % 2);
    if (int(localFlags / 256) % 2 == 1)
    {
        ChangeData(localAllies, when localValue % 100 == 76 -> 14, localValue % 100);
        ChangeData(localEnemies, @LARGE_BOT_TOTAL@ - localAllies);
    }
    ChangeData(roomLivesOverride, localFlags % 2);
    ChangeData(roomAmmoOverride, int(localFlags / 2) % 2);
    ChangeData(roomMinutesOverride, int((localFlags % 256) / 4) - 1);
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
            + "/" + str(@ROOM_LIVES@) + "/" + str(@ROOM_MINUTES@) + "/" + str(@ROOM_AMMO@)
            + "/" + str(localLargeTeams));
    }
}
action LocalDifficultyNext()
{
    if (localDifficulty == 2) { ChangeData(localDifficulty, 0); }
    else { ChangeData(localDifficulty, localDifficulty + 1); }
    DirectEvent("LocalSync", ".");
}
action LocalOpenModePicker()
{
    ChangeData(modePreview, when localBotSetup -> localRules, @ROOM_MODE@);
    ChangeData(modePickerOpen, true);
}
action LocalCloseModePicker() { ChangeData(modePickerOpen, false); }
action LocalPreviousMode() { ChangeData(modePreview, (modePreview + @LAST_MODE@) % @MODE_COUNT@); }
action LocalNextMode() { ChangeData(modePreview, (modePreview + 1) % @MODE_COUNT@); }
action LocalApplyModePicker()
{
    if (localBotSetup) { ChangeData(localRules, modePreview); DirectEvent("LocalSync", "."); }
    else { ChangeData(roomModeOverride, modePreview); }
    ChangeData(modePickerOpen, false);
}
action LocalNoop() {}
action LocalRoomSingleLife() { ChangeData(roomLivesOverride, 0); DirectEvent("LocalSync", "."); }
action LocalRoomUnlimitedLives() { ChangeData(roomLivesOverride, 1); DirectEvent("LocalSync", "."); }
action LocalRoomToggleAmmo() { ChangeData(roomAmmoOverride, 1 - @ROOM_AMMO@); DirectEvent("LocalSync", "."); }
action LocalRoomDifficultyNext()
{
    ChangeData(roomDifficultyOverride, when @ROOM_DIFFICULTY@ == 2 -> 0, @ROOM_DIFFICULTY@ + 1);
}
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
action LocalRoomToggleTeams()
{
    if (localBotSetup)
    {
        if (localLargeTeams == 1)
        {
            ChangeData(localLargeTeams, 0);
            ChangeData(localAllies, 6);
            ChangeData(localEnemies, 7);
        }
        else
        {
            ChangeData(localLargeTeams, 1);
            ChangeData(localAllies, 14);
            ChangeData(localEnemies, 15);
        }
        DirectEvent("LocalSync", ".");
    }
    else { ChangeData(roomTeamOverride, 1 - @ROOM_TEAM@); }
}
'''
    actions = actions.replace('@TITLE@', TITLE).replace('@PREFIX@', PREFIX)
    actions = actions.replace('@ROOM_MODE@', ROOM_MODE_VALUE).replace('@ROOM_MINUTES@', ROOM_MINUTES_VALUE)
    actions = actions.replace('@ROOM_AMMO@', ROOM_AMMO_VALUE).replace('@ROOM_LIVES@', ROOM_LIVES_VALUE)
    actions = actions.replace('@ROOM_DIFFICULTY@', ROOM_DIFFICULTY_VALUE)
    actions = actions.replace('@ROOM_TEAM@', ROOM_TEAM_VALUE)
    actions = actions.replace('@LAST_MODE@', str(len(MODE_KEYS)-1)).replace('@MODE_COUNT@', str(len(MODE_KEYS)))
    actions = actions.replace('@RULES_BASE@', str(1 << RULES_SHIFT))
    actions = actions.replace('@LARGE_BOT_TOTAL@', str(LARGE_TEAM_SIZE))
    for name, delta, standard in (
        ('AllyLess', -1, 'ChangeData(localAllies, max(0, localAllies - 1));'),
        ('AllyMore', 1, 'ChangeData(localAllies, min(6, localAllies + 1));'),
        ('EnemyLess', 1, 'ChangeData(localEnemies, max(1, localEnemies - 1));'),
        ('EnemyMore', -1, 'ChangeData(localEnemies, min(7, localEnemies + 1));'),
    ):
        actions += f"""
action Local{name}()
{{
    if (localLargeTeams == 1)
    {{
        ChangeData(localAllies, min({LARGE_TEAM_SIZE - 1}, max(0, localAllies + ({delta}))));
        ChangeData(localEnemies, {LARGE_TEAM_SIZE} - localAllies);
    }}
    else {{ {standard} }}
    DirectEvent("LocalSync", ".");
}}
"""
    for team, value in ROOM_BOT_VALUES.items():
        actions += f"""
action LocalRoomBots{team}Less() {{ ChangeData(roomBots{team}Override, max(0, {value} - 1)); }}
action LocalRoomBots{team}More() {{ ChangeData(roomBots{team}Override, min({ROOM_BOT_LIMIT}, {value} + 1)); }}
"""
    actions += ''.join(f'action LocalPreviewMode{i}() {{ ChangeData(modePreview, {i}); }}\n'
                       for i in range(len(MODE_KEYS)))
    boss_actions = ''.join(f'action SolPick{i}() {{ ChangeData(solRole, solRole + 10 - solRole % 10 + {i}); }}\n' for i in range(1, 6))
    boss_actions += ''.join(f'action SolLevel{i}() {{ ChangeData(solRoleLevel, {i}); }}\n'
                            for i in range(1, big_boss.ROLE_LEVEL_COUNT + 1))
    boss_actions += 'action SolRespawn() { ChangeData(solRespawn, solRespawn + 1); }\n'
    boss_actions += 'action SolChangeTank() { ChangeData(solChangeTank, solChangeTank + 1); }\n'
    boss_actions += client_practice_picker.actions()
    return {ACTIONS: dvpl.encode(actions.replace('@MAX_ROOM_MINUTES@', str(MAX_ROOM_MINUTES)).encode()),
            BOSS_ACTIONS: dvpl.encode(boss_actions.encode())}


STRINGS = {
    'en': {'Settings': 'Battle settings', 'Kind': 'Battle type', 'Room': 'Training room', 'Allies': 'Allied bots',
           'Enemies': 'Enemy bots', 'Difficulty': 'Bot difficulty', 'Rules': 'Battle rules',
           'Easy': 'Easy', 'Normal': 'Normal', 'Hard': 'Hard', 'Capture': 'Base capture',
           'Elimination': 'Elimination', 'Encounter': 'Encounter',
           'Supremacy': 'Supremacy', 'Gravity': 'Gravity Force', 'BigBoss': 'Big Boss', 'MadGames': 'Mad Games',
           'Uprising': 'Uprising',
           'Save': 'Save', 'Saved': 'Settings saved', 'Create': 'Create',
           'Lives': 'Lives per tank', 'Duration': 'Battle duration', 'Minutes': 'min', 'ModeDefault': 'Mode default',
           'SingleLife': 'One life', 'Endless': 'Unlimited', 'NoTimer': 'No limit',
           'UnlimitedAmmo': 'Unlimited ammo', 'AmmoOn': 'On', 'AmmoOff': 'Off',
           'TeamSize': 'Battle size', 'StandardTeams': 'Standard', 'ThirtyTanks': '30 tanks',
           'RoomBotsCaption': 'Description',
           'RoomBotsHint': 'Optional', 'RoomBots1': 'Team 1 bots', 'RoomBots2': 'Team 2 bots',
           'RoomHeaderBots': 'Bots', 'RoomHeaderTime': 'Time', 'RoomHeaderDifficulty': 'Difficulty'},
    'ru': {'Settings': 'Настройки боя', 'Kind': 'Тип боя', 'Room': 'Тренировочная комната', 'Allies': 'Союзные боты',
           'Enemies': 'Боты противника', 'Difficulty': 'Сложность ботов', 'Rules': 'Правила боя',
           'Easy': 'Лёгкая', 'Normal': 'Обычная', 'Hard': 'Сложная', 'Capture': 'Захват базы',
           'Elimination': 'Перестрелка', 'Encounter': 'Встречный бой',
           'Supremacy': 'Превосходство', 'Gravity': 'Гравитация', 'BigBoss': 'Большой босс', 'MadGames': 'Безумные игры',
           'Uprising': 'Возрождение',
           'Save': 'Сохранить', 'Saved': 'Настройки сохранены', 'Create': 'Создать',
           'Lives': 'Жизни на танк', 'Duration': 'Время боя', 'Minutes': 'мин', 'ModeDefault': 'По режиму',
           'SingleLife': 'Одна жизнь', 'Endless': 'Без ограничений', 'NoTimer': 'Без лимита',
           'UnlimitedAmmo': 'Бесконечные снаряды', 'AmmoOn': 'Вкл.', 'AmmoOff': 'Выкл.',
           'TeamSize': 'Размер боя', 'StandardTeams': 'Обычный', 'ThirtyTanks': '30 танков',
           'RoomBotsCaption': 'Описание',
           'RoomBotsHint': 'Необязательно', 'RoomBots1': 'Боты команды 1', 'RoomBots2': 'Боты команды 2',
           'RoomHeaderBots': 'Боты', 'RoomHeaderTime': 'Время', 'RoomHeaderDifficulty': 'Сложность'}}


STRINGS['en'].update(PolygonSettings='Polygon settings', ModeCancel='Cancel', ModeSelect='Select', ModeTabMadGames='Mad Games',
                     ModeDescriptionElimination='Destroy the enemy team.',
                     ModeDescriptionEncounter='Capture the base or destroy the enemy team.',
                     ModeDescriptionSupremacy='Capture control points to earn team points.',
                     ModeDescriptionGravity='Low gravity and a jump ability.',
                     ModeDescriptionBigBoss='Five roles with special abilities and respawns.',
                     ModeDescriptionMadGames='Speed boosts, dashes and reinforced ramming.',
                     ModeDescriptionUprising='Three lives and abilities that grow stronger between respawns.')
STRINGS['ru'].update(PolygonSettings='Настройки Полигона', ModeCancel='Отмена', ModeSelect='Выбрать', ModeTabMadGames='Безумие',
                     ModeDescriptionElimination='Уничтожь команду противника.',
                     ModeDescriptionEncounter='Захвати базу или уничтожь противников.',
                     ModeDescriptionSupremacy='Захватывай точки и набирай очки команды.',
                     ModeDescriptionGravity='Низкая гравитация и прыжок танка.',
                     ModeDescriptionBigBoss='Пять ролей со способностями и возрождениями.',
                     ModeDescriptionMadGames='Ускорение, рывки и усиленный таран.',
                     ModeDescriptionUprising='Три жизни и выбор усилений перед возрождением.')


for locale, respawn, title in (('en', 'RESPAWN', 'Choose a role'), ('ru', 'РЕСПАВН', 'Выбери роль')):
    strings = STRINGS[locale]
    strings['ChangeTank'] = 'СМЕНИТЬ ТАНК' if locale == 'ru' else 'CHANGE TANK'
    strings['ReturnToBattle'] = 'ВЕРНУТЬСЯ В БОЙ' if locale == 'ru' else 'RETURN TO BATTLE'
    strings.update(Respawn=respawn, BossRoleTitle=title,
                   BossRoleLevel='Role level' if locale == 'en' else 'Уровень роли')
    for index, (_, en_name, ru_name, en_ability, ru_ability, stats) in enumerate(BOSS_CARDS, 1):
        prefix = f'BossRole{index}'
        strings[prefix] = f'{index}  {en_name if locale == "en" else ru_name}'
        strings[prefix + 'Ability'] = en_ability if locale == 'en' else ru_ability
        for row, (en_label, ru_label, value, *units) in enumerate(stats, 1):
            unit = ' ' + units[locale == 'ru'] if units else ''
            strings[f'{prefix}Label{row}'] = en_label if locale == 'en' else ru_label
            key = f'{prefix}Value{row}'
            if isinstance(value, tuple):
                for level, amount in enumerate(value, 1):
                    strings[f'{key}L{level}'] = amount + unit
            else:
                strings[key] = value + unit

for translations in STRINGS.values():
    for key, value in translations.items():
        if (key not in ('Saved', 'Minutes', 'RoomBotsHint')
                and not key.startswith('ModeDescription')
                and not (key.startswith('BossRole') and 'Value' in key)):
            translations[key] = value.upper()


def _boss_actions(root, *names):
    root['components']['UIActionComponent'] = {'actionsPath': '~res:/'+BOSS_ACTIONS.removesuffix('.dvpl')}
    root['components']['UIDataLocalVarsComponent'] = {'data': [['int', name, '0'] for name in names]}


def _boss_value(name, expression):
    return dict(name=name, **{'class': 'UITextField'}, visible=False, input=False,
                bindings=[['text', f'str({expression})']])


def _boss_label(name, key, style, top, *, align='HCENTER', left=7., right=7., height=22.):
    return {
        'class': 'UIControl', 'name': name, 'input': False, 'size': [140., height],
        'classes': style,
        'components': {
            'Anchor': {'leftAnchorEnabled': True, 'leftAnchor': left,
                       'rightAnchorEnabled': True, 'rightAnchor': right,
                       'topAnchorEnabled': True, 'topAnchor': top},
            'UITextComponent': {'align': [align, 'VCENTER'], 'fitting': 'FITTING_REDUCE',
                                'color': [1., 1., 1., 1.], 'colorInheritType': 'COLOR_IGNORE_PARENT'},
            'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'},
        },
        'bindings': [['UITextComponent.text', f'localize("Local/BattleSettings/{key}")']],
    }


def patch_boss_screen(document):
    root, = document['Prototypes']
    _boss_actions(root, 'solRole')
    root['components']['UIDataLocalVarsComponent']['data'].append(['int', 'solRoleLevel', '1'])
    # The client resolves the original prototypes from its own installed resources.
    document.setdefault('ImportedPackages', []).extend((
        '~res:/UI/Screens/Battle/OptionalHUDControls/BossmodeAbility.yaml',
        '~res:/UI/Screens/Common/StyledButton.yaml',
    ))
    centered = {'hCenterAnchorEnabled': True}
    edges = {f'{edge}AnchorEnabled': True for edge in ('left', 'right', 'top', 'bottom')}
    detail = 'AbilityButton/Content/AbilityDetailContainer'
    cards = []
    for index, (icon, *_, stats) in enumerate(BOSS_CARDS, 1):
        prefix = f'BossRole{index}'
        rows = []
        for row, (_, _, value, *_) in enumerate(stats, 1):
            key = f'"Local/BattleSettings/{prefix}Value{row}"'
            if isinstance(value, tuple):
                key += ' + "L" + str(solRoleLevel)'
            rows.append({
                'prototype': 'BossmodeAbility/AbilityParam', 'name': f'Stat{row}',
                'size': [170., 32.],
                'components': {
                    'LinearLayout': {'enabled': False},
                    'SizePolicy': {'horizontalPolicy': 'PercentOfParent', 'horizontalValue': 100.,
                                   'verticalPolicy': 'FixedSize', 'verticalValue': 32.},
                },
                'children': [
                    {'path': 'Descr', 'classes': 'text2-small text-color-white-70', 'size': [112., 32.], 'components': {
                        'Anchor': {'leftAnchorEnabled': True, 'leftAnchor': 12.,
                                   'rightAnchorEnabled': True, 'rightAnchor': 47.,
                                   'topAnchorEnabled': True, 'topAnchor': 0.},
                        'SizePolicy': {'horizontalPolicy': 'Ignore', 'verticalPolicy': 'Ignore'},
                        'UITextComponent': {'align': ['LEFT', 'VCENTER'],
                                            'multiline': 'MULTILINE_DISABLED', 'fitting': 'FITTING_REDUCE'}},
                     'bindings': [['UITextComponent.text',
                                   f'localize("Local/BattleSettings/{prefix}Label{row}")']]},
                    {'path': 'Value', 'size': [38., 32.], 'components': {
                        'Anchor': {'rightAnchorEnabled': True, 'rightAnchor': 12.,
                                   'topAnchorEnabled': True, 'topAnchor': 0.},
                        'SizePolicy': {'horizontalPolicy': 'Ignore', 'verticalPolicy': 'Ignore'},
                        'UITextComponent': {'align': ['RIGHT', 'VCENTER']}},
                     'bindings': [['UITextComponent.text',
                                   f'localize({key})']]},
                ],
            })
        ability = _boss_label('Ability', prefix + 'Ability', 'text3-small text-color-white', 100.)
        ability['components']['IgnoreLayout'] = {}
        cards.append({
            'prototype': 'BossmodeAbility/Ability', 'name': f'Role{index}', 'size': [170., 226.],
            'components': {
                'SizePolicy': {'horizontalPolicy': 'Formula', 'horizontalFormula': '(parent - 32) / 5',
                               'verticalPolicy': 'FixedSize', 'verticalValue': 226.},
            },
            'children': [
                {'path': 'AbilityButton', 'input': True, 'classes': 'button-choose-sound',
                 'components': {'SizePolicy': {'horizontalPolicy': 'PercentOfParent',
                                               'verticalPolicy': 'PercentOfParent'},
                                'UIInputEventComponent': {'onTouchUpInside': f'SolPick{index}'}}},
                {'path': 'AbilityButton/Content', 'components': {
                    'LinearLayout': {'padding': '0', 'spacing': 0.},
                    'SizePolicy': {'horizontalPolicy': 'PercentOfParent', 'verticalPolicy': 'PercentOfParent'}},
                 'children': [ability]},
                {'path': 'AbilityButton/Content/Fade', 'components': {
                    'SizePolicy': {'horizontalPolicy': 'PercentOfParent', 'verticalValue': 146.}}},
                {'path': 'AbilityButton/Content/Background', 'components': {
                    'Anchor': {'topAnchor': 48.},
                    'SizePolicy': {'verticalPolicy': 'Formula', 'verticalFormula': 'parent - 48'}}},
                {'path': detail, 'classes': '', 'components': {
                    'LinearLayout': {'orientation': 'TopDown', 'spacing': 0.},
                    'SizePolicy': {'horizontalPolicy': 'PercentOfParent', 'verticalPolicy': 'FixedSize',
                                   'verticalValue': 123.}}},
                {'path': detail + '/icon', 'components': {
                    'Background': {'drawType': 'DRAW_SCALE_PROPORTIONAL_ONE',
                                   'sprite': f'~res:/Gfx/Shared/HUD/bossmode/icon_role-{icon}_s'},
                    'SizePolicy': {'horizontalValue': 70., 'verticalValue': 70.}}},
                {'path': detail + '/Spacer', 'components': {
                    'SizePolicy': {'verticalValue': 4.}}},
                {'path': detail + '/BossmodeAbilityType', 'visible': False,
                 'components': {'IgnoreLayout': {}}},
                {'path': detail + '/AbilityName', 'size': [156., 24.], 'components': {
                    'SizePolicy': {'horizontalPolicy': 'PercentOfParent', 'horizontalValue': 94.,
                                   'verticalPolicy': 'FixedSize', 'verticalValue': 24.},
                    'UITextComponent': {'align': ['HCENTER', 'VCENTER'], 'fitting': 'FITTING_REDUCE'}},
                 'bindings': [['UITextComponent.text', f'localize("Local/BattleSettings/{prefix}")']]},
                {'path': detail + '/AbilityDescription', 'visible': False,
                 'components': {'IgnoreLayout': {}}},
                {'path': 'AbilityButton/Content/AbilityParamsContainer', 'children': rows},
            ],
        })
    levels = []
    for level in range(1, big_boss.ROLE_LEVEL_COUNT + 1):
        button = _button(f'Level{level}', f'"{level}"', f'SolLevel{level}', 42)
        button['components']['SizePolicy']['verticalValue'] = 30.
        button['bindings'] = [['classes', f'when solRoleLevel == {level} -> '
                               '"button button-color-orange", "button button-color-gray"']]
        levels.append(button)
    panel = {
        'class': 'UIControl', 'name': 'RoleSelection', 'input': False, 'size': [882., 325.],
        'components': {
            'Anchor': dict(centered, vCenterAnchorEnabled=True, vCenterAnchor=59.),
            'SizePolicy': {'horizontalPolicy': 'Formula', 'horizontalFormula': 'min(parent - 24, 882)'},
        },
        'children': [
            _boss_label('Title', 'BossRoleTitle', 'header0 text-color-white', 0., height=30.),
            _boss_label('LevelTitle', 'BossRoleLevel', 'text2-small text-color-white', 31.),
            {'class': 'UIControl', 'name': 'Levels', 'input': False,
             'components': {
                 'Anchor': dict(centered, topAnchorEnabled=True, topAnchor=54.),
                 'LinearLayout': {'spacing': 5.},
                 'SizePolicy': {'horizontalPolicy': 'PercentOfChildrenSum',
                                'verticalPolicy': 'PercentOfMaxChild'},
             },
             'children': levels},
            {'class': 'UIControl', 'name': 'Cards', 'input': False,
             'components': {
                 'Anchor': {'leftAnchorEnabled': True, 'rightAnchorEnabled': True,
                            'topAnchorEnabled': True, 'topAnchor': 96.},
                 'LinearLayout': {'spacing': 8.},
                 'SizePolicy': {'verticalPolicy': 'PercentOfMaxChild'},
             },
             'children': cards},
        ],
    }
    # Native code toggles this root, including the dimmer, as one unit.
    root['children'].insert(-1, {
        'class': 'UIControl', 'name': 'SolBossPicker', 'visible': False, 'input': False,
        'components': {'Anchor': edges, 'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'},
                       'Background': {'drawType': 'DRAW_FILL', 'color': [.025, .035, .045, .57]}},
        'children': [panel, _boss_value('SolRoleValue', 'solRole'),
                     _boss_value('SolRoleLevel', 'solRoleLevel')],
    })


def patch_boss_menu(document):
    from copy import deepcopy
    root, = document['Prototypes']
    _boss_actions(root, 'solRespawn', 'solChangeTank')
    buttons = next(node for node in _walk(root) if node.get('path') == 'Middle/VerticalBox')['children']
    exit_button = next(button for button in buttons if button.get('name') == 'exitButton')
    respawn = deepcopy(exit_button)
    respawn.update(name='SolRespawnButton', visible=False, classes='button button-color-blue')
    respawn['components']['UIInputEventComponent'] = {'onTouchUpInside': 'SolRespawn'}
    respawn['children'][0]['components']['UITextComponent']['text'] = 'Local/BattleSettings/Respawn'
    buttons.insert(buttons.index(exit_button) + 1, respawn)
    root['children'].append(_boss_value('SolRespawnValue', 'solRespawn'))
    change_tank = deepcopy(respawn)
    change_tank.update(name='SolChangeTankButton', classes='button button-color-gray')
    change_tank['components']['UIInputEventComponent'] = {'onTouchUpInside': 'SolChangeTank'}
    change_tank['children'][0]['components']['UITextComponent']['text'] = 'Local/BattleSettings/ChangeTank'
    buttons.insert(buttons.index(respawn) + 1, change_tank)
    root['children'].append(_boss_value('SolChangeTankValue', 'solChangeTank'))
    client_practice_picker.patch_menu(document, root, buttons)


def _room_header(document):
    root, = [node for node in document['Prototypes'] if node.get('name') == 'ScreenContainer']
    header, = [node for node in _walk(root) if node.get('name') == 'HorizContainer2']
    mode, = [node for node in _walk(header) if node.get('name') == 'ModeTxt']
    mode['bindings'] = [['UITextComponent.text', ROOM_RULES_CAPTION.replace(
        ROOM_MODE_VALUE, '(when roomHeaderMode < 0 -> 1, roomHeaderMode)')]]
    return root, header


def patch_room(document):
    """Keep room metadata in the native map and mode row, outside the notes."""
    root, header = _room_header(document)
    params = root['components']['UIDataParamsComponent']['params']
    room_params = tuple((f'roomHeader{name}', default) for name, default in
                        (('Mode', -1), ('Minutes', -1), ('Bots1', 0), ('Bots2', 0),
                         ('Difficulty', 1)))
    names = {name for name, _ in room_params}
    params[:] = [row for row in params if row[1] not in names]
    params.extend(['int', name, str(default), str(default)] for name, default in room_params)
    header['children'][:] = [child for child in header['children']
                             if child.get('name') != 'RoomOptionsSummary']
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
                      'str(roomHeaderBots1) + ":" + str(roomHeaderBots2) + "  •  " + '
                      'localize("Local/BattleSettings/RoomHeaderDifficulty") + " " + '
                      '(when roomHeaderDifficulty == 0 -> localize("Local/BattleSettings/Easy"), '
                      'roomHeaderDifficulty == 2 -> localize("Local/BattleSettings/Hard"), '
                      'localize("Local/BattleSettings/Normal"))']]
    }
    header['children'].append(summary)

BATTLE_SETTINGS_UI_VERSION = 73


def upgrade_form(lab, manifest):
    """Refresh the installed native battle settings form in an idle client."""
    if manifest.get('battle_settings_ui_version') == BATTLE_SETTINGS_UI_VERSION:
        return manifest
    from hashlib import sha256
    import json
    import yaml
    from .client_ui import (BASE_LOBBY, BASE_LOBBY_ACTIONS, BATTLE_BUTTON, GAME_TYPES, HANGAR, HANGAR_ACTIONS, STRINGS_EN,
                            STRINGS_RU, _StringsLoader, patched_resource)
    from .connection import atomic_write
    from . import client_practice_picker

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
            description, = [node for node in _walk(root) if node.get('name') == 'Description'
                            and any(child.get('path') == 'Container/Caption' for child in node.get('children', []))]
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
                _mode_picker(root)
                client_battle_setup.patch_form(document, root)
                _order_settings_rows(root)
                changed = True
        else:
            document = yaml.load(raw, Loader=_StringsLoader)
            locale = 'en' if relative == STRINGS_EN else 'ru'
            strings = {f'Local/BattleSettings/{key}': value for key, value in STRINGS[locale].items()}
            strings['#training_rooms:CreationScreen/Header'] = STRINGS[locale]['Settings']
            locale_index = 0 if locale == 'en' else 1
            strings.update({f'Local/Sb{key}': values[locale_index]
                            for key, values in client_practice_picker.STRINGS.items()
                            if key.startswith(('Stats', 'Target', 'Tm'))})
            strings.update({f'Local/SbSetup{key}': values[locale_index]
                            for key, values in client_battle_setup.STRINGS.items()})
            changed = any(document.get(key) != value for key, value in strings.items())
            document.update(strings)
        content = (dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                                              width=1 << 30).encode()) if changed else original)
        if changed:
            staged[target] = content
        overrides[relative] = sha256(content).hexdigest()
    if has_room_form:
        room = data/ROOM
        if room.parent.resolve(strict=True) != room.parent:
            raise ValueError('Room header requires a private UI overlay')
        if ROOM in overrides:
            if room.resolve(strict=True) != room or not room.is_file():
                raise ValueError('Room header requires a private UI overlay')
            document = yaml.safe_load(dvpl.read(room))
            patch_room(document)
        else:
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
    source = manifest.get('source_game') or manifest.get('game')
    if source:
        from .client_ui import local_directory
        game = Path(source).resolve(strict=True)
        screen_source = game/'Data'/TRAINING_SCREEN
        if has_room_form and screen_source.is_file():
            target = data/SOLO_SCREEN
            staged[target] = standalone_screen(screen_source)
            overrides[SOLO_SCREEN] = sha256(staged[target]).hexdigest()
        for relative in (BASE_LOBBY, BASE_LOBBY_ACTIONS, HANGAR, HANGAR_ACTIONS, BATTLE_BUTTON):
            if relative in overrides:
                target = data/relative
                if target.resolve(strict=True) != target or not target.is_file():
                    raise ValueError('Training return requires a private hangar overlay')
                staged[target] = patched_resource(game/'Data'/relative, relative)
                overrides[relative] = sha256(staged[target]).hexdigest()
        if GAME_TYPES in overrides:
            target = data/GAME_TYPES
            executable = lab/'client/wotblitz.exe'
            if executable.is_file():
                with executable.open('rb') as handle:
                    handle.seek(NAVIGATION_OFFSET)
                    navigation = handle.read(len(NAVIGATION_BEFORE))
                if navigation in (NAVIGATION_AFTER.ljust(len(NAVIGATION_BEFORE), b'\x90'),
                                  STANDALONE_NAVIGATION):
                    if target.resolve(strict=True) != target or not target.is_file():
                        raise ValueError('Battle settings require a private game type picker')
                    staged[target] = patched_resource(game/'Data'/GAME_TYPES, GAME_TYPES)
                    overrides[GAME_TYPES] = sha256(staged[target]).hexdigest()
        menu_source = game/'Data'/BATTLE_MENU
        if menu_source.is_file():
            document = yaml.safe_load(dvpl.read(menu_source))
            patch_boss_menu(document)
            menu = data/BATTLE_MENU
            for parent in reversed(menu.parents):
                if parent.is_relative_to(data) and parent.is_symlink():
                    local_directory(parent, game)
            staged[menu] = dvpl.encode(yaml.safe_dump(
                document, sort_keys=False, allow_unicode=True).encode())
            staged[data/BOSS_ACTIONS] = extra_resources()[BOSS_ACTIONS]
            for relative in (BATTLE_MENU, BOSS_ACTIONS):
                overrides[relative] = sha256(staged[data/relative]).hexdigest()
    for target, content in staged.items():
        atomic_write(target, content, replace_symlink=target in (data/ROOM, data/BATTLE_MENU))
    updated = dict(manifest, ui_overrides=overrides, room_bot_hint_version=1,
                   battle_settings_ui_version=BATTLE_SETTINGS_UI_VERSION)
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
# Native battle_settings_client.S with direct_hangar=1. Only the solo save
# branch changes. Keep the load and room paths at their original addresses.
DIRECT_HANGAR_EXTENSION = (
    SHARED_RULES_EXTENSION[:0x114]
    + bytes.fromhex("e8dc00000090909090909090909090")
    + SHARED_RULES_EXTENSION[0x123:]
    + bytes.fromhex("89f1e8d409e2fb8d8e48010000e8896a37fd89f1e9b204e2fb")
)
# The native form is hosted directly on the lobby stack. Rebuild with
# direct_hangar=1 and standalone_settings=1.
STANDALONE_SETTINGS_EXTENSION = DIRECT_HANGAR_EXTENSION + bytes.fromhex(
    "5589e583ec20535657e8000000005b81eb1ce0710489cee8a609e2fb31c08945fc8945f88945f48945f08945ec8945e88b86"
    "1001000085c00f84c40100008d9366e371045250e85802000085c00f84af0100008b403885c00f84a40100008945e08d8350"
    "e3710450683d0000008d837de371045056e8580200008945fc85c00f847d0100008d8357e3710450ff75fce80f02000085c0"
    "0f84660100008b962001000052508d45f450e83562dffc83c40c837df4000f84480100008d835ee3710450ff75fce8da0100"
    "0085c00f84310100008945e48d45e4508d45f850e8e16075fc83c408837df8000f84140100008d83bbe3710450682d000000"
    "8d83c7e371045056e8cb0100008945f085c00f84f00000008b862001000083c014508d45ec50e86dff34fd83c408837dec00"
    "0f84d0000000682c020000e83485abfd83c40485c00f84bb00000089c16a008d45f0508d45f4508d862001000050ff75f8ff"
    "75ecc745ec00000000e8b68435fd8945e88b55f88990b80100008b55fc8990bc0100008b55e08990c0010000c745f8000000"
    "00c745fc00000000568b3083ee048db8c4010000b91a000000f3a55e8d9360e271048990c80100008d90c801000089108b4d"
    "e0e8fa07e2fb8b4de06a00e8406e1efc8b45e8ffb0bc0100008b8e100100008b11ff522c8b45e889c1e8d207e2fbff75e889"
    "f18b06ff50448b4de8e8f0fbffff8b4de8e8340000008b4dece8350000008b4df0e8240000008b4df4e81c0000008b4df8e8"
    "1d0000008b4dfce80c00000089f1e87902e2fb5f5e5bc9c385c90f856c02e2fbc385c974068b016a01ff10c35356578bb1b8"
    "0100008bb9bc0100008b99c0010000ff742410e8f2ff35fd5089f1e8cfffffff8b4f3885c97406578b01ff503089f9e82602"
    "e2fb89d96a01e87d6d1efc89d9e81602e2fb585f5e5bc204005589e583ec1857ff750c8d4de8e80dc5b7fb8d45e8508b4d08"
    "e8b1011cfc89c78d4de8e847c4b7fb89f85fc9c208005589e583ec3857ff7510ff750c8d4dc8e86bcce7fb8d45c8508d45fc"
    "508b4d08e80bb2c3fb8d4dc8e8a3ccb8fb31ff837dfc007434ff75148d4de4e8b0c4b7fb8d45e4508b4dfce864511cfc89c7"
    "85ff740789f9e89706e2fb8d4de4e8dfc3b7fb8b4dfce87701e2fb89f85fc9c2100053637265656e0048656164657200436f"
    "6e74656e74002a2a2f47616d6554797065735363726f6c6c56696577007e7265733a2f55492f53637265656e732f4c6f6262"
    "792f547261696e696e672f536f6c627265616b6572426174746c6553657474696e67732e79616d6c00486561646572526967"
    "6874007e7265733a2f55492f53637265656e732f4c6f6262792f547261696e696e672f526f6f6d4c6973742e79616d6c00"
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
