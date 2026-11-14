"""Retarget only lab resources and exchange a public connection profile."""
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from urllib.parse import urlsplit,urlunsplit

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from . import dvpl
from .market import configure_market
from .network import defaults,load_network,parse_config
from .build_info import SERVER_NAME, LEGACY_SERVER_NAME

REGIONS=('regions_publish.yaml.dvpl','regions_development.yaml.dvpl','regions_review.yaml.dvpl',
         'regions_supertest.yaml.dvpl','regions_switch.yaml.dvpl')
CONNECTION_PROFILE_MAX_BYTES = 16384


def public_key(lab):
    raw=dvpl.read(Path(lab)/'client/Data/loginapp_wot.pubkey.dvpl')
    return raw,key_id(raw)


def key_id(raw):
    key=serialization.load_pem_public_key(raw)
    if not isinstance(key,rsa.RSAPublicKey) or key.key_size!=2048:
        raise ValueError('Expected a 2048-bit RSA LoginApp public key')
    canonical=key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(canonical).hexdigest()


def regions_config(regions,network):
    regions=copy.deepcopy(regions)
    names = set(regions['Regions'])
    if names not in ({SERVER_NAME}, {LEGACY_SERVER_NAME}):
        raise ValueError('Expected the isolated Solbreaker region')
    region_name = next(iter(names))
    local = regions['Regions'][region_name]
    previous=urlsplit(local['wgnwn'])
    def replace(value):
        if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
        if isinstance(value,list):return [replace(v) for v in value]
        if isinstance(value,str) and value.startswith(('http://','https://')):
            url=urlsplit(value)
            if (url.scheme,url.netloc)==(previous.scheme,previous.netloc):
                new=urlsplit(network.advertise_http)
                return urlunsplit((new.scheme,new.netloc,url.path,url.query,url.fragment))
        return value
    regions=replace(regions)
    local=regions['Regions'][region_name]
    local['wgnwn']=network.advertise_http
    local['hosts']={'LOCAL':dict(clusterID=0,url=str(network.advertise_login))}
    local['preferredHost']='LOCAL'
    regions['Common']['additionalLocalizationBaseUrl']=network.advertise_http+'/localizations'
    configure_market(regions,network.advertise_http)
    return regions


def atomic_write(path,data,*,replace_symlink=False):
    path=Path(path)
    if path.is_symlink() and not replace_symlink:
        raise ValueError(f'Refusing to overwrite a resource symlink: {path}')
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def ensure_lab(lab):
    lab=Path(lab).resolve(strict=True)
    manifest_path=lab/'manifest.json'
    def linked(path):
        info=path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)
    def separate(first,second):
        return not (first==second or first in second.parents or second in first.parents)
    if linked(manifest_path) or not manifest_path.is_file() or manifest_path.stat().st_nlink!=1:
        raise ValueError('Expected a private regular lab manifest')
    manifest=json.loads(manifest_path.read_text())
    client=lab/'client';data=client/'Data'
    if (str(client)!=manifest['client'] or linked(client) or linked(data)
            or not client.is_dir() or not data.is_dir()):
        raise ValueError('Expected a separate prepared lab client directory')
    game=Path(manifest['game']).resolve(strict=True)
    source=Path(manifest.get('source_game',manifest['game'])).resolve(strict=True)
    if not separate(lab,source):
        raise ValueError('The lab must be separate from game resources')
    if not game.is_dir() or not separate(client,game) or os.path.samefile(client,game):
        raise ValueError('The game resource view must be separate from the lab client')
    for original in (source,source/'Data',game,game/'Data'):
        if original.exists() and (os.path.samefile(client,original) or os.path.samefile(data,original)):
            raise ValueError('Lab client directories must not alias source/view directories')
    if game.is_relative_to(lab) and manifest.get('tank_workshop_view') is not None:
        # Workshop server views retain the external pinned executable and
        # version resource. Client directories are checked separately above.
        from .build_info import BUILD_SHA256
        from .resource_source import validate_snapshot
        if (game.parent != lab/'tank-views' or game.name != manifest['tank_workshop_view']
                or not game.name.startswith('view-') or linked(lab/'tank-views')
                or manifest.get('exe_sha256') != BUILD_SHA256 or 'source_game' not in manifest
                or not os.path.samefile(game/'wotblitz.exe', source/'wotblitz.exe')
                or not os.path.samefile(game/'Data/version.txt.dvpl', source/'Data/version.txt.dvpl')):
            raise ValueError('Invalid Workshop resource view')
        validate_snapshot(manifest)
    elif game.is_relative_to(lab):
        # Supported overlays retain their external immutable snapshot as
        # source_game. Do not accept an arbitrary directory merely because it
        # was labelled a private view in editable lab metadata.
        from .build_info import BUILD_SHA256
        from .resource_source import validate_snapshot
        # The earlier overlay builder retained the same immutable pin under
        # source_snapshot; normalize a copy, never rewrite the lab manifest.
        snapshot=manifest.get('snapshot') or manifest.get('source_snapshot')
        snapshot_sha=(manifest.get('snapshot_sha256') if manifest.get('snapshot')
                      else manifest.get('source_snapshot_sha256'))
        if (not manifest.get('source_game') or not snapshot or not snapshot_sha
                or manifest.get('exe_sha256')!=BUILD_SHA256):
            raise ValueError('An internal game view requires its pinned external source snapshot')
        metadata=Path(snapshot)
        if (metadata.name!='snapshot.json' or metadata.parent.resolve(strict=True)!=source.parent
                or linked(metadata) or not metadata.is_file()):
            raise ValueError('Expected snapshot metadata beside the external pinned source')
        for directory in (game,*game.parents):
            if directory==lab:break
            if linked(directory):raise ValueError('Game view directories must not be aliases')
        validate_snapshot(dict(manifest,snapshot=snapshot,snapshot_sha256=snapshot_sha))
    elif not separate(lab,game):
        raise ValueError('The lab must be separate from game resources')
    # Check every destination before writing any of them.
    for path in [lab/'network.toml',lab/'regions.yaml',lab/'manifest.json',
                 *(data/name for name in REGIONS),data/'server_config_urls.yaml.dvpl',data/'loginapp_wot.pubkey.dvpl']:
        if path.exists() or path.is_symlink():
            info=path.lstat()
            if linked(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise ValueError(f'Refusing to overwrite a shared or non-regular resource: {path}')
        resolved=path.resolve()
        if not resolved.is_relative_to(lab) or resolved.is_relative_to(source) or resolved.is_relative_to(game):
            raise ValueError('Management resources must remain outside source/view directories')
        relative=path.relative_to(client) if path.is_relative_to(client) else path.relative_to(lab)
        for root in (source,game):
            original=root/relative
            if not path.exists() or not original.exists() or not os.path.samefile(path,original):continue
            # create_game_view exposes client configuration through reverse
            # leaf symlinks. It is safe to replace the private client file;
            # writing through the view or into the stock source is not.
            if root==game and game!=source and original.is_symlink() and original.resolve()==resolved:continue
            raise ValueError('Management resource aliases its source/view file')
    return lab,manifest


def configure(lab,network,*,key=None,client_only=False,tls_ca_certificate=None,server_name=None):
    if client_only:
        from .server_identity import validate_name
        server_name = validate_name(SERVER_NAME if server_name is None else server_name)
    if tls_ca_certificate is not None:
        from .certificate_authorities import validate_ca
        validate_ca(tls_ca_certificate)
        if not network.advertise_http.startswith('https://'):
            raise ValueError('A TLS CA certificate requires HTTPS')
    lab,manifest=ensure_lab(lab)
    ident=key_id(key) if key is not None else public_key(lab)[1]
    # Client files and the HTTP response are generated by the same function.
    regions=regions_config(yaml.safe_load((lab/'regions.yaml').read_bytes()),network)
    raw=yaml.safe_dump(regions,sort_keys=False,allow_unicode=True).encode()
    urls=yaml.safe_dump(dict(Location1=network.advertise_http+'/conf',
                             Location2=network.advertise_http+'/conf')).encode()
    for name in REGIONS:atomic_write(lab/'client/Data'/name,dvpl.encode(raw))
    atomic_write(lab/'client/Data/server_config_urls.yaml.dvpl',dvpl.encode(urls))
    if key is not None:atomic_write(lab/'client/Data/loginapp_wot.pubkey.dvpl',dvpl.encode(key))
    atomic_write(lab/'regions.yaml',raw)
    atomic_write(lab/'network.toml',network.toml().encode())
    manifest['server_id']=ident
    if client_only:
        manifest.update(client_only=True,server_lab=None)
        manifest['server_name'] = server_name
        manifest.pop('tls_ca_file', None)
        manifest.pop('tls_ca_certificate', None)
        if tls_ca_certificate is not None:
            manifest['tls_ca_certificate'] = tls_ca_certificate
    atomic_write(lab/'manifest.json',(json.dumps(manifest,indent=2)+'\n').encode())
    return dict(server_id=ident,network=network.document(),restart_required=True)


def configure_file(lab,path=None):
    lab=Path(lab)
    manifest=json.loads((lab/'manifest.json').read_text())
    if path is None:network=parse_config(defaults(manifest))
    else:
        path=Path(path)
        if path.stat().st_size>16384:raise ValueError('Network configuration is too large')
        network=parse_config(tomllib.loads(path.read_text()))
    return configure(lab,network)


def configure_lan(lab, address, subnet):
    from .network import ipv4
    from .storage import account_access

    lab = Path(lab)
    host = ipv4(address)
    network_scope = ipaddress.IPv4Network(subnet)
    if ipaddress.IPv4Address(host) not in network_scope:
        raise ValueError('The selected address is outside the sharing subnet')
    with account_access(lab/'private'):
        lab, manifest = ensure_lab(lab)
        if manifest.get('client_only'):
            raise ValueError('Select your server state folder')
        profile_path = lab/'connection.json'
        if profile_path.is_symlink() or (profile_path.exists() and not profile_path.is_file()):
            raise ValueError('The connection profile must be a regular file')
        current = load_network(lab, manifest)
        if current.version == 2 or manifest.get('http_tls'):
            raise ValueError('Use the network TOML to change a public HTTPS server')
        listen = {name: f'{host}:{endpoint.port}' for name, endpoint in
                  (('http', current.listen_http), ('login', current.listen_login), ('base', current.listen_base))}
        advertise = dict(listen, http='http://'+listen['http'])
        document = dict(version=1, listen=listen, advertise=advertise,
                        services={'http': advertise['http']}, access={'client_networks': [str(network_scope)]})
        result = configure(lab, parse_config(document))
        atomic_write(profile_path, (json.dumps(connection_profile(lab), indent=2)+'\n').encode())
        return result


def connection_profile(lab):
    """Public management-HTTP/export v1 contract, never a manifest dump."""
    lab,manifest=ensure_lab(lab);network=load_network(lab,manifest)
    raw,ident=public_key(lab)
    from .server_identity import server_name
    profile=dict(version=network.version,game_version=manifest['version'],exe_sha256=manifest['exe_sha256'],
                 server_id=ident,server_name=server_name(manifest),
                 advertise=network.document()['advertise'],login_public_key=raw.decode('ascii'))
    if network.advertise_http.startswith('https://') and (
            manifest.get('tls_ca_file') is not None or manifest.get('tls_ca_certificate') is not None):
        from .certificate_authorities import ca_file, validate_ca
        profile['tls_ca_certificate'] = validate_ca(ca_file(lab, manifest).read_text('ascii'))
    if len(json.dumps(profile,indent=2).encode('utf-8')) > CONNECTION_PROFILE_MAX_BYTES:
        raise ValueError('Connection profile is too large')
    return profile


def export_connection(lab,output):
    profile=connection_profile(lab)
    output=Path(output)
    # Never overwrite an existing profile or arbitrary project file.
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(profile,stream,indent=2)
    return dict(path=str(output.resolve()),server_id=profile['server_id'])


def connect_client(lab,profile_path):
    lab,manifest=ensure_lab(lab)
    path=Path(profile_path)
    if path.stat().st_size>CONNECTION_PROFILE_MAX_BYTES:raise ValueError('Connection profile is too large')
    profile=json.loads(path.read_text())
    network,raw,ca,name=validate_connection_profile(profile,manifest)
    return configure(lab,network,key=raw,client_only=True,tls_ca_certificate=ca,server_name=name)


def validate_connection_profile(profile,manifest):
    keys={'version','game_version','exe_sha256','server_id','advertise','login_public_key'}
    if (not isinstance(profile,dict) or not keys <= set(profile)
            or set(profile) - keys - {'tls_ca_certificate', 'server_name'}
            or type(profile['version']) is not int or profile['version'] not in (1,2)):
        raise ValueError('Invalid connection profile')
    from .server_identity import validate_name
    name = validate_name(profile.get('server_name', SERVER_NAME))
    if (profile['game_version'],profile['exe_sha256'])!=(manifest['version'],manifest['exe_sha256']):
        raise ValueError('Connection profile requires a different pinned client build')
    if (not isinstance(profile['login_public_key'],str) or not isinstance(profile['advertise'],dict)
            or set(profile['advertise'])!={'http','login','base'}):
        raise ValueError('Invalid public key or advertised endpoints')
    raw=profile['login_public_key'].encode('ascii')
    ca = profile.get('tls_ca_certificate')
    if 'tls_ca_certificate' in profile:
        from .certificate_authorities import validate_ca
        validate_ca(ca)
        if (not isinstance(profile['advertise']['http'], str)
                or not profile['advertise']['http'].startswith('https://')):
            raise ValueError('A TLS CA certificate requires HTTPS')
    ident=key_id(raw)
    if ident!=profile['server_id']:raise ValueError('Connection profile public key fingerprint mismatch')
    document=defaults(manifest);document['version']=profile['version'];document['advertise']=profile['advertise']
    document['services']['http']=profile['advertise']['http']
    # A client profile declares no listeners of its own. These addresses are
    # retained only for schema compatibility; serve rejects client-only labs.
    from .network import PRIVATE
    document['access']['client_networks']=[str(n) for n in PRIVATE]
    if profile['version'] == 2:
        hosts = (urlsplit(profile['advertise']['http']).hostname,
                 profile['advertise']['login'].rpartition(':')[0],
                 profile['advertise']['base'].rpartition(':')[0])
        addresses = set()
        for host in hosts:
            try:address = ipaddress.IPv4Address(host)
            except ValueError:continue
            addresses.add(f'{address}/32')
        document['access']['client_networks'].extend(sorted(addresses))
    return parse_config(document),raw,ca,name
