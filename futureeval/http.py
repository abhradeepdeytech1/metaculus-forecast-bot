import json
import urllib.error
import urllib.parse
import urllib.request


class RemoteError(RuntimeError):
    """Never include server response text, headers, or credentials in errors."""


def request(method, url, *, token=None, scheme="Bearer", payload=None, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "futureeval-autonomous-bot/1"}
    if token:
        headers["Authorization"] = f"{scheme} {token}"
    body = None
    if payload is not None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        raise RemoteError(f"Remote request returned HTTP {error.code}") from None
    except Exception:
        raise RemoteError("Remote request failed; automatic retry is disabled") from None
