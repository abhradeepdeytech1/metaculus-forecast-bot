import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request


log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}


class RemoteError(RuntimeError):
    """Never include server response text, headers, or credentials in errors."""


def request(method, url, *, token=None, scheme="Bearer", payload=None, params=None, sleep=time.sleep):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "futureeval-autonomous-bot/1"}
    if token:
        headers["Authorization"] = f"{scheme} {token}"
    body = None
    if payload is not None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    # Reads are free and idempotent, so rate limits and outages get a bounded backoff.
    # Writes never retry: an uncertain POST may already have taken effect.
    attempts = 4 if method == "GET" else 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as error:
            if error.code not in RETRY_STATUS or attempt + 1 == attempts:
                raise RemoteError(f"Remote request returned HTTP {error.code}") from None
            wait, detail = retry_after(error.headers), f"HTTP {error.code}"
        except Exception:
            if attempt + 1 == attempts:
                raise RemoteError("Remote request failed after retries" if attempts > 1
                                  else "Remote request failed; automatic retry is disabled") from None
            wait, detail = None, "network error"
        wait = min(60, wait if wait is not None else 5 * 3 ** attempt)
        log.warning("remote_retry %s attempt=%s wait_s=%s", detail, attempt + 1, wait)
        sleep(wait)


def retry_after(headers):
    try:
        return max(0, int((headers or {}).get("Retry-After", "")))
    except (TypeError, ValueError):
        return None
