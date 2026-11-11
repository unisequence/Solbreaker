"""Human-readable server identity, separate from native account cache keys."""
import json
from pathlib import Path
import unicodedata

from .build_info import SERVER_NAME


def validate_name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 48
            or value != value.strip()
            or any(unicodedata.category(char).startswith('C') for char in value)):
        raise ValueError('Название сервера должно содержать от 1 до 48 символов без управляющих знаков')
    return value


def server_name(manifest):
    return validate_name(manifest.get('server_name', SERVER_NAME))


def configure_name(state, name):
    from .connection import atomic_write, ensure_lab, connection_profile
    from .storage import account_access
    name = validate_name(name)
    state = Path(state).resolve(strict=True)
    with account_access(state/'private'):
        state, manifest = ensure_lab(state)
        if manifest.get('client_only'):
            raise ValueError('Выбери данные своего сервера')
        # Refresh the managed profile used by the local launcher's next launch.
        profile_path = state/'connection.json'
        profile = connection_profile(state)
        if profile_path.exists():
            previous = json.loads(profile_path.read_text())
            if previous.get('server_id') != profile['server_id']:
                raise ValueError('Файл подключения принадлежит другому серверу')
        manifest['server_name'] = name
        atomic_write(state/'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode())
        if profile_path.exists():
            profile['server_name'] = name
            atomic_write(profile_path, (json.dumps(profile, indent=2)+'\n').encode())
    return dict(server_name=name, restart_required=True)
