"""Native controls for editing the per-account bot lineup and presets."""
from . import client_practice_picker as picker
from .sandbox import TANK_STAT_NAMES

STRINGS = {
    'Lineup': ('TEAM LINEUP', 'СОСТАВ КОМАНД'),
    'Presets': ('TANK PRESETS', 'ПРЕСЕТЫ ХАРАКТЕРИСТИК'),
    'Edit': ('Open', 'Открыть'),
    'Auto': ('Default matchmaking', 'Обычный подбор'),
    'Custom': ('Custom lineup', 'Ручной состав'),
    'Team1': ('Team 1', 'Команда 1'),
    'Team2': ('Team 2', 'Команда 2'),
    'Allies': ('Allies', 'Союзники'),
    'Enemies': ('Enemies', 'Противники'),
    'Own': ('Your tank', 'Твой танк'),
    'Automatic': ('Automatic', 'Автоматически'),
    'Random': ('Random tank', 'Случайный танк'),
    'Tank': ('Choose a tank', 'Выбор танка'),
    'Modules': ('Tank modules', 'Модули танка'),
    'UseTank': ('Use this tank', 'Выбрать танк'),
    'Specific': ('Specific tank and modules', 'Конкретный танк и модули'),
    'Empty': ('Empty', 'Пусто'),
    'AllRandom': ('Fill with random tanks', 'Заполнить случайными'),
    'Slot': ('Bot slot', 'Слот бота'),
    'TierAny': ('Any tier near yours', 'Уровень рядом с твоим'),
    'Class0': ('Any class', 'Любой класс'),
    'Class1': ('Heavy tank', 'Тяжёлый танк'),
    'Class2': ('Medium tank', 'Средний танк'),
    'Class3': ('Tank destroyer', 'ПТ-САУ'),
    'Class4': ('Light tank', 'Лёгкий танк'),
    'Back': ('Back', 'Назад'),
    'Find': ('Search', 'Найти'),
    'Activate': ('Use this preset', 'Применить пресет'),
    'Deactivate': ('Use stock parameters', 'Вернуть штатные параметры'),
    'PresetOn': ('Active for next battle', 'Активен на следующий бой'),
    'PresetOff': ('Inactive', 'Не активен'),
    'BotsOn': ('Matching bots: on', 'Боты этой модели: вкл.'),
    'BotsOff': ('Matching bots: off', 'Боты этой модели: выкл.'),
    'Reset': ('Reset selected preset', 'Сбросить выбранный пресет'),
    'SaveHint': ('Use Save in Battle settings to apply your changes.', 'Нажми «Сохранить» в настройках боя.'),
}


def _key(name):
    return 'Local/SbSetup'+name


def _marker(command):
    return {'class': 'UITextField', 'name': 'SolSetupAction', 'text': str(command),
            'input': False, 'visible': False, 'size': [1., 1.]}


def _attach(control, command):
    """Use the pinned slider event path for local commands without a room RPC."""
    control.get('components', {}).pop('UIInputEventComponent', None)
    control['input'] = False
    control.setdefault('children', []).append({
        'prototype': 'Slider/Slider', 'name': 'Action', 'input': True,
        'position': [0., 0.], 'size': list(control['size']),
        'children': [{'path': 'Bg', 'visible': False},
                     {'path': 'thumbSpriteControl', 'visible': False}, _marker(command)],
    })
    return control


def _button(name, caption, x, y, width, command, *, height=40.):
    return _attach(picker._button(name, caption, x, y, width, 0, height=height), command)


def _lineup():
    column_width = 212.
    right = 224.
    controls = [
        _button('Mode', _key('Auto'), 0., 8., right+column_width, 100, height=34.),
        _button('Allies', _key('Allies'), 0., 49., column_width, 115, height=32.),
        _button('Enemies', _key('Enemies'), right, 49., column_width, 116, height=32.),
        picker._mark('AlliesSelected', 0., 83., column_width),
        picker._mark('EnemiesSelected', right, 83., column_width),
        picker._module_text('YourTank', _key('Own'), 10., 94., column_width-10., 34.),
    ]
    for index in range(14):
        position = 0 if index == 13 else index+1 if index < 6 else index-6
        controls.append(_button(f'Slot{index:02}', _key('Automatic'),
                                right*(position%2), 94.+40.*(position//2),
                                column_width, 120+index, height=34.))
    controls.extend((
        _button('FillAllies', _key('AllRandom'), 0., 260., column_width, 104, height=34.),
        _button('FillEnemies', _key('AllRandom'), 0., 260., column_width, 105, height=34.),
        _button('Close', _key('Back'), right, 260., column_width, 102, height=34.),
    ))
    return picker._sandbox_panel('SolSetupRoster', 'SetupLineup', 305., controls, width=476.)


def _slot():
    return picker._sandbox_panel('SolSetupSlot', 'SetupSlot', 338., [
        _button('Automatic', _key('Automatic'), 0., 10., 520., 108),
        _button('Specific', _key('Specific'), 0., 59., 520., 109),
        _button('Tier', _key('TierAny'), 0., 111., 253., 110),
        _button('Class', _key('Class0'), 267., 111., 253., 111),
        _button('Random', _key('Random'), 0., 158., 520., 112),
        _button('Empty', _key('Empty'), 0., 206., 520., 113),
        _button('Back', _key('Back'), 135., 270., 250., 114),
    ])


PRESET_GROUPS = {
    'WeaponGroup': ('Aim', 'Accuracy', 'Turret', 'Damage', 'Reload'),
    'MobilityGroup': ('Boost', 'Brake', 'Reverse', 'Grip', 'Hull'),
    'SurvivalGroup': ('Health',),
}


def _presets():
    controls = [_button(f'Preset{i}', str(i+1), 58.*i, 8., 50., 140+i)
                for i in range(4)]
    controls.append(picker._module_text('State', _key('PresetOff'), 244., 8., 276., 40., title=True))
    for group_index, (group, names) in enumerate(PRESET_GROUPS.items()):
        x = group_index*176.
        controls.extend((
            _button(group, 'Local/SbStats'+group, x, 54., 168., 150+group_index),
            picker._mark(f'StatsGroup{group_index}', x, 96., 168.),
        ))
        for row, name in enumerate(names):
            index = TANK_STAT_NAMES.index(name)
            y = 104.+43.*row
            value = picker._module_text(name+'Value', '0%', 462., y, 58., 19.)
            value['components']['UITextComponent']['align'] = ['RIGHT', 'VCENTER']
            controls.extend((value, picker._module_text(name+'Label', 'Local/SbStats'+name, 0., y, 452., 19.),
                             {'prototype': 'Slider/Slider', 'name': name, 'position': [0., y+19.],
                              'size': [520., 23.], 'input': True,
                              'children': [{'path': 'thumbSpriteControl', 'size': [22., 22.]},
                                           _marker(180+index)]}))
    controls.extend((
        _button('Activate', _key('Activate'), 0., 328., 252., 145),
        _button('Bots', _key('BotsOff'), 268., 328., 252., 146),
        _button('Reset', _key('Reset'), 0., 373., 252., 147),
        _button('Close', _key('Back'), 268., 373., 252., 102),
    ))
    return picker._sandbox_panel('SolSetupPresets', 'SetupPresets', 425., controls)


def patch_form(document, root):
    packages = document['ImportedPackages']
    for name in ('Common.yaml', 'Common/Slider.yaml', 'Common/TextFieldPanel.yaml'):
        path = '~res:/UI/Screens/'+name
        if path not in packages:
            packages.append(path)
    from .client_battle_settings import _row, _walk
    container, = (node for node in _walk(root) if node.get('name') == 'scrollContainerControl')
    container['children'][:] = [node for node in container['children']
                                if node.get('name') not in ('SolSetupLineupRow', 'SolSetupPresetRow', 'SolRoomLineupRow')]
    for name, command, row_name in (
        ('Lineup', 101, 'SolSetupLineupRow'), ('Presets', 103, 'SolSetupPresetRow'),
        ('Lineup', 106, 'SolRoomLineupRow'),
    ):
        editor = _button('Open', _key('Edit'), 0., 0., 228., command)
        editor['components']['SizePolicy'] = {'horizontalPolicy': 'FixedSize', 'horizontalValue': 228.,
                                              'verticalPolicy': 'FixedSize', 'verticalValue': 40.}
        row = _row(row_name, _key(name), [editor])
        if command == 106:
            row['bindings'] = [['visible', 'not localBotSetup']]
        container['children'].insert(0, row)

    source = {'components': {'UIDataLocalVarsComponent': {'data': []}}, 'children': []}
    picker.patch_menu({'ImportedPackages': []}, source, [])
    panels = [node for node in source['children'] if node['name'] in ('SolTargetPicker', 'SolTargetModules')]
    for panel in panels:
        for control in list(_walk(panel)):
            event = control.get('components', {}).get('UIInputEventComponent', {}).get('onTouchUpInside', '')
            if event.startswith('SolTarget'):
                _attach(control, int(event[9:]))
    for panel, title, button, caption in (
        (panels[0], 'Tank', 'Cancel', 'Back'),
        (panels[1], 'Modules', 'Apply', 'UseTank'),
    ):
        heading, body = panel['children']
        heading['components']['UITextComponent']['text'] = _key(title)
        action = next(control for control in body['children'] if control.get('name') == button)
        action['children'][0]['components']['UITextComponent']['text'] = _key(caption)
    target_panel = panels[0]['children'][1]['children']
    # The catalogue reuses the current row/filter controls. Search is explicit
    # so non-modal text edits never poll the native renderer.
    search = next(n for n in target_panel if n.get('name') == 'Search')
    next(n for n in search['children'] if n.get('name') == 'Query')['size'][0] = 340.
    target_panel.append(_button('Find', _key('Find'), 399., 11., 121., 107))
    for name in ('SolTargetPicker', 'SolTargetModules', 'SolSetupIdentity', 'SolSetupRoster',
                 'SolSetupSlot', 'SolSetupPresets'):
        root['children'][:] = [node for node in root['children'] if node.get('name') != name]
    dialogs = [_lineup(), _slot(), _presets(), *panels]
    for dialog in dialogs:
        dialog['components']['IgnoreLayout'] = {}
    root['children'].extend([{'class': 'UIControl', 'name': 'SolSetupIdentity', 'visible': False,
                              'input': False, 'components': {'IgnoreLayout': {}}}, *dialogs])
