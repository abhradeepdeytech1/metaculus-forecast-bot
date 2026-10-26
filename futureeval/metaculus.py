from datetime import datetime, timezone

from .http import request
from .method import Question, payload

BASE = "https://www.metaculus.com/api"


def still_open(q):
    if q.status != "open" or not q.close_time:
        return False
    close = datetime.fromisoformat(q.close_time.replace("Z", "+00:00"))
    if close.tzinfo is None:
        return False
    return close > datetime.now(timezone.utc)


def already_forecasted(post):
    question = post.get("question") or {}
    forecasts = question.get("my_forecasts")
    if isinstance(forecasts, dict):
        latest = forecasts.get("latest") or {}
        return bool(forecasts.get("history")) or latest.get("forecast_values") is not None
    return bool(forecasts or question.get("my_forecast") or post.get("user_forecast"))


class Metaculus:
    def __init__(self, token=None, send=request, *, allow_publish=False):
        self.token, self.send = token, send
        self.allow_publish = allow_publish

    def get_post(self, post_id):
        return self.send("GET", f"{BASE}/posts/{int(post_id)}/", token=self.token, scheme="Token")

    def list_open(self, tournament):
        offset = 0
        while True:
            response = self.send("GET", BASE + "/posts/", token=self.token, scheme="Token", params={
                "tournaments": tournament, "statuses": "open", "limit": 100, "offset": offset,
                "order_by": "scheduled_close_time", "forecast_type": "binary,numeric,discrete,multiple_choice",
                "include_description": "true"})
            posts = response["results"]
            yield from posts
            if not response.get("next") or not posts:
                break
            offset += len(posts)

    def publish(self, q, prediction, comment):
        if not self.token or not self.allow_publish:
            raise ValueError("Publishing is disabled for this client")
        # The official bulk endpoint validates and saves both parts atomically.
        # Never fall back to separate posts, and never retry an uncertain response.
        return self.send("POST", BASE + "/questions/bulk-forecast-comment/", token=self.token, scheme="Token",
                         payload={"forecasts": [payload(q, prediction)],
                                  "comments": [{"on_post": q.post_id, "text": comment,
                                                "is_private": True, "included_forecast": True}]})
