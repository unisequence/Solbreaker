"""Small additional server labels; the client already loads packaged strings."""
import yaml

LOGIN_ERROR_LABELS = {
    '#wgn:APIErrors/invalid_grant': (
        'Неверная почта или пароль. Проверь данные аккаунта на этом сервере.',
        'Incorrect email or password. Check your account details for this server.',
    ),
    '#wgn:APIErrors/invalid_token': (
        'Сессия входа истекла. Войди в аккаунт заново.',
        'Your sign-in session has expired. Sign in again.',
    ),
    '#wgn:APIErrors/local_login_busy': (
        'Сервер занят проверкой входа. Повтори попытку через несколько секунд.',
        'The server is busy processing sign-ins. Try again in a few seconds.',
    ),
    '#wgn:APIErrors/local_token_capacity': (
        'На сервере слишком много активных сессий. Повтори попытку позже.',
        'The server has too many active sessions. Try again later.',
    ),
    '#wgn:APIErrors/local_server_stopping': (
        'Сервер останавливается. Дождись его запуска и войди снова.',
        'The server is shutting down. Wait for it to start and sign in again.',
    ),
}


def login_error_labels(language):
    return {key: texts[language] for key, texts in LOGIN_ERROR_LABELS.items()}


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
    labels.update(login_error_labels(language))
    labels.update({'Local PoC': server_name, 'Solbreaker': server_name})
    return yaml.safe_dump(labels,allow_unicode=True).encode()
