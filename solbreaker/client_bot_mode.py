"""Local bot-mode presentation without changing owned vehicle modules.

TFS uses the pinned client's mode7 queue slot for bots. The native hangar
otherwise applies its Rating-only top-module and crew predicates to that slot.
All other BattleButton prerequisites stay in the original prototype.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT_HANGAR = ContextVar('tfs_bot_mode_current_hangar', default=True)
_APPEARANCE = ContextVar('tfs_bot_mode_appearance', default='native')

BOT_MODE = 'eGameMode.Rating'
BOT_DESCRIPTION = 'Local/BotsDescription'


def _bot_mode(prefix):
    description = prefix+'.gameModeOptions.description'
    return (prefix+'.gameMode == '+BOT_MODE+' and ('+description+' == "'+BOT_DESCRIPTION+
            '" or '+description+' == localize("'+BOT_DESCRIPTION+'"))')


CARD_BOT_MODE = _bot_mode('mode')
SELECTED_BOT_MODE = _bot_mode('session.gameModesAccountModel.mode')
ORIGINAL_VEHICLE_GATE = ('not isNull(selectedTank) and selectedTank.allowedForGameMode '
                         'and not selectedTank.needsTopMastery and not selectedTank.needsTopModules')
LOCAL_VEHICLE_GATE = ('not isNull(selectedTank) and (('+SELECTED_BOT_MODE+') or '
                      '(selectedTank.allowedForGameMode and not selectedTank.needsTopMastery '
                      'and not selectedTank.needsTopModules))')
BADGE_NAME = 'LocalBotsModeBadge'
MODE_ICON = '~res:/Gfx/Shared/icons/icon_game-mode_realistic_colored'
ORIGINAL_CELL_BLOCKED = ('showStatus and (broken or locked or (soloOrSquad and '
    '(not tank.allowedForGameMode or tank.needsTopMastery or tank.needsTopModules)))')
CALIBRATION_GATE = ('if(session.gameModesAccountModel.mode.battleType == "7" and isNoRatingBattles '
    'and isBattleTypeChanged and notRatingSeasonMismatch and '
    'session.gameModesAccountModel.mode.availabilityStatus == eGameModeAvailability.AVAILABLE '
    'and prebattleIsAvailableForCalibration and not isHasRating)')


@contextmanager
def previous_hangar_recipe(appearance='original'):
    """Reconstruct a known previous presentation for verified old receipts.

    Context-local so a launch of one installation cannot affect another thread.
    Only the theme migration uses this; gameplay never selects the old rule.
    """
    if appearance not in ('original', 'flat'):
        raise ValueError('Unsupported previous bot-mode appearance')
    token = _CURRENT_HANGAR.set(appearance != 'original')
    style_token = _APPEARANCE.set(appearance)
    try:
        yield
    finally:
        _APPEARANCE.reset(style_token)
        _CURRENT_HANGAR.reset(token)


def patch_hangar_actions(text):
    if text.count(CALIBRATION_GATE) != 1:
        raise ValueError('Unsupported local bot calibration action')
    return text.replace(CALIBRATION_GATE, 'if(not ('+SELECTED_BOT_MODE+') and ('+
                        CALIBRATION_GATE[3:-1]+'))')


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _prototype(document, name):
    roots = document.get('Prototypes', [])
    if len(roots) != 1 or roots[0].get('name') != name:
        raise ValueError('Unsupported local bot-mode prototype: '+name)
    return roots[0]


def _flat_badge(condition, *, card=False):
    # Retained byte-for-byte only to recognize previous owned theme receipts.
    result = {'class': 'UIControl', 'name': BADGE_NAME, 'input': False,
        'classes': 'header3 color=text-color-white' if card else 't-headline bold white-wild-sand-text',
        'components': {
            'Background': {'drawType': 'DRAW_FILL', 'color': [0.055, 0.18, 0.23, 1.]},
            'UITextComponent': {'text': 'AI', 'align': ['HCENTER', 'VCENTER']},
            'SizePolicy': {'horizontalPolicy': 'FixedSize', 'horizontalValue': 48.,
                           'verticalPolicy': 'FixedSize', 'verticalValue': 48.},
            'StyleSheet': {'styles': ('~res:/UI/Screens/Fonts.yaml;~res:/UI/Styles/ColorStyles.yaml'
                if card else '~res:/UI/Screens3/Font.style.yaml;~res:/UI/Screens3/Color.style.yaml')}},
        'bindings': [['visible', condition]]}
    if card:
        result['components']['Anchor'] = {'hCenterAnchorEnabled': True,
            'topAnchorEnabled': True, 'topAnchor': -30.}
    else:
        result['components'].update({'IgnoreLayout': {}, 'UIAttachLayoutSlaveComponent': {
            'attachId': 'CHOOSE_BATTLE_TYPE', 'masterAlign': 'TopCenter',
            'slaveAlign': 'TopCenter', 'additionalOffset': [0., 4.]}})
    return result


def _native_label(condition, *, card=False):
    # The native sprite, original Icon geometry, HSV material and input remain
    # in their existing slots. Only a small shadowed caption distinguishes AI.
    return {'class': 'UIControl', 'name': BADGE_NAME, 'input': False,
        'components': {
            'UITextComponent': {'text': 'AI', 'fontPath': ('~res:/Fonts/WarHeliosCondCBold.ttf'
                if card else '~res:/Fonts/HelveticaNeueWorld-75Bold.ttf'),
                'fontSize': 14. if card else 12., 'color': [1., 1., 1., 1.],
                'colorInheritType': 'COLOR_MULTIPLY_ALPHA_ONLY',
                'shadowColor': [0., 0., 0., .95], 'shadowOffset': [0., 1.],
                'align': ['HCENTER', 'VCENTER']},
            'Anchor': {'hCenterAnchorEnabled': True, 'bottomAnchorEnabled': True,
                       'bottomAnchor': -1. if card else 0.},
            'SizePolicy': {'horizontalPolicy': 'FixedSize', 'horizontalValue': 32.,
                           'verticalPolicy': 'FixedSize', 'verticalValue': 18. if card else 16.}},
        'bindings': [['visible', condition]]}


def patch_battle_button(document):
    if _APPEARANCE.get() != 'native':
        return False
    root = _prototype(document, 'BattleButton')
    _mode_param(root)
    wrappers = [node for node in _walk(root) if node.get('name') == 'BattleTypeWrapper']
    if len(wrappers) != 1:
        raise ValueError('Unsupported local bot mode button')
    icons = [node for node in _walk(wrappers[0]) if node.get('name') == 'Icon']
    if len(icons) != 1 or icons[0].get('bindings') != [['UITextureComponent.spritePath', 'battleTypeSprite']]:
        raise ValueError('Unsupported local bot mode button icon')
    icons[0]['bindings'][0][1] = 'when localBotsMode -> "'+MODE_ICON+'", battleTypeSprite'
    icons[0].setdefault('children', []).append(_native_label('localBotsMode'))
    return True


def patch_hangar(document):
    if not _CURRENT_HANGAR.get():
        return
    root = _prototype(document, 'Hangar')
    calls = [item for item in _walk(root) if item.get('prototypePath') ==
             '~res:/UI/Screens3/Lobby/Hangar/BattleButton.yaml']
    if len(calls) != 1 or calls[0].get('args', {}).get('isVehicleFitsForGameMode') != ORIGINAL_VEHICLE_GATE:
        raise ValueError('Unsupported local bot vehicle eligibility binding')
    calls[0]['args']['isVehicleFitsForGameMode'] = LOCAL_VEHICLE_GATE
    panels = [item for item in _walk(root) if item.get('prototypePath') ==
              '~res:/UI/Screens3/Lobby/Hangar/TanksPanel/TanksPanel.yaml']
    if len(panels) != 1 or 'localBotsMode' in panels[0].get('args', {}):
        raise ValueError('Unsupported local bot carousel attachment')
    panels[0]['args']['localBotsMode'] = SELECTED_BOT_MODE
    if any(item.get('name') == BADGE_NAME for item in _walk(root)):
        raise ValueError('Duplicate local bot-mode badge')
    if _APPEARANCE.get() == 'flat':
        condition = (SELECTED_BOT_MODE+' and account.diaryDataModel.isBattleTypeButtonVisible '
            'and not isInSquad and not (account.prebattleModel.settings.prebattleType '
            '== PrebattleType.TRAINING and app.sDlc.isGeneralPackLoaded)')
        root['children'].append(_flat_badge(condition))
    else:
        calls[0]['args']['localBotsMode'] = SELECTED_BOT_MODE


def _mode_param(root):
    values = root['components']['UIDataParamsComponent']['params']
    if any(row[1] == 'localBotsMode' for row in values):
        raise ValueError('Duplicate local bot mode parameter')
    values.append(['bool', 'localBotsMode', 'false', 'false'])


def patch_tanks_panel(document):
    if not _CURRENT_HANGAR.get():
        return
    root = _prototype(document, 'TanksPanel')
    _mode_param(root)
    repeats = [row for row in _walk(root) if row.get('control') ==
               '~res:/UI/Screens3/Lobby/Hangar/TanksPanel/OwnedTankCell.yaml']
    if len(repeats) != 1 or 'localBotsMode' in repeats[0].get('args', {}):
        raise ValueError('Unsupported local bot carousel repeat')
    repeats[0]['args']['localBotsMode'] = 'localBotsMode'


def patch_owned_cell(document):
    if not _CURRENT_HANGAR.get():
        return
    root = _prototype(document, 'OwnedTankCell')
    _mode_param(root)
    values = root['components']['UIDataLocalBindingsComponent']['data']
    blocked = [row for row in values if row[:2] == ['bool', 'blocked']]
    hint = [row for row in values if row[:2] == ['string', 'blockHint']]
    if len(blocked) != 1 or blocked[0][2] != ORIGINAL_CELL_BLOCKED or len(hint) != 1:
        raise ValueError('Unsupported local bot carousel eligibility')
    blocked[0][2] = ORIGINAL_CELL_BLOCKED.replace('soloOrSquad and ',
                                                'soloOrSquad and not localBotsMode and ')
    for predicate in ('not tank.allowedForGameMode', 'tank.needsTopMastery', 'tank.needsTopModules'):
        before = predicate+' -> '
        if hint[0][2].count(before) != 1:
            raise ValueError('Unsupported local bot carousel block hint')
        hint[0][2] = hint[0][2].replace(before, 'not localBotsMode and '+before)


def patch_card(document):
    root = _prototype(document, 'GameType')
    values = root['components']['UIDataLocalBindingsComponent']['data']
    for key, before, after in (
            ('sprite', '~res:/Gfx/UI/Hangar/GameTypes/battle-type_rating',
             '~res:/Gfx/UI/Hangar/GameTypes/battle-type_regular'),
            ('gameModeSprite', '~res:/Gfx/Shared/icons/icon_game-mode_rating_colored',
             '~res:/Gfx/Shared/icons/icon_game-mode_random_colored' if _APPEARANCE.get() == 'flat' else MODE_ICON)):
        matches = [row for row in values if row[:2] == ['string', key]]
        if (len(matches) != 1 or matches[0][2].count(before) != 1
                or not matches[0][2].startswith('when ')):
            raise ValueError('Unsupported local bot card graphic: '+key)
        # The raw description key is the server's existing local-mode marker.
        # A genuine Rating mode7 from another server keeps its original icon.
        matches[0][2] = ('when '+CARD_BOT_MODE+' -> "'+after+'", '+matches[0][2][5:])
    content, = [child for child in root['children'] if child.get('name') == 'GameTypeContent']
    icon, = [child for child in content['children'] if child.get('name') == 'Icon']
    if any(row[0] == 'visible' for row in icon.get('bindings', [])):
        raise ValueError('Unsupported local bot card icon visibility')
    if _APPEARANCE.get() == 'flat':
        icon.setdefault('bindings', []).append(['visible', 'not ('+CARD_BOT_MODE+')'])
        content['children'].append(_flat_badge(CARD_BOT_MODE, card=True))
    else:
        icon.setdefault('children', []).append(_native_label(CARD_BOT_MODE, card=True))
