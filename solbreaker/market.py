"""Empty local WebMarket used by the native hangar synchronization."""
import json
from .build_info import SERVER_NAME, LEGACY_SERVER_NAME


def configure_market(config, origin):
    """Route every GGM prerequisite to this server, including existing labs."""
    names = set(config['Regions'])
    if names not in ({SERVER_NAME}, {LEGACY_SERVER_NAME}):
        raise ValueError('Expected the isolated Solbreaker region')
    region = config['Regions'][next(iter(names))]
    ggm = region.setdefault("ggmCredits", {})
    for key in ("tokenUrl", "payUrl", "frontierUrl", "notificationCenterUrl"):
        ggm[key] = origin + "/market/"
    ggm.update(client_id="solbreaker-local", client_secret="local-only")


def market_response(accounts, method, path, body, headers):
    if not path.startswith("/market/"):
        return None
    if method == "POST" and path == "/market/api/v1/oauth/token/":
        try:
            request = json.loads(body)
        except (ValueError, UnicodeError):
            return 400, {"error": "invalid_request"}
        # A password login already has an account access token. Token2
        # reconnects obtain a fresh SPA token through Account base method 2.
        if not isinstance(request, dict) or not accounts.valid(accounts.access, request.get("token")):
            return 401, {"error": "invalid_token"}
        token = accounts.issue(accounts.market, accounts.access[request["token"]].account_id)
        if token is None:
            return 429, {"error": "local_token_capacity"}
        return 200, {"token_type": "Bearer", "id_token": token,
                     "expires_in": 3600}
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not accounts.valid(accounts.market, token):
        return 401, {"error": "invalid_token"}
    if method == "GET" and path == "/market/api/v1/batch/":
        return 200, {"data": {"gifts": [], "products": [], "categories": [], "notifications": []}}
    if method == "GET" and path == "/market/api/v1/notifications/":
        return 200, {"data": {"notifications": []}}
    return 501, {"error": "Local market operation is not implemented"}
