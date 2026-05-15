import logging
import os
import socket

log = logging.getLogger(__name__)


def get_lan_ip() -> str:
    """Pick the IPv4 address of the interface Windows would use to reach the LAN.

    The UDP-connect trick picks the default-route interface without actually
    sending a packet. This is the same interface the Chromecast's mDNS resolves
    from, so rewritten HLS URLs that use this IP will be reachable from the cast
    device. If a VPN adapter is active, this may return the VPN's tunnel IP —
    `CASTBOOSTER_LAN_IP` env var overrides for that case.
    """
    override = os.environ.get("CASTBOOSTER_LAN_IP", "").strip()
    if override:
        log.info("get_lan_ip: using CASTBOOSTER_LAN_IP override = %s", override)
        return override

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = ""
    finally:
        s.close()

    if ip and not ip.startswith("127."):
        log.info("get_lan_ip: detected via udp-connect = %s", ip)
        return ip

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cand = info[4][0]
            if not cand.startswith("127."):
                log.info("get_lan_ip: fallback via getaddrinfo = %s", cand)
                return cand
    except socket.gaierror:
        pass

    log.warning("get_lan_ip: falling back to 127.0.0.1 — Chromecast will NOT reach us")
    return "127.0.0.1"
