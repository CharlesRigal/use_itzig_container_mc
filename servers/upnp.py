"""Ouverture automatique des ports sur la box (UPnP IGD), serveur par serveur.

Les redirections sont demandées avec un bail d'une heure et renouvelées par le planificateur :
si l'app s'arrête, la box les retire d'elle-même. Seul le port Minecraft est ouvert, jamais
l'interface web.
"""

import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin
from xml.sax.saxutils import escape

log = logging.getLogger("servers")

LEASE = 3600  # s ; renouvelé bien avant l'échéance
DESCRIPTION = "MC Manager - {slug}"
SERVICE_RE = re.compile(r"<serviceType>(urn:schemas-upnp-org:service:WAN(?:IP|PPP)Connection:\d)</serviceType>.*?<controlURL>(.*?)</controlURL>", re.S)

_lock = threading.Lock()
_gateway = None  # (service_type, control_url, local_ip), mis en cache
_gateway_at = 0.0


class UPnPError(Exception):
    pass


def _discover():
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900", 'MAN: "ssdp:discover"', "MX: 2",
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1", "", "",
    ]).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.sendto(msg, ("239.255.255.250", 1900))
        try:
            while True:
                data, (ip, _) = s.recvfrom(4096)
                m = re.search(r"^location:\s*(\S+)", data.decode(errors="replace"), re.I | re.M)
                if m:
                    location = m.group(1)
                    break
        except socket.timeout:
            raise UPnPError("aucune box compatible UPnP trouvée sur le réseau (UPnP désactivé sur la box ?)") from None
    desc = urllib.request.urlopen(location, timeout=5).read().decode(errors="replace")
    m = SERVICE_RE.search(desc)
    if not m:
        raise UPnPError("la box répond en UPnP mais ne propose pas l'ouverture de ports")
    # Adresse locale du Pi vue depuis la box (la bonne interface si plusieurs).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((ip, 1900))
        local_ip = s.getsockname()[0]
    return m.group(1), urljoin(location, m.group(2)), local_ip


def _gw():
    global _gateway, _gateway_at
    with _lock:
        if _gateway is None or time.monotonic() - _gateway_at > 3600:
            _gateway, _gateway_at = _discover(), time.monotonic()
        return _gateway


def _soap(action, **args):
    global _gateway
    service, url, _ = _gw()
    body = "".join(f"<New{k}>{escape(str(v))}</New{k}>" for k, v in args.items())
    envelope = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>'
    )
    req = urllib.request.Request(url, envelope.encode(), {
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"',
    })
    try:
        return urllib.request.urlopen(req, timeout=5).read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        code = re.search(r"<errorCode>(\d+)<", text)
        desc = re.search(r"<errorDescription>(.*?)<", text)
        raise UPnPError(f"{action} refusé par la box ({code.group(1) if code else exc.code} {desc.group(1) if desc else ''})".strip()) from None
    except OSError as exc:
        _gateway = None  # la box a peut-être changé d'adresse : nouvelle découverte la prochaine fois
        raise UPnPError(f"box injoignable : {exc}") from None


def _val(xml, name):
    m = re.search(rf"<New{name}>(.*?)</New{name}>", xml)
    return m.group(1) if m else ""


def external_ip():
    return _val(_soap("GetExternalIPAddress"), "ExternalIPAddress")


def mappings():
    out = []
    for i in range(256):
        try:
            xml = _soap("GetGenericPortMappingEntry", PortMappingIndex=i)
        except UPnPError:
            break  # fin de liste (erreur 713)
        out.append({
            "port": int(_val(xml, "ExternalPort") or 0), "protocol": _val(xml, "Protocol"),
            "client": _val(xml, "InternalClient"), "internal_port": int(_val(xml, "InternalPort") or 0),
            "description": _val(xml, "PortMappingDescription"),
        })
    return out


def open_port(server):
    _, _, local_ip = _gw()
    _soap(
        "AddPortMapping", RemoteHost="", ExternalPort=server.port, Protocol="TCP",
        InternalPort=server.port, InternalClient=local_ip, Enabled=1,
        PortMappingDescription=DESCRIPTION.format(slug=server.slug), LeaseDuration=LEASE,
    )


def close_port(port):
    _soap("DeletePortMapping", RemoteHost="", ExternalPort=port, Protocol="TCP")


def sync(servers=None):
    """Ouvre les ports des serveurs démarrés qui le demandent, ferme les autres.

    Ne touche qu'aux redirections créées par MC Manager vers cette machine.
    Enregistre sur chaque serveur l'adresse publique ou l'erreur rencontrée.
    """
    from . import podman
    from .models import Server

    servers = list(servers if servers is not None else Server.objects.all())
    wanted = [s for s in servers if s.upnp_enabled]
    if not wanted and not any(s.upnp_address or s.upnp_error for s in servers):
        return  # rien à faire : pas de trafic UPnP inutile
    running = {n for c in podman.ps_json() if c.get("State") == "running" for n in c.get("Names") or []}
    try:
        _, _, local_ip = _gw()
        ip = external_ip()
        ours = [m for m in mappings() if m["description"].startswith("MC Manager") and m["client"] == local_ip]
    except UPnPError as exc:
        for s in wanted:
            _save(s, "", str(exc))
        return
    keep = set()
    for s in wanted:
        if s.container_name not in running:
            _save(s, "", "")
            continue
        try:
            open_port(s)
            keep.add(s.port)
            _save(s, f"{ip}:{s.port}", "")
        except UPnPError as exc:
            hint = " Le port est peut-être déjà redirigé vers une autre machine." if "718" in str(exc) else ""
            _save(s, "", f"{exc}.{hint}")
    for m in ours:
        if m["port"] not in keep and m["protocol"] == "TCP":
            try:
                close_port(m["port"])
                log.info("UPnP : port %s fermé", m["port"])
            except UPnPError:
                log.warning("UPnP : impossible de fermer le port %s", m["port"])
    for s in servers:
        if not s.upnp_enabled:
            _save(s, "", "")


def _save(server, address, error):
    if (server.upnp_address, server.upnp_error) != (address, error):
        type(server).objects.filter(pk=server.pk).update(upnp_address=address, upnp_error=error)
        server.upnp_address, server.upnp_error = address, error
        if address:
            log.info("UPnP : %s ouvert sur %s", server.slug, address)


def sync_quietly(servers=None):
    try:
        sync(servers)
    except Exception:  # le réseau ne doit jamais faire échouer une action
        log.exception("UPnP")
