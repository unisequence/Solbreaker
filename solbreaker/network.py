"""Explicit IPv4 addresses for listening, client discovery and local services."""
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


PRIVATE = tuple(ipaddress.ip_network(n) for n in
                ('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10'))


def ipv4(value, *, wildcard=False, public=False):
    try:address=ipaddress.IPv4Address(value)
    except (ValueError,TypeError):raise ValueError('Use a literal IPv4 address') from None
    if wildcard and address.is_unspecified:return str(address)
    if not any(address in network for network in PRIVATE) and not (public and address.is_global):
        raise ValueError('Use an IPv4 address in a loopback, private LAN or CGNAT network')
    if address.is_multicast or address.is_unspecified or address==ipaddress.IPv4Address('127.0.0.0'):
        raise ValueError('Invalid endpoint address')
    return str(address)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls,value,*,listen=False,public=False):
        if not isinstance(value,str):raise ValueError('Endpoint must be an IPv4:port string')
        host,sep,port=value.rpartition(':')
        if not sep or not port.isdecimal():raise ValueError('Endpoint must contain IPv4:port')
        host=ipv4(host,wildcard=listen,public=public);port=int(port)
        if not 1024<=port<=65535:raise ValueError('Endpoint port must be 1024..65535')
        return cls(host,port)

    @property
    def pair(self):return self.host,self.port

    def __str__(self):return f'{self.host}:{self.port}'


def http_host(value, *, public=False):
    """Canonical IPv4 or an explicit v2 DNS name, without resolving DNS."""
    if value == 'localhost':
        return '127.0.0.1'
    try:
        ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        if not public or not isinstance(value, str):
            raise ValueError('DNS HTTP endpoints require an explicit version 2 profile') from None
        try:
            name = value.removesuffix('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ValueError('Invalid DNS server name') from None
        labels = name.split('.')
        if (len(name) > 253 or len(labels) < 2
                or not any(character.isalpha() for character in labels[-1])
                or any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None
                       for label in labels)):
            raise ValueError('Use a fully qualified DNS server name')
        return name
    return ipv4(value, public=public)


def origin(value, *, public=False):
    if not isinstance(value,str):raise ValueError('HTTP origin must be a string')
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('HTTP origin must not contain whitespace or control characters')
    url=urlsplit(value)
    if (url.scheme not in ('http','https') or url.username is not None
            or url.password is not None or url.path or url.query or url.fragment):
        raise ValueError('Use an HTTP(S) origin without credentials, path, query or fragment')
    host = http_host(url.hostname, public=public)
    if url.scheme != 'https':
        ipv4(host, public=public)  # DNS origins always require authenticated HTTPS.
    port = url.port if url.port is not None else (443 if url.scheme == 'https' else 80)
    if not 1 <= port <= 65535:
        raise ValueError('HTTP port must be 1..65535')
    return f'{url.scheme}://{host}:{port}'


@dataclass(frozen=True)
class NetworkConfig:
    listen_http: Endpoint
    listen_login: Endpoint
    listen_base: Endpoint
    advertise_http: str
    advertise_login: Endpoint
    advertise_base: Endpoint
    services_http: str
    client_networks: tuple
    version: int = 1

    def allows(self,address):
        try:address=ipaddress.IPv4Address(address)
        except (ValueError,TypeError):return False
        return any(address in network for network in self.client_networks)

    def document(self):
        return dict(version=self.version,
            listen=dict(http=str(self.listen_http),login=str(self.listen_login),base=str(self.listen_base)),
            advertise=dict(http=self.advertise_http,login=str(self.advertise_login),base=str(self.advertise_base)),
            services=dict(http=self.services_http),
            access=dict(client_networks=[str(n) for n in self.client_networks]))

    def toml(self):
        document=self.document();lines=[f'version = {self.version}','']
        for section in ('listen','advertise','services','access'):
            lines.append(f'[{section}]')
            lines.extend(f'{key} = {json.dumps(value)}' for key,value in document[section].items())
            lines.append('')
        return '\n'.join(lines)


def defaults(manifest):
    http=manifest['http_port'];login=manifest['login_port'];base=manifest.get('base_port',login+1)
    return dict(version=1,
        listen=dict(http=f'127.0.0.1:{http}',login=f'127.0.0.1:{login}',base=f'127.0.0.1:{base}'),
        advertise=dict(http=f'http://127.0.0.1:{http}',login=f'127.0.0.1:{login}',base=f'127.0.0.1:{base}'),
        services=dict(http=f'http://127.0.0.1:{http}'),access=dict(client_networks=['127.0.0.0/8']))


def parse_config(document):
    expected={'version','listen','advertise','services','access'}
    if not isinstance(document,dict) or set(document)!=expected or type(document['version']) is not int or document['version'] not in (1,2):
        raise ValueError('Network configuration must have version 1 or 2, listen, advertise, services and access')
    public = document['version'] == 2
    sections={'listen':{'http','login','base'},'advertise':{'http','login','base'},
              'services':{'http'},'access':{'client_networks'}}
    for name,keys in sections.items():
        if not isinstance(document[name],dict) or set(document[name])!=keys:
            raise ValueError(f'Invalid fields in network section {name}')
    bind=document['listen'];advertise=document['advertise']
    advertised_http = origin(advertise['http'], public=public)
    if public and not advertised_http.startswith('https://'):
        raise ValueError('Public network profiles require HTTPS')
    listeners={k:Endpoint.parse(v,listen=True,public=public) for k,v in bind.items()}
    if listeners['login'].port==listeners['base'].port and (listeners['login'].host==listeners['base'].host or
            '0.0.0.0' in (listeners['login'].host,listeners['base'].host)):
        raise ValueError('LoginApp and BaseApp must not bind the same UDP socket')
    networks=document['access']['client_networks']
    if not isinstance(networks,list) or not 1<=len(networks)<=32 or not all(isinstance(n,str) for n in networks):
        raise ValueError('Provide one to 32 IPv4 client networks')
    try:networks=tuple(ipaddress.IPv4Network(n,strict=True) for n in networks)
    except ValueError:raise ValueError('Client networks must be canonical IPv4 CIDRs') from None
    if any(not any(n.subnet_of(allowed) for allowed in PRIVATE)
           and not (public and n.prefixlen == 32 and n.network_address.is_global
                    and not n.network_address.is_multicast) for n in networks):
        raise ValueError('Client networks require private CIDRs or explicit public /32 hosts in version 2')
    result=NetworkConfig(listeners['http'],listeners['login'],listeners['base'],advertised_http,
        Endpoint.parse(advertise['login'],public=public),Endpoint.parse(advertise['base'],public=public),
        origin(document['services']['http'],public=public),networks,document['version'])
    # Literal service addresses retain the existing ACL consistency check.
    # DNS is resolved by the connecting client. Inbound peer ACLs still use IPs.
    for address in (urlsplit(result.advertise_http).hostname,result.advertise_login.host,
                    result.advertise_base.host,urlsplit(result.services_http).hostname):
        try:ipaddress.IPv4Address(address)
        except ValueError:continue
        if not result.allows(address):raise ValueError(f'Address {address} is outside access.client_networks')
    return result


def load_network(lab,manifest=None):
    lab=Path(lab)
    if manifest is None:manifest=json.loads((lab/'manifest.json').read_text())
    path=lab/'network.toml'
    if not path.exists():return parse_config(defaults(manifest))
    if path.stat().st_size>16384:raise ValueError('Network configuration is too large')
    return parse_config(tomllib.loads(path.read_text()))
