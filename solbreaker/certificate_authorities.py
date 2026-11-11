import re
import ssl
from pathlib import Path

def ca_file(lab, manifest):
    """Use an explicitly provisioned CA, otherwise the host's trusted roots."""
    from .connection import atomic_write
    embedded = manifest.get('tls_ca_certificate')
    if embedded is not None:
        raw = validate_ca(embedded).encode('ascii')
        path = Path(lab) / 'private/http-ca.pem'
        atomic_write(path, raw)
        return path.resolve(strict=True)
    configured = manifest.get('tls_ca_file')
    if configured is not None:
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError('tls_ca_file must be a certificate bundle path')
        path = Path(lab) / configured
        ssl.create_default_context(cafile=str(path))
        return path.resolve(strict=True)
    roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    if not roots:
        raise ValueError('No trusted certificate authorities are available')
    path = Path(lab) / 'private/http-ca.pem'
    atomic_write(path, ''.join(ssl.DER_cert_to_PEM_cert(root) for root in roots).encode('ascii'))
    return path.resolve(strict=True)

def validate_ca(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or re.fullmatch(r'(\s*-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+', value) is None):
        raise ValueError('Expected a PEM CA certificate bundle, at most 8 KiB')
    ssl.create_default_context(cadata=value)
    return value
