"""Configuration and roles for the orders agent.

The gateway sends the caller's identity and its application role on every request. This service
never sees Slack. `orders.use` is the gateway's decision (may this person use the assistant).
What each person may SEE is decided here, in authorization.py, from the secret twl-orders-authz.
"""

import base64
import json
import os
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "the-rabbit-hole-509200")
SHOPIFY_SECRET = os.environ.get("SHOPIFY_CONFIG_SECRET", "twl-shopify-config")
CACHE_SECONDS = 300

USE_ROLE = "orders.use"  # the gateway's role: may this person use the orders assistant at all
AUTHZ_SECRET = os.environ.get("ORDERS_AUTHZ_SECRET", "twl-orders-authz")  # who sees what

_cache = {"value": None, "loaded_at": 0.0}


def read_secret_json(secret_name):
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = AuthorizedSession(credentials)
    response = session.get(
        (
            "https://secretmanager.googleapis.com/v1/projects/"
            f"{PROJECT}/secrets/{secret_name}/versions/latest:access"
        ),
        timeout=15,
    )
    response.raise_for_status()
    encoded = response.json()["payload"]["data"]
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


def get_shopify_config():
    """Secret twl-shopify-config. Either a fixed token:

        {"shop": "your-store.myshopify.com", "access_token": "shpat_...", "api_version": "2026-07"}

    or the client credentials of a Dev Dashboard app (a token is fetched and cached):

        {"shop": "your-store.myshopify.com", "client_id": "...", "client_secret": "...",
         "api_version": "2026-07"}
    """
    now = time.time()
    if _cache["value"] is None or now - _cache["loaded_at"] > CACHE_SECONDS:
        config = read_secret_json(SHOPIFY_SECRET)

        shop = str(config.get("shop", "")).strip().lower()
        shop = shop.removeprefix("https://").removeprefix("http://").rstrip("/")
        if not shop.endswith(".myshopify.com"):
            raise RuntimeError(f"{SHOPIFY_SECRET}: 'shop' must be your .myshopify.com domain")

        has_token = bool(config.get("access_token"))
        has_client = bool(config.get("client_id") and config.get("client_secret"))
        if not has_token and not has_client:
            raise RuntimeError(
                f"{SHOPIFY_SECRET} needs either access_token, or client_id and client_secret"
            )

        config["shop"] = shop
        config.setdefault("api_version", "2026-07")
        _cache["value"] = config
        _cache["loaded_at"] = now
    return _cache["value"]
