"""Native Polygon picker controls and a local search index for the pinned client."""
import struct
from xml.etree.ElementTree import ParseError

import yaml

from . import dvpl, practice_catalog
from .connection import atomic_write

INDEX = 'solbreaker-targets.bin'
INDEX_MAGIC = b'SBT3'
INDEX_ROWS = 1024
INDEX_MODULES = 8192
INDEX_HEADER = struct.Struct('<4sII')
INDEX_RECORD = struct.Struct('<HBBBxHH64s182s')
INDEX_MODULE = struct.Struct('<BB62s')
INDEX_SIZE = INDEX_HEADER.size + INDEX_ROWS * INDEX_RECORD.size + INDEX_MODULES * INDEX_MODULE.size
PAGE_ROWS = 8
MODULE_COLUMNS = 5
NATIONS = ('ussr', 'germany', 'usa', 'china', 'france', 'uk', 'japan', 'other', 'european')
AMBER = [1., .67, .17, 1.]

STRINGS = {
    'Picker': ('Free battle targets', 'Мишени свободного боя'),
    'TargetChoose': ('Choose tank and modules', 'Выбрать танк и модули'),
    'TargetSpawn': ('Place target at reticle', 'Поставить мишень в точке прицеливания'),
    'TargetAim': ('Target under the reticle', 'Мишень под прицелом'),
    'TargetHeal': ('Restore hit points', 'Восстановить прочность'),
    'TargetKill': ('Destroy', 'Уничтожить'),
    'TargetRemove': ('Remove', 'Убрать'),
    'TargetClear': ('Remove all targets', 'Убрать все мишени'),
    'TargetFire': ('Toggle return fire for new targets', 'Переключить ответный огонь новых мишеней'),
    'TmMiss': ('Aim at a Polygon target first', 'Наведи прицел на мишень Полигона'),
    'TmHeal': ('Target hit points restored', 'Прочность мишени восстановлена'),
    'TmKill': ('Target destroyed', 'Мишень уничтожена'),
    'TmGone': ('Target removed', 'Мишень убрана'),
    'TmFull': ('Target already at full hit points', 'У мишени полная прочность'),
    'Sandbox': ('Sandbox', 'Песочница'),
    'Stats': ('Tank stats', 'Характеристики танка'),
    'StatsBoost': ('Boost', 'Форсаж'),
    'StatsReload': ('Fast reload', 'Ускоренная перезарядка'),
    'StatsAim': ('Aiming speed', 'Скорость сведения'),
    'StatsAccuracy': ('Accuracy', 'Точность'),
    'StatsTurret': ('Turret traverse', 'Поворот башни'),
    'StatsHull': ('Hull traverse', 'Поворот корпуса'),
    'StatsDamage': ('Shell damage', 'Урон снаряда'),
    'StatsHealth': ('Hit points', 'Прочность'),
    'StatsBrake': ('Braking', 'Торможение'),
    'StatsReverse': ('Reverse speed', 'Скорость заднего хода'),
    'StatsGrip': ('Lateral grip', 'Боковое сцепление'),
    'StatsMobilityGroup': ('Mobility', 'Мобильность'),
    'StatsWeaponGroup': ('Gun', 'Орудие'),
    'StatsCooldownGroup': ('Cooldown', 'Перезарядка'),
    'StatsSurvivalGroup': ('Survivability', 'Выживаемость'),
    'StatsHelp': ('0% uses stock settings. Braking at 100% stops the tank immediately.',
                  '0% — штатные настройки. Торможение на 100% останавливает танк сразу.'),
    'Borders': ('Toggle map borders', 'Переключить границы карты'),
    'Border0': ('Map border opened', 'Выезд за границу открыт'),
    'Border1': ('Map border restored', 'Граница карты восстановлена'),
    'Border2': ('Return to the playable area first', 'Сначала вернись в игровую зону'),
    'Ai': ('Enemy AI', 'ИИ противников'),
    'AiTarget': ('Target under reticle', 'Цель под прицелом'),
    'AiAll': ('All enemies', 'Все противники'),
    'AiOff': ('Off', 'Выключен'),
    'AiEasy': ('Easy', 'Лёгкий'),
    'AiNormal': ('Normal', 'Обычный'),
    'AiHard': ('Hard', 'Сложный'),
    'AiSet': ('Enemy AI updated', 'Настройки ИИ применены'),
    'AiMiss': ('No enemy target selected', 'Подходящая цель не найдена'),
    'SandboxBack': ('Back to sandbox', 'Назад в песочницу'),
    'PickHint': ('Search by tank name', 'Найти танк по имени'),
    'PickEmpty': ('No matching tanks', 'Танки не найдены'),
    'PickPrev': ('Previous', 'Назад'),
    'PickNext': ('Next', 'Дальше'),
    'PickClose': ('Back to menu', 'Назад в меню'),
    'PickHow': ('Select a tank to see its modules', 'Выбери танк, чтобы открыть модули'),
    'PickUnavailable': ('Target catalogue unavailable', 'Каталог мишеней недоступен'),
    'PickModules': ('Target modules', 'Модули мишени'),
    'PickUse': ('Select target', 'Выбрать мишень'),
    'PickBack': ('Back to tanks', 'Назад к танкам'),
    'PickStock': ('Stock', 'Штатный'),
    'PickChassis': ('Suspension', 'Ходовая'),
    'PickTurret': ('Turret', 'Башня'),
    'PickGun': ('Turret guns', 'Орудия башни'),
    'PickEngine': ('Engine', 'Двигатель'),
    **{f'PickNation{i}': pair for i, pair in enumerate((
        ('All nations', 'Все нации'), ('USSR', 'СССР'), ('Germany', 'Германия'), ('USA', 'США'),
        ('China', 'Китай'), ('France', 'Франция'), ('UK', 'Великобритания'), ('Japan', 'Япония'),
        ('Hybrid', 'Сборная нация'), ('Europe', 'Европа')))},
    **{f'PickClass{i}': pair for i, pair in enumerate((
        ('All classes', 'Все классы'), ('Light', 'Лёгкий'), ('Medium', 'Средний'),
        ('Heavy', 'Тяжёлый'), ('Tank destroyer', 'ПТ-САУ')))},
    'PickTier': ('All tiers', 'Все уровни'),
}


_MENU_HELP = {'StatsHelp', 'PickHint', 'PickEmpty', 'PickHow', 'PickUnavailable'}
for _key, _texts in STRINGS.items():
    if (_key not in _MENU_HELP
            and (_key.startswith(('Picker', 'Target', 'Sandbox', 'Stats', 'Pick'))
                 or _key in ('Borders', 'Ai', 'AiTarget', 'AiAll', 'AiOff', 'AiEasy', 'AiNormal', 'AiHard'))):
        STRINGS[_key] = tuple(text.upper() for text in _texts)


def search_index(game):
    from .client_ui import _StringsLoader

    names = [yaml.load(dvpl.read(game/f'Data/Strings/{locale}.yaml.dvpl'), Loader=_StringsLoader)
             for locale in ('en', 'ru')]
    def translated(table, key):
        value = table.get(key, '')
        return '' if value == key else value

    rows = []
    for row in practice_catalog.catalog(game).values():
        if not row.installed:
            continue
        short = row.name_key+'_short'
        key = short if all(translated(table, short) for table in names) else row.name_key
        if not any(translated(table, key) for table in names) or row.model.startswith(
                row.name_key.rsplit(':', 1)[-1] + '_tutorial_'):
            key = row.model
        rows.append((row, key))
    rows.sort(key=lambda row: names[0].get(row[1], row[0].model).casefold())
    if len(rows) > INDEX_ROWS:
        raise ValueError('Polygon catalogue exceeds the pinned client capacity')
    records, options = bytearray(), bytearray()
    for target, key in rows:
        tier = target.tier if target.tier is not None and 1 <= target.tier <= 10 else 0
        vehicle_class = (practice_catalog.CLASSES.index(target.vehicle_class)+1
                         if target.vehicle_class else 0)
        nation = ((target.compact_id >> 4) & 15)+1
        aliases = (target.model, *(table.get(key, '') for table in names),
                   *(table.get(target.name_key, '') for table in names))
        searchable = ' '.join(dict.fromkeys(aliases)).casefold().encode()
        encoded = key.encode()
        if len(encoded) >= 64:
            raise ValueError('Polygon vehicle name key is too long')
        start = len(options) // INDEX_MODULE.size
        options.extend(module_records(game, target))
        count = len(options) // INDEX_MODULE.size - start
        records.extend(INDEX_RECORD.pack(target.compact_id, tier, nation, vehicle_class,
                                        start, count, encoded, searchable[:181]))
    count = len(options) // INDEX_MODULE.size
    if count > INDEX_MODULES:
        raise ValueError('Polygon module catalogue exceeds the pinned client capacity')
    return (INDEX_HEADER.pack(INDEX_MAGIC, len(rows), count)
            + records.ljust(INDEX_ROWS * INDEX_RECORD.size, b'\0')
            + options.ljust(INDEX_MODULES * INDEX_MODULE.size, b'\0'))


def module_records(game, target):
    try:
        choices = practice_catalog.modules(game, target)
    except (OSError, ValueError, ParseError):
        return b''
    groups = [(0, 0, choices.chassis), (1, 0, choices.turrets),
              (3, 0, choices.engines)]
    groups.extend((2, turret, guns) for turret, guns in enumerate(choices.guns[:16]))
    return b''.join(INDEX_MODULE.pack(slot, turret, module.name_key.encode()[:61])
                    for slot, turret, names in groups for module in names[:16])


def install(lab):
    game = lab/'client'
    if not (game/'Data/XML/item_defs/vehicles/common/nations.xml.dvpl').is_file():
        return
    path = lab/'documents'/INDEX
    if path.is_file() and path.stat().st_size == INDEX_SIZE:
        with path.open('rb') as source:
            if source.read(4) == INDEX_MAGIC:
                return
    atomic_write(path, search_index(game))


def _button(name, caption, x, y, width, command, *, height=46., color='gray'):
    return {'prototype': 'StyledButton/StyledButton', 'name': name,
            'classes': f'button button-color-{color}', 'position': [x, y], 'size': [width, height],
            'components': {'UIInputEventComponent': {'onTouchUpInside': f'SolTarget{command}'}},
            'children': [{'path': 'Caption', 'classes': 'text2 text-color-white',
                          'components': {
                              'Anchor': {'leftAnchor': 7., 'rightAnchor': 7.},
                              'UITextComponent': {'text': caption, 'fitting': 'FITTING_REDUCE'},
                          }}]}


def _mark(name, x, y, width, *, visible=True):
    return {'class': 'UIControl', 'name': name, 'input': False, 'visible': visible,
            'position': [x, y], 'size': [width, 2.], 'components': {
                'Background': {'drawType': 'DRAW_FILL', 'color': list(AMBER)}}}


def _sprite(name, asset, position, size, *, frame=0, visible=False):
    return {'class': 'UIControl', 'name': name, 'input': False, 'visible': visible,
            'position': position, 'size': size, 'components': {'Background': {
                'drawType': 'DRAW_SCALE_PROPORTIONAL_ONE', 'sprite': '~res:/'+asset, 'frame': frame}}}


def _row(index):
    row = _button(f'Row{index}', '', 0., 116.+index*42, 520., 8+index, height=39.)
    caption = row['children'][0]['components']
    caption['Anchor'] = {'leftAnchorEnabled': True, 'leftAnchor': 58.,
                         'rightAnchorEnabled': True, 'rightAnchor': 88.}
    caption['UITextComponent'].update(align=['LEFT', 'VCENTER'], fitting='FITTING_REDUCE')
    row['children'].extend(
        _sprite(f'Flag{nation}', f'Gfx/Lobby/flags/flag_filter_{tag}', [10., 8.], [38., 23.])
        for nation, tag in enumerate(NATIONS, 1))
    row['children'].extend(
        _sprite(f'Type{kind}', 'Gfx/UI/LobbyCommon/TankTypesSmall', [435., 6.], [27., 27.], frame=kind)
        for kind in range(1, 5))
    row['children'].append({
        'class': 'UIStaticText', 'name': 'Rank', 'input': False,
        'position': [472., 0.], 'size': [34., 39.], 'classes': 'text2 text-color-orange',
        'components': {'UITextComponent': {'align': ['HCENTER', 'VCENTER'], 'color': list(AMBER)}}})
    return row


def patch_menu(document, root, buttons):
    document['ImportedPackages'].extend(('~res:/UI/Screens/Common/TextFieldPanel.yaml',
                                         '~res:/UI/Screens/Common/Slider.yaml'))
    root['components']['UIDataLocalVarsComponent']['data'].append(['int', 'solTargetCommand', '0'])
    open_button = _button('SolTargetButton', 'Local/SbSandbox', 0., 0., 226., 40, height=70., color='orange')
    open_button['children'][0]['classes'] = 'header3 text-color-white'
    open_button['visible'] = False
    open_button['components']['Anchor'] = {
        'leftAnchorEnabled': True, 'leftAnchor': 10., 'rightAnchorEnabled': True, 'rightAnchor': 10.}
    buttons.insert(1, open_button)
    root['children'].append({'class': 'UITextField', 'name': 'SolTargetCommand',
                             'visible': False, 'input': False,
                             'bindings': [['text', 'str(solTargetCommand)']]})

    search = {'prototype': 'TextFieldPanel/TextFieldPanel', 'name': 'Search',
              'size': [520., 44.], 'components': {'Anchor': {'topAnchorEnabled': True, 'topAnchor': 12.},
                                                  'SizePolicy': {'horizontalPolicy': 'Ignore',
                                                                 'verticalPolicy': 'Ignore'}},
              'children': [
                  _sprite('Icon', 'Gfx/UI/CommonUIElements/search_icon', [10., 9.], [24., 24.],
                          visible=True),
                  {'class': 'UITextField', 'name': 'Query', 'size': [470., 36.], 'position': [40., 4.],
                  'fontPath': '~res:/Fonts/HelveticaNeueWorld-55Roman.ttf', 'fontSize': 17.,
                  'maxLength': 60, 'components': {'Focus': {'state': 'Enabled'}},
              }, {
                  'class': 'UIStaticText', 'name': 'Hint', 'input': False,
                  'classes': 'text2 text-color-white-70', 'position': [41., 4.], 'size': [470., 36.],
                  'components': {'UITextComponent': {'text': 'Local/SbPickHint',
                                                     'align': ['LEFT', 'VCENTER']}},
              }]}
    filters = []
    for name, label, x, command in (('Nation', 'Nation0', 0., 5),
                                    ('Tier', 'Tier', 177., 6), ('Class', 'Class0', 354., 7)):
        filters.extend((_button(name, f'Local/SbPick{label}', x, 64., 166., command, height=40.),
                        _mark(name+'Active', x+4., 102., 158., visible=False)))
    rows = [_row(i) for i in range(PAGE_ROWS)]
    navigation = [_button('Prev', 'Local/SbPickPrev', 0., 454., 121., 3, height=40.),
                  _button('Next', 'Local/SbPickNext', 399., 454., 121., 4, height=40., color='orange'),
                  _button('Cancel', 'Local/SbPicker', 145., 507., 230., 52, height=40.)]
    status = {'class': 'UIStaticText', 'name': 'Status', 'input': False,
              'classes': 'text2 text-color-white-70', 'position': [127., 454.], 'size': [266., 40.],
              'components': {'UITextComponent': {'align': ['HCENTER', 'VCENTER'],
                                                'fitting': 'FITTING_REDUCE'}}}
    root['children'].append({
        'prototype': 'Common/ResizableDialog', 'name': 'SolTargetPicker',
        'visible': False, 'size': [560., 619.],
        'components': {'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True},
                       'ModalInput': {}, 'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'}},
        'children': [
            {'path': 'Top/TitleText', 'components': {'UITextComponent': {'text': 'Local/SbPicker'}}},
            {'path': 'Middle/VerticalBox', 'components': {
                'LinearLayout': {'enabled': False}, 'SizePolicy': {'verticalPolicy': 'FixedSize',
                                                                   'verticalValue': 563.}},
             'children': [_mark('Accent', 0., 0., 520.), search, *filters, *rows, *navigation, status]},
        ],
    })
    root['children'].append(module_panel())
    root['children'].append(_sandbox_panel('SolSandboxHub', 'Sandbox', 354., [
        _button('Targets', 'Local/SbPicker', 0., 10., 520., 52, height=52.),
        _button('AI', 'Local/SbAi', 0., 74., 520., 41, height=52.),
        _button('Borders', 'Local/SbBorders', 0., 138., 520., 50, height=52.),
        _button('Stats', 'Local/SbStats', 0., 202., 520., 51, height=52.),
        _button('Back', 'Local/SbPickClose', 134., 285., 252., 39, height=44.),
    ]))
    root['children'].append(_sandbox_panel('SolTargetMenu', 'Picker', 365., [
        _button('Choose', 'Local/SbTargetChoose', 0., 10., 520., 1, height=44.),
        _button('Spawn', 'Local/SbTargetSpawn', 0., 62., 520., 53, height=44., color='orange'),
        _module_text('Aimed', 'Local/SbTargetAim', 0., 116., 520., 27., title=True),
        _mark('AimedDivider', 0., 144., 520.),
        _button('Heal', 'Local/SbTargetHeal', 0., 153., 190., 54, height=43.),
        _button('Destroy', 'Local/SbTargetKill', 198., 153., 157., 55, height=43.),
        _button('Remove', 'Local/SbTargetRemove', 363., 153., 157., 56, height=43.),
        _button('Fire', 'Local/SbTargetFire', 0., 207., 520., 58, height=43.),
        _button('Clear', 'Local/SbTargetClear', 0., 258., 520., 57, height=43.),
        _button('Back', 'Local/SbSandboxBack', 134., 312., 252., 2, height=40.),
    ]))
    root['children'].append(tank_stats_panel())
    rows = []
    for scope, label in enumerate(('AiTarget', 'AiAll')):
        y = 12. + scope * 88.
        rows.append(_module_text(label, 'Local/Sb' + label, 0., y, 520., 28., title=True))
        rows.extend(_button(f'AI{scope}{level}', 'Local/SbAi' + name,
                            131. * level, y + 34., 123., 42 + 4 * scope + level, height=40.)
                    for level, name in enumerate(('Off', 'Easy', 'Normal', 'Hard')))
    rows.append(_button('Back', 'Local/SbSandboxBack', 134., 199., 252., 2, height=44.))
    root['children'].append(_sandbox_panel('SolTargetAI', 'Ai', 267., rows))


def _stats_group(group, names, x, y):
    width = 400.
    controls = [_module_text(group, 'Local/SbStats'+group, x, y, width, 26., title=True),
                _mark(group+'Divider', x, y+27., width)]
    for index, name in enumerate(names):
        row = y + 32. + 55. * index
        amount = _module_text(name+'Amount', '0%', x+336., row, 64., 22.)
        amount['components']['UITextComponent']['align'] = ['RIGHT', 'VCENTER']
        controls.extend((
            _module_text(name+'Label', 'Local/SbStats'+name, x, row, 308., 22.),
            amount,
            {'prototype': 'Slider/Slider', 'name': name, 'input': True,
             'position': [x, row+23.], 'size': [width, 30.],
             'children': [{'path': 'thumbSpriteControl', 'size': [28., 28.]}]},
        ))
    return controls


def tank_stats_panel():
    controls = [
        *_stats_group('MobilityGroup', ('Boost', 'Brake', 'Reverse', 'Grip', 'Hull'), 0., 10.),
        *_stats_group('SurvivalGroup', ('Health',), 0., 331.),
        *_stats_group('WeaponGroup', ('Aim', 'Accuracy', 'Turret', 'Damage'), 440., 10.),
        *_stats_group('CooldownGroup', ('Reload',), 440., 276.),
        _module_text('Help', 'Local/SbStatsHelp', 0., 425., 840., 26.),
        _button('Back', 'Local/SbSandboxBack', 294., 458., 252., 2, height=44.),
    ]
    return _sandbox_panel('SolTankStats', 'Stats', 518., controls, width=880.)


def _sandbox_panel(name, title, height, controls, *, width=560.):
    return {'prototype': 'Common/ResizableDialog', 'name': name, 'visible': False,
            'size': [width, height + 56.],
            'components': {'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True},
                           'ModalInput': {}, 'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'}},
            'children': [
                {'path': 'Top/TitleText', 'components': {'UITextComponent': {'text': 'Local/Sb' + title}}},
                {'path': 'Middle/VerticalBox', 'components': {
                    'LinearLayout': {'enabled': False}, 'SizePolicy': {'verticalPolicy': 'FixedSize',
                                                                       'verticalValue': height}},
                 'children': controls},
            ]}


def _module_text(name, value, x, y, width, height, *, title=False):
    return {'class': 'UIStaticText', 'name': name, 'input': False, 'position': [x, y],
            'size': [width, height],
            'classes': 'header3 text-color-white' if title else 'text2 text-color-white-70',
            'components': {'UITextComponent': {'text': value, 'align': ['LEFT', 'VCENTER'],
                                                'fitting': 'FITTING_REDUCE'}}}


def _rail(name, x, y, width, height, *, active=False):
    return {'class': 'UIControl', 'name': name, 'input': False,
            'position': [x, y], 'size': [width, height],
            'components': {'Background': {'drawType': 'DRAW_FILL',
                                          'color': list(AMBER) if active else [.33, .41, .47, 1.]}}}


def _module_card(slot, column, x, y):
    width, height = 126., 54.
    card = _button(f'Card{column}', '', x, y, width, 18 + MODULE_COLUMNS * slot + column, height=height)
    card['children'][0]['components']['UITextComponent'].update(
        {'align': ['HCENTER', 'VCENTER'], 'fitting': 'FITTING_REDUCE'})
    card['children'].extend((
        _rail('Incoming', -10., 26., 10., 2.),
        _rail('Selected', 2., height - 4., width - 4., 3., active=True),
    ))
    if slot == 1:
        card['children'].append(_rail('GunLink', width / 2., height, 2., 13., active=True))
    return card


def module_panel():
    controls = [_mark('Accent', 0., 0., 820.), _module_text('Vehicle', '', 0., 9., 810., 39., title=True)]
    for slot, (name, icon) in enumerate((('Chassis', 'tracks'), ('Turret', 'tower'),
                                         ('Gun', 'gun'), ('Engine', 'engine'))):
        controls.append({
            'class': 'UIControl', 'name': f'Branch{slot}', 'input': False,
            'position': [0., 56. + slot * 70.], 'size': [820., 66.],
            'children': [
                _sprite('Icon', f'Gfx/Shared/icons/damaged-modules/icon_damaged-module_{icon}_m',
                        [0., 14.], [25., 26.], visible=True),
                _module_text('Slot', 'Local/SbPick' + name, 33., 0., 100., 54.),
                *(_module_card(slot, column, 143. + 136. * column, 0.)
                  for column in range(MODULE_COLUMNS)),
            ],
        })
    controls.extend((
        _button('Back', 'Local/SbPickBack', 0., 356., 219., 16, height=46.),
        _button('Apply', 'Local/SbPickUse', 601., 356., 219., 17, height=46., color='orange'),
    ))
    return {'prototype': 'Common/ResizableDialog', 'name': 'SolTargetModules',
            'visible': False, 'size': [860., 480.],
            'components': {'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True},
                           'ModalInput': {}, 'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'}},
            'children': [
                {'path': 'Top/TitleText', 'components': {'UITextComponent': {'text': 'Local/SbPickModules'}}},
                {'path': 'Middle/VerticalBox', 'components': {
                    'LinearLayout': {'enabled': False}, 'SizePolicy': {'verticalPolicy': 'FixedSize',
                                                                       'verticalValue': 424.}},
                 'children': controls},
            ]}


def actions():
    return ''.join(f'action SolTarget{i}() {{ ChangeData(solTargetCommand, '
                   f'(64 + solTargetCommand - solTargetCommand % 64) % 32000 + {i}); }}\n'
                   for i in (*range(1, 18 + 4 * MODULE_COLUMNS), *range(39, 59)))
