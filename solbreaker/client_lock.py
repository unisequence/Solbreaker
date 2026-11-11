"""Keep configuration stable for the complete owned client session."""
from contextlib import contextmanager
from pathlib import Path

from .platform_io import PrivateFileBusy, private_lock


class ClientConfigurationBusy(ValueError):
    pass


@contextmanager
def client_configuration_access(lab):
    """Use the same native private-file backend as account/server ownership."""
    lab = Path(lab).expanduser().resolve(strict=True)
    if not lab.is_dir():
        raise ValueError('Нужна существующая папка установки')
    if (lab/'private').is_symlink():
        raise ValueError('Папка private не должна быть символической ссылкой')
    try:
        with private_lock(lab/'private', 'client-config.lock'):
            yield
    except PrivateFileBusy:
        raise ClientConfigurationBusy('Клиент этой установки уже запущен или его конфигурация изменяется') from None
