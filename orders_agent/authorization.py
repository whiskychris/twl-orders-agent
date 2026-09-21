"""Who may see what. Decided here, in code, before any Shopify data is fetched.

Identity comes from the gateway (a trusted, private caller): `user.user_id` is a stable TWL id such
as "twl:chris-ross". This service never accepts an identity from anywhere else, and never from
message text.

What that person may see comes from the secret `twl-orders-authz`, which this service owns:

    {"users": {"twl:chris-ross": {"name": "Chris Ross",
                                  "capabilities": ["orders", "products", "inventory", "customers"]}}}

Capabilities:
    orders     orders, order totals, order counts, fulfilment and tracking
    products   products and variants, including selling prices
    inventory  stock quantities and locations (also the stock figures on products)
    customers  customer details: names, emails, phones, addresses, the order note, customer search

The rules:
- Deny by default. Not listed means no access. If the permissions cannot be read, nothing is looked up.
- The model never decides access. Tools for a capability the user lacks are not registered, and the
  Shopify queries never request the fields (see sources/shopify.py).
- Customer details are never shown outside a direct message, because a reply in a channel is visible
  to everyone in it. The gateway tells us which kind of conversation this is. Anything unclear is
  treated as a channel.
"""

import json
import logging
import time
from dataclasses import dataclass

from .config import AUTHZ_SECRET, CACHE_SECONDS, USE_ROLE, read_secret_json

CAPABILITIES = ("orders", "products", "inventory", "customers")

log = logging.getLogger("orders_agent.authz")

_cache = {"value": None, "loaded_at": 0.0}


class AuthorizationError(Exception):
    """The caller is not allowed to use this assistant. The message is safe to show them."""


class AuthorizationUnavailable(Exception):
    """Permissions could not be read. The request must fail closed."""


@dataclass(frozen=True)
class AuthContext:
    request_id: str
    user_id: str
    name: str
    source: str
    visibility: str  # "dm" or "channel"
    capabilities: frozenset
    withheld: tuple = ()  # capabilities the person has, but that are not shown in this conversation

    def has(self, capability):
        return capability in self.capabilities

    def describe(self):
        """A short statement for the model, so it phrases answers well. It does NOT grant or
        limit anything. The tools and queries are what enforce access."""
        can = ", ".join(sorted(self.capabilities)) or "nothing"
        cannot = [c for c in CAPABILITIES if c not in self.capabilities]
        where = "a direct message" if self.visibility == "dm" else "a shared channel"
        text = f"This conversation is {where}. Access for this user: can see {can}."
        if cannot:
            text += " Cannot see: " + ", ".join(cannot) + "."
        if "customers" in self.withheld:
            text += (
                " Customer details are withheld because this is a shared channel. They are"
                " available in a direct message."
            )
        return text


# --- configuration ------------------------------------------------------------------------


def get_authz_config():
    now = time.time()
    if _cache["value"] is None or now - _cache["loaded_at"] > CACHE_SECONDS:
        try:
            config = read_secret_json(AUTHZ_SECRET)
        except Exception as exc:  # noqa: BLE001 - any failure means fail closed
            raise AuthorizationUnavailable(f"could not read {AUTHZ_SECRET}: {exc}") from exc
        users = config.get("users") if isinstance(config, dict) else None
        if not isinstance(users, dict):
            raise AuthorizationUnavailable(f"{AUTHZ_SECRET} has no 'users' map")
        _cache["value"] = users
        _cache["loaded_at"] = now
    return _cache["value"]


def _granted(entry):
    raw = entry.get("capabilities") if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(item).strip().lower() for item in raw) & frozenset(CAPABILITIES)


# --- audit --------------------------------------------------------------------------------


def audit(event, request_id, user_id="", source="", visibility="", **fields):
    """One structured log line per decision. Records who, what kind of thing, and the outcome.
    Never records tokens, customer data or what was asked."""
    log.info(
        json.dumps(
            {
                "audit": event,
                "request_id": request_id,
                "user_id": user_id,
                "source": source,
                "visibility": visibility,
                **fields,
            },
            sort_keys=True,
        )
    )


def audit_tool(ctx, tool, capability, allowed):
    audit(
        "tool",
        ctx.request_id,
        ctx.user_id,
        ctx.source,
        ctx.visibility,
        tool=tool,
        capability=capability,
        allowed=allowed,
    )


# --- resolving a caller ---------------------------------------------------------------------


def resolve_context(user, conversation, request_id):
    """Turn what the gateway sent into an AuthContext, or raise.

    AuthorizationError    the person may not use this assistant (safe to show them)
    AuthorizationUnavailable   permissions could not be read (fail closed)
    """
    user = user if isinstance(user, dict) else {}
    conversation = conversation if isinstance(conversation, dict) else {}

    user_id = str(user.get("user_id") or "").strip()
    source = str(conversation.get("source") or "unknown")
    visibility = conversation.get("visibility")
    if visibility not in ("dm", "channel"):
        visibility = "channel"  # anything unclear is treated as the more restrictive case

    roles = [str(role) for role in (user.get("roles") or [])]
    if USE_ROLE not in roles or not user_id:
        audit("denied", request_id, user_id, source, visibility, reason="no_role_or_identity")
        raise AuthorizationError("You're not set up to use the orders assistant.")

    entry = get_authz_config().get(user_id)  # raises AuthorizationUnavailable, so fails closed
    granted = _granted(entry)
    if not granted:
        audit("denied", request_id, user_id, source, visibility, reason="no_capabilities")
        raise AuthorizationError(
            "You haven't been given access to any order data yet. Ask an admin to add you."
        )

    withheld = ()
    if "customers" in granted and visibility != "dm":
        granted = granted - {"customers"}
        withheld = ("customers",)
    if not granted:
        audit("denied", request_id, user_id, source, visibility, reason="only_customers_in_channel")
        raise AuthorizationError(
            "Customer details are only available in a direct message with me, not in a channel."
        )

    name = str(entry.get("name") or user.get("name") or user_id)
    ctx = AuthContext(
        request_id=request_id,
        user_id=user_id,
        name=name,
        source=source,
        visibility=visibility,
        capabilities=granted,
        withheld=withheld,
    )
    audit(
        "request",
        request_id,
        user_id,
        source,
        visibility,
        granted=sorted(granted),
        withheld=list(withheld),
    )
    return ctx
