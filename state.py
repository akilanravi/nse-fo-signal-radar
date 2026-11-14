"""Persistent day/group checkpoints on the separate scanner-state branch."""
import asyncio
import base64
import json
import random
from datetime import datetime, timedelta

import aiohttp

import config as cfg


class StateError(RuntimeError):
    pass


class State:
    def __init__(self, session, repo, token, day, group, dry):
        self.session = session
        self.root = f"https://api.github.com/repos/{repo}"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        self.path = f"state/{day}/{group}.json"
        self.dry = dry
        self.doc = {"symbols": {}, "notices": {}, "counts": {"CE": 0, "PE": 0, "EXIT": 0},
                    "scans": 0, "missing": 0}

    async def api(self, method, endpoint, **kwargs):
        async with self.session.request(method, self.root + endpoint, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=8), **kwargs) as r:
            try:
                body = await r.json(content_type=None)
            except ValueError:
                body = {}
            return r.status, body

    async def branch(self, sha):
        status, _ = await self.api("GET", "/git/ref/heads/" + cfg.STATE_BRANCH)
        if status == 404:
            status, _ = await self.api("POST", "/git/refs", json={
                "ref": "refs/heads/" + cfg.STATE_BRANCH, "sha": sha})
            if status == 422:  # may have appeared concurrently
                status, _ = await self.api("GET", "/git/ref/heads/" + cfg.STATE_BRANCH)
        if status not in {200, 201}:
            raise StateError(f"Cannot initialize state branch (HTTP {status})")

    async def get(self):
        return await self.api("GET", "/contents/" + self.path, params={"ref": cfg.STATE_BRANCH})

    async def load(self):
        if self.dry:
            return
        status, body = await self.get()
        if status == 200:
            self.doc = json.loads(base64.b64decode(body["content"]))
        elif status != 404:
            raise StateError(f"Cannot restore state (HTTP {status})")

    async def save(self):
        if self.dry:
            return
        # Contents writes from different groups may conflict at the branch head.
        payload = {"branch": cfg.STATE_BRANCH, "message": "Checkpoint scanner state",
                   "content": base64.b64encode(json.dumps(self.doc, separators=(",", ":")).encode()).decode()}
        for attempt in range(7):
            try:
                status, body = await self.get()
                if status not in {200, 404}:
                    raise StateError(f"Cannot read state (HTTP {status})")
                if status == 200:
                    payload["sha"] = body["sha"]
                status, _ = await self.api("PUT", "/contents/" + self.path, json=payload)
                if status in {200, 201}:
                    return
                if status not in {403, 409, 422, 429} and status < 500:
                    raise StateError(f"Cannot save state (HTTP {status})")
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(random.uniform(.4, 1.2) * (1 + attempt))
        raise StateError("Could not checkpoint scanner-state after retries")


def transition(previous, result, close, cooldown=cfg.COOLDOWN_MINUTES, *, exit_flip=None):
    """Return new state and events. A new setup may requalify after dropping below 7."""
    old = dict(previous or {})
    stamp = close.isoformat()
    if old.get("bar", "") >= stamp:
        return old, []
    side, opened = result.side(), old.get("open", "")
    events = []
    # None preserves the legacy API for other callers. The live scanner always
    # supplies the fast flip, including 0 when its direction did not change.
    exit_direction = result.st if exit_flip is None else exit_flip
    exited = ""
    if opened and ((opened == "CE" and exit_direction < 0) or (opened == "PE" and exit_direction > 0)):
        exited = opened
        events.append("EXIT " + opened)
        opened = ""
    if side and side != exited:
        checks = result.ce if side == "CE" else result.pe
        count = sum(x is True for x in checks)
        previous_alert = old.get("alert_at")
        elapsed = previous_alert and close - datetime.fromisoformat(previous_alert) >= timedelta(minutes=cooldown)
        if (old.get("active") != side or count > old.get("tier", 0)
                or (cooldown > 0 and elapsed)):
            events.append("BUY " + side)
            old.update(alert_at=stamp, tier=count, open=side)
            opened = side
    elif not side:
        old["tier"] = 0
    old.update(bar=stamp, active=side, open=opened)
    return old, events
