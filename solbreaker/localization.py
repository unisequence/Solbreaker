"""Small additional server labels; the client already loads packaged strings."""
import yaml

LABELS={
    'NEWS_NOTIFICATION_BATTLE_WIN':('Победа!','Victory!'),
    'NEWS_NOTIFICATION_BATTLE_DEFEAT':('Поражение','Defeat'),
    'NEWS_NOTIFICATION_BATTLE_DRAW':('Ничья','Draw'),
    '#battle_results:Reason_technical':('Техническое завершение боя','Battle ended by forfeit'),
    '#battle_results:Result/Victory':('Победа!','Victory!'),
    '#battle_results:Result/Defeat':('Поражение','Defeat'),
    '#battle_results:Result/Draw':('Ничья','Draw'),
    'battleType/EncounterBattle':('Встречный бой','Encounter battle'),
    'hangar/BattleButton':('В бой!','Battle!'),
    'hangar/RepairButton':('Ремонт','Repair'),
    'Local/TrainingTitle':('УЧЕБНЫЙ БОЙ','TRAINING BATTLE'),
    'Local/TrainingDescription':('Локальный учебный бой.','Local training battle.'),
    'ratingBattles/gameplayType/dominationTitle':('Встречный бой','Encounter battle'),
    'ratingBattles/gameplayType/supremacyTitle':('Превосходство','Supremacy'),
}


def additional_labels(locale, server_name='Solbreaker'):
    language=0 if locale.split('_')[0].split('-')[0].lower()=='ru' else 1
    labels = {k:v[language] for k,v in LABELS.items()}
    labels.update({'Local PoC': server_name, 'Solbreaker': server_name})
    return yaml.safe_dump(labels,allow_unicode=True).encode()
