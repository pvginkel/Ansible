#!/usr/bin/env python3
"""
Send an Android/iOS push notification through Home Assistant's
companion-app notifier: notify.mobile_app_pieter_telefoon.

Copied from /work/IaCAgent/bin/send_message.py — stdlib only, no extra
package dependency. Used by the AI-workflow skills (run-slice, triage) and
for ad-hoc "let me know" notifications.

Usage:
    send_message.py --title "title" --channel "channel" "message"
    send_message.py "message only"

Environment variables (typically exported by iac-impl from
/etc/iac/secrets.yaml):
    HA_URL    e.g. http://homeassistant.home:8123
    HA_TOKEN  long-lived access token from Home Assistant profile
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

SERVICE_URL_PATH = "/api/services/notify/mobile_app_pieter_telefoon"
TIMEOUT_SECONDS = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a push notification via Home Assistant."
    )
    parser.add_argument("--title", help="Optional notification title.")
    parser.add_argument("--channel", help="Optional Android notification channel.")
    parser.add_argument("message", help="Notification message text.")
    return parser.parse_args()


def build_payload(message: str, title: str | None, channel: str | None) -> dict[str, Any]:
    inner_data: dict[str, Any] = {
        "ttl": 0,
        "priority": "high",
        "importance": "high",
    }
    if channel:
        inner_data["channel"] = channel
    payload: dict[str, Any] = {
        "message": message,
        "data": inner_data,
    }
    if title:
        payload["title"] = title
    return payload


def main() -> int:
    args = parse_args()

    ha_url = os.environ.get("HA_URL", "").strip().rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "").strip()

    if not ha_url:
        print("Error: HA_URL environment variable is not set.", file=sys.stderr)
        return 2
    if not ha_token:
        print("Error: HA_TOKEN environment variable is not set.", file=sys.stderr)
        return 2

    payload = build_payload(args.message, args.title, args.channel)
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=f"{ha_url}{SERVICE_URL_PATH}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            if status >= 400:
                print(f"Home Assistant returned HTTP {status}", file=sys.stderr)
                return 1
    except urllib.error.HTTPError as e:
        print(f"Home Assistant returned HTTP {e.code}: {e.reason}", file=sys.stderr)
        try:
            print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        except Exception:
            pass
        return 1
    except urllib.error.URLError as e:
        print(f"Could not reach Home Assistant at {ha_url}: {e.reason}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
