"""Who may see what. Decided here, in code, before any Shopify data is fetched.

Identity comes from the gateway (a trusted, private caller): `user.user_id` is a stable TWL id such
as "twl:chris-ross". This service never accepts an identity from anywhere else, and never from
message text.

What that person may see comes from the secret `twl-orders-authz`, which this service owns:

    {"users": {"twl:chris-ross": {"name": "Chris Ross",
                                  "capabilities": ["orders", "products", "inventory", "customers"]}}}

Capabilities:
    orders       orders, order totals, order counts, fulfilment and tracking
    products     products and variants, including selling prices
    inventory    stock quantities and locations (also the stock figures on products)
    customers    customer details: names, emails, phones, addresses, the order note, customer search
    order_entry  prepare a new order for an existing customer (company), for approval. Shows only the
                 company and location name, never emails, phones or addresses. Available in a DM and in
                 the channels listed under "order_entry_channels" in the secret (for example #sales):

    {"users": {...}, "order_entry_channels": ["C0123SALES"]}

Approving and creating the order is a separate step done in /v1/act (see entry.py), which checks the
approver's role and this capability again.

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

CAPABILITIES = ("orders", "products", "inventory", "customers")  # reading data
ORDER_ENTRY = "order_entry"  # preparing (and, with approval, creating) a new order
ALL_CAPABILITIES = CAPABILITIES + (ORDER_ENTRY,)

log = logging.getLogger("orders_agent.authz")

_cache = {"value": None, "channels": None, "loaded_at": 0.0}


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
    channel_id: str = ""  # the Slack channel id (from the conversation id), for the allowlist

    def has(self, capability):
        return capability in self.capabilities

    def describe(self):
        """A short statement for the model, so it phrases answers well. It does NOT grant or
        limit anything. The tools and queries are what enforce access."""
        readable = [c for c in self.capabilities if c in CAPABILITIES]
        can = ", ".join(sorted(readable)) or "nothing"
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
        if self.has(ORDER_ENTRY):
            text += " This user can prepare new orders for existing customers (companies or individuals)."
        elif ORDER_ENTRY in self.withheld:
            text += (
                " Preparing new orders is not available in this conversation. It works in a direct"
                " message and in the sales channel."
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
        channels = config.get("order_entry_channels")
        _cache["value"] = users
        _cache["channels"] = frozenset(
            str(channel).strip().upper() for channel in channels if str(channel).strip()
        ) if isinstance(channels, list) else frozenset()
        _cache["loaded_at"] = now
    return _cache["value"]


def get_order_entry_channels():
    """The Slack channel ids where order entry is allowed besides a DM (for example #sales).
    Comes from the same secret. An absent or malformed list means no channels."""
    get_authz_config()  # loads or refreshes the cache, and fails closed if unreadable
    return _cache["channels"] or frozenset()


def channel_of(conversation):
    """The Slack channel id from a conversation, or ''. Ids look like slack:<channel>:<thread ts>."""
    parts = str((conversation or {}).get("id") or "").split(":")
    if len(parts) >= 3 and parts[0] == "slack":
        return parts[1].strip().upper()
    return ""


def _granted(entry):
    raw = entry.get("capabilities") if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(item).strip().lower() for item in raw) & frozenset(ALL_CAPABILITIES)


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

    channel_id = channel_of(conversation)
    withheld = ()
    if "customers" in granted and visibility != "dm":
        granted = granted - {"customers"}
        withheld = ("customers",)
    if ORDER_ENTRY in granted and visibility != "dm":
        # Outside a DM, order entry works only in the channels on the allowlist (for example #sales).
        if not channel_id or channel_id not in get_order_entry_channels():
            granted = granted - {ORDER_ENTRY}
            withheld = withheld + (ORDER_ENTRY,)
    if not granted:
        audit("denied", request_id, user_id, source, visibility, reason="nothing_available_here")
        raise AuthorizationError(
            "That isn't available in a channel. Customer details and order entry work in a direct "
            "message with me (and order entry also in the sales channel)."
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
        channel_id=channel_id,
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
