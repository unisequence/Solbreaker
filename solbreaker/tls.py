"""TLS for the account HTTP listener, configured before opening game sockets."""
import ipaddress
from pathlib import Path
import ssl
from urllib.parse import urlsplit


def server_context(lab, manifest, network):
    settings = manifest.get('http_tls')
    secure_listener = urlsplit(network.services_http).scheme == 'https'
    if settings is None:
        if secure_listener:
            raise ValueError('HTTPS services require http_tls certificate and private_key')
        if network.version == 2 and not ipaddress.ip_address(network.listen_http.host).is_loopback:
            raise ValueError('Public HTTP listener requires TLS or a loopback reverse-proxy backend')
        return None
    if (not isinstance(settings, dict) or set(settings) != {'certificate', 'private_key'}
            or any(not isinstance(value, str) or not value.strip() for value in settings.values())):
        raise ValueError('http_tls requires certificate and private_key paths')
    if not secure_listener:
        raise ValueError('A TLS listener requires HTTPS services.http')
    certificate, private_key = (Path(lab) / settings[name] for name in ('certificate', 'private_key'))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # An encrypted unattended-server key must fail instead of prompting on stdin.
    context.load_cert_chain(certificate, private_key, password=lambda: b'')
    return context
