"""Normal recon configuration, shared by server startup and the local runner."""
import os


def requested_enabled(environ=None):
    """Default on; the explicit normal flag takes precedence over the legacy flag.

    Retaining the old switch lets isolated fixtures explicitly disable recon
    with SOLBREAKER_RECON_SERVER_TEST=0. Values other than 0/1 are never guessed.
    """
    environ = os.environ if environ is None else environ
    name = ('SOLBREAKER_RECON_ENABLED' if 'SOLBREAKER_RECON_ENABLED' in environ
            else 'SOLBREAKER_RECON_SERVER_TEST')
    value = environ.get(name, '1')
    if value not in ('0', '1'):
        raise ValueError(name + ' must be 0 or 1')
    return value == '1'


def verify_running_recon(running, requested):
    """Require the canonical boolean reported by the current local server."""
    actual = running.get('recon_enabled')
    if type(actual) is not bool or actual != requested:
        raise ValueError('The running server has a different or unknown recon setting. '
                         'Stop that server and restart with the requested SOLBREAKER_RECON_ENABLED setting.')
