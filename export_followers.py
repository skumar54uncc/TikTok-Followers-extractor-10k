#!/usr/bin/env python3
"""Export a public TikTok follower list.

One row per follower: display name, profile link, follower count.
Pages TikTok's public profile list, writes CSV as it goes, and can resume
after a stop. The following list is requested once; TikTok returns it only
when that profile has made following visible.

TikTok's public follower list stops after the most recent ~10,000 accounts.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests

USERNAME = ""
PAGE_SIZE = 30
SCENE_FOLLOWERS = 67
SCENE_FOLLOWING = 21
PROFILE_URL = ""
LIST_URL = "https://www.tiktok.com/api/user/list/"

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
CSV_PATH = OUT_DIR / "followers.csv"
FOLLOWING_PATH = OUT_DIR / "following.csv"
STATE_PATH = OUT_DIR / "state.json"
LOG_PATH = OUT_DIR / "export.log"

IMPERSONATE = "chrome131"
MAX_ATTEMPTS = 6
BASE_DELAY = 0.15


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "username": USERNAME,
            "sec_uid": None,
            "min_cursor": 0,
            "exported": 0,
            "total": None,
            "done": False,
            "following_status": None,
        }
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def known_links(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["profile_link"] for row in csv.DictReader(handle) if row.get("profile_link")}


class TikTokSession:
    def __init__(self) -> None:
        self.http = requests.Session(impersonate=IMPERSONATE)

    def warm(self) -> str:
        response = self.http.get(PROFILE_URL, timeout=30)
        response.raise_for_status()
        match = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            response.text,
        )
        if not match:
            raise RuntimeError("Profile page did not include user data.")
        payload = json.loads(match.group(1))
        detail = payload["__DEFAULT_SCOPE__"]["webapp.user-detail"]
        if detail.get("statusCode") not in (0, None):
            raise RuntimeError(f"Profile lookup failed: {detail.get('statusCode')}")
        user = detail["userInfo"]["user"]
        stats = detail["userInfo"]["stats"]
        if user.get("uniqueId") != USERNAME:
            raise RuntimeError(f"Unexpected profile: {user.get('uniqueId')}")
        if user.get("privateAccount") or user.get("secret"):
            raise RuntimeError("This account is private. Follower export is not available.")
        log(
            f"profile @{USERNAME}: followers={stats.get('followerCount')} "
            f"following={stats.get('followingCount')} "
            f"following_visibility={user.get('followingVisibility')}"
        )
        return user["secUid"]

    def user_list(self, sec_uid: str, scene: int, min_cursor: int) -> dict:
        response = self.http.get(
            LIST_URL,
            params={
                "secUid": sec_uid,
                "count": str(PAGE_SIZE),
                "minCursor": str(min_cursor),
                "maxCursor": "0",
                "scene": str(scene),
                "aid": "1988",
                "app_name": "tiktok_web",
                "device_platform": "web_pc",
            },
            headers={
                "Referer": PROFILE_URL,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


def rows_from_page(page: dict) -> list[dict]:
    rows = []
    for entry in page.get("userList") or []:
        user = entry.get("user") or {}
        stats = entry.get("stats") or {}
        unique_id = (user.get("uniqueId") or "").strip()
        if not unique_id:
            continue
        nickname = (user.get("nickname") or unique_id).strip()
        follower_count = stats.get("followerCount")
        if follower_count is None:
            follower_count = (entry.get("statsV2") or {}).get("followerCount", "")
        rows.append(
            {
                "display_name": nickname,
                "profile_link": f"https://www.tiktok.com/@{unique_id}",
                "follower_count": follower_count,
            }
        )
    return rows


def open_csv(path: Path):
    new_file = not path.exists() or path.stat().st_size == 0
    handle = path.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(
        handle,
        fieldnames=["display_name", "profile_link", "follower_count"],
    )
    if new_file:
        writer.writeheader()
        handle.flush()
    return handle, writer


def fetch_page(client: TikTokSession, sec_uid: str, scene: int, cursor: int) -> dict:
    delay = BASE_DELAY
    last_error = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            page = client.user_list(sec_uid, scene, cursor)
        except Exception as error:  # network, decode, HTTP
            last_error = str(error)
            log(f"request failed attempt {attempt}: {last_error}")
            client.warm()
            time.sleep(delay + random.random() * 0.2)
            delay = min(delay * 2, 20)
            continue

        status = page.get("statusCode")
        if status in (0, None) and page.get("userList") is not None:
            return page
        # 10222 is a privacy block, not a transient failure.
        if status == 10222:
            return page
        last_error = f"statusCode={status}"
        log(f"list status {status} attempt {attempt} cursor={cursor}")
        if attempt >= 2:
            client.warm()
        time.sleep(delay + random.random() * 0.2)
        delay = min(delay * 2, 20)
    raise RuntimeError(f"Gave up at cursor {cursor}: {last_error}")


def export_following(client: TikTokSession, sec_uid: str, state: dict) -> None:
    if state.get("following_status") == "done":
        log(f"following already saved at {FOLLOWING_PATH.name}")
        return
    page = fetch_page(client, sec_uid, SCENE_FOLLOWING, 0)
    status = page.get("statusCode")
    if status == 10222 or not page.get("userList"):
        state["following_status"] = "hidden"
        save_state(state)
        log(
            "following list is hidden on this profile (TikTok status 10222). "
            "Only the account owner can view accounts they follow."
        )
        return

    seen = known_links(FOLLOWING_PATH)
    handle, writer = open_csv(FOLLOWING_PATH)
    cursor = 0
    try:
        while True:
            if cursor != 0:
                page = fetch_page(client, sec_uid, SCENE_FOLLOWING, cursor)
            batch = rows_from_page(page)
            wrote = 0
            for row in batch:
                if row["profile_link"] in seen:
                    continue
                writer.writerow(row)
                seen.add(row["profile_link"])
                wrote += 1
            handle.flush()
            next_cursor = page.get("minCursor")
            has_more = bool(page.get("hasMore")) and bool(batch)
            log(f"following +{wrote} total={len(seen)} cursor={next_cursor} has_more={has_more}")
            if not has_more or next_cursor in (None, cursor) or not batch:
                break
            cursor = int(next_cursor)
            time.sleep(BASE_DELAY + random.random() * 0.1)
    finally:
        handle.close()
    state["following_status"] = "done"
    save_state(state)


def export_followers(client: TikTokSession, sec_uid: str, state: dict) -> None:
    if state.get("done"):
        log(f"followers already complete: {state.get('exported')} rows in {CSV_PATH.name}")
        return

    seen = known_links(CSV_PATH)
    state["exported"] = len(seen)
    cursor = int(state.get("min_cursor") or 0)
    handle, writer = open_csv(CSV_PATH)
    stalled = 0
    try:
        while True:
            page = fetch_page(client, sec_uid, SCENE_FOLLOWERS, cursor)
            if page.get("total") is not None:
                state["total"] = page.get("total")
            batch = rows_from_page(page)
            wrote = 0
            for row in batch:
                if row["profile_link"] in seen:
                    continue
                writer.writerow(row)
                seen.add(row["profile_link"])
                wrote += 1
            handle.flush()

            next_cursor = page.get("minCursor")
            has_more = bool(page.get("hasMore"))
            state["exported"] = len(seen)
            state["min_cursor"] = next_cursor if next_cursor is not None else cursor
            save_state(state)

            if state["exported"] % 300 < PAGE_SIZE or wrote == 0:
                total = state.get("total") or "?"
                log(f"followers {state['exported']}/{total} cursor={next_cursor} wrote={wrote}")

            if not has_more or not batch:
                state["done"] = True
                total = state.get("total")
                if isinstance(total, int) and state["exported"] + PAGE_SIZE < total:
                    state["stopped_reason"] = "public_list_ended"
                    log(
                        f"public follower list ended at {state['exported']} rows. "
                        f"The profile total is {total}. TikTok stopped this list there "
                        f"(next cursor {next_cursor})."
                    )
                else:
                    state["stopped_reason"] = "complete"
                    log(f"followers finished: {state['exported']} rows")
                save_state(state)
                return

            if next_cursor in (None, cursor) or wrote == 0:
                stalled += 1
                log(f"page made no progress ({stalled}) cursor={cursor} next={next_cursor}")
                if stalled >= 3:
                    state["done"] = True
                    save_state(state)
                    log(f"stopped after repeated stalls at {state['exported']} rows")
                    return
            else:
                stalled = 0
                cursor = int(next_cursor)

            time.sleep(BASE_DELAY + random.random() * 0.1)
    finally:
        handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a public TikTok follower list (about 10,000 accounts)."
    )
    parser.add_argument("username", help="TikTok username, with or without @")
    return parser.parse_args()


def configure(username: str) -> None:
    global USERNAME, PROFILE_URL, CSV_PATH, FOLLOWING_PATH
    USERNAME = username.lstrip("@").strip()
    if not USERNAME:
        raise SystemExit("Username is required.")
    PROFILE_URL = f"https://www.tiktok.com/@{USERNAME}"
    CSV_PATH = OUT_DIR / f"{USERNAME}_followers.csv"
    FOLLOWING_PATH = OUT_DIR / f"{USERNAME}_following.csv"


def main() -> int:
    configure(parse_args().username)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    client = TikTokSession()
    sec_uid = client.warm()
    if state.get("sec_uid") not in (None, sec_uid):
        raise RuntimeError("Saved secUid does not match this profile. Remove output/state.json to start over.")
    state["sec_uid"] = sec_uid
    state["username"] = USERNAME
    save_state(state)

    export_following(client, sec_uid, state)
    export_followers(client, sec_uid, state)
    log(f"csv: {CSV_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("interrupted; rerun the same command to resume")
        raise SystemExit(130)
    except Exception as error:
        log(f"error: {error}")
        raise
