#!/usr/bin/env python3
"""
Send an Android/iOS push notification through Home Assistant's companion app
notifier: notify.mobile_app_pieter_telefoon

Usage:
    send_message.py --title "title" --channel "channel" "message"
    send_message.py "message only"

Environment variables:
    HA_URL   e.g. http://homeassistant.local:8123
    HA_TOKEN long-lived access token from Home Assistant profile
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import requests


SERVICE_URL_PATH = "/api/services/notify/mobile_app_pieter_telefoon"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a push notification via Home Assistant."
    )
    parser.add_argument(
        "--title",
        help="Optional notification title.",
    )
    parser.add_argument(
        "--channel",
        help="Optional Android notification channel.",
    )
    parser.add_argument(
        "message",
        help="Notification message text.",
    )
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

    url = f"{ha_url}{SERVICE_URL_PATH}"
    payload = build_payload(args.message, args.title, args.channel or "claude")

    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    if response.ok:
        print("Notification sent.")
        return 0

    print(
        f"Home Assistant returned HTTP {response.status_code}: {response.text}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
