"""Fetch and validate goPodder playback snapshots before permitting cleanup."""

import base64
import hashlib
import json
import math
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from urllib.parse import quote, urlencode

from .config import Config


def parse_actions(payload: object, feed_url: str) -> dict[str, tuple[bool, str]]:
    """Fail closed on malformed snapshots, unknown states, and duplicate actions."""
    if not isinstance(payload, dict) or "actions" not in payload:
        raise ValueError("Sync returned no valid actions array; cleanup paused")
    if payload["actions"] is None:
        return {}  # goPodder encodes its empty slice as null.
    if not isinstance(payload["actions"], list):
        raise ValueError("Sync returned no valid actions array; cleanup paused")
    result = {}
    for action in payload["actions"]:
        if not isinstance(action, dict):
            raise ValueError("Invalid episode action; cleanup paused")
        if action.get("podcast") != feed_url:
            continue
        episode = action.get("episode")
        kind = action.get("action")
        if not isinstance(episode, str) or not episode:
            raise ValueError("Sync action has no enclosure URL; cleanup paused")
        if episode in result:
            raise ValueError("Conflicting duplicate sync actions; cleanup paused")
        if kind in {"download", "delete"}:
            continue  # Neither means played or unplayed.
        timestamp = action.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError("Missing sync action timestamp; cleanup paused")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid sync action timestamp; cleanup paused") from error
        if kind == "new":
            completed = False
            position = total = None
        elif kind == "play":
            position, total = action.get("position"), action.get("total")
            if (
                any(
                    isinstance(n, bool)
                    or not isinstance(n, (int, float))
                    or not math.isfinite(n)
                    for n in (position, total)
                )
                or position < 0
                or total <= 0
            ):
                raise ValueError("Unknown playback position/duration; cleanup paused")
            completed = position >= total
        else:
            raise ValueError("Unknown sync action; cleanup paused")
        signature = hashlib.sha256(
            json.dumps([kind, timestamp, position, total]).encode()
        ).hexdigest()
        result[episode] = (completed, signature)
    return result


def fetch_actions(config: Config) -> dict[str, tuple[bool, str]]:
    if not config.username or not config.password:
        raise ValueError("Set sync.username and sync.password; cleanup paused")
    query = urlencode({"podcast": config.feed_url})
    path = f"/api/2/episodes/{quote(config.username, safe='')}.json?{query}"
    credentials = base64.b64encode(
        f"{config.username}:{config.password}".encode()
    ).decode()
    # Direct loopback only: never inherit HTTP proxies or forward auth on redirects.
    connection = HTTPConnection("127.0.0.1", config.gopodder_port, timeout=15)
    try:
        connection.request(
            "GET", path, headers={"Authorization": "Basic " + credentials}
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"Sync HTTP {response.status}; cleanup paused")
        payload = json.load(response)
    except HTTPException as error:
        raise ValueError("Invalid sync HTTP response; cleanup paused") from error
    finally:
        connection.close()
    return parse_actions(payload, config.feed_url)
