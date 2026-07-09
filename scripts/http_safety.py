import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def public_host_addresses(hostname, port):
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"URL host cannot be resolved: {hostname}") from exc
    if not addresses:
        raise ValueError(f"URL host cannot be resolved: {hostname}")
    for _family, _type, _proto, _canonname, sockaddr in addresses:
        address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if not address.is_global:
            raise ValueError(f"URL host resolves to a non-public address: {hostname}")
    return True


def _validate_http_url(
    value,
    *,
    allowed_hosts=None,
    allow_local_http=False,
    resolved_origin=None,
):
    parsed = urlparse(str(value))
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if parsed.scheme != "https":
        if not (allow_local_http and parsed.scheme == "http" and hostname in LOCAL_HOSTS):
            raise ValueError(f"HTTPS URL required: {value}")
    if not hostname:
        raise ValueError(f"URL host is required: {value}")
    if allowed_hosts:
        allowed = tuple(str(host).lower().rstrip(".") for host in allowed_hosts)
        if not any(hostname == host or hostname.endswith("." + host) for host in allowed):
            raise ValueError(f"URL host is not allowed: {hostname}")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"URL port is invalid: {value}") from exc
    origin = (parsed.scheme.lower(), hostname, port)
    if not (allow_local_http and hostname in LOCAL_HOSTS) and origin != resolved_origin:
        public_host_addresses(hostname, port)
    return str(value), origin


def validate_http_url(value, *, allowed_hosts=None, allow_local_http=False):
    validated, _origin = _validate_http_url(
        value,
        allowed_hosts=allowed_hosts,
        allow_local_http=allow_local_http,
    )
    return validated


class ValidatingRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator):
        super().__init__()
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_validated_url(
    request_or_url,
    *,
    timeout,
    allowed_hosts=None,
    allow_local_http=False,
):
    resolved_origin = None

    def validator(value):
        nonlocal resolved_origin
        validated, resolved_origin = _validate_http_url(
            value,
            allowed_hosts=allowed_hosts,
            allow_local_http=allow_local_http,
            resolved_origin=resolved_origin,
        )
        return validated

    initial_url = getattr(request_or_url, "full_url", request_or_url)
    validator(initial_url)
    opener = build_opener(ValidatingRedirectHandler(validator))
    response = opener.open(request_or_url, timeout=timeout)
    try:
        validator(response.geturl())
    except Exception:
        response.close()
        raise
    return response
