"""Exchange this Cloud Run service's Google identity for an Anthropic access token.

Needs four environment variables on the service (see docs/setup.md):
ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID, ANTHROPIC_SERVICE_ACCOUNT_ID,
ANTHROPIC_WORKSPACE_ID.
"""

import json
import os
import urllib.request


def get_anthropic_token():
    metadata_url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        "instance/service-accounts/default/identity"
        "?audience=https%3A%2F%2Fapi.anthropic.com&format=full"
    )
    metadata_request = urllib.request.Request(
        metadata_url, headers={"Metadata-Flavor": "Google"}
    )
    with urllib.request.urlopen(metadata_request, timeout=10) as response:
        google_jwt = response.read().decode("utf-8")

    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": google_jwt,
        "federation_rule_id": os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
        "organization_id": os.environ["ANTHROPIC_ORGANIZATION_ID"],
        "service_account_id": os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"],
        "workspace_id": os.environ["ANTHROPIC_WORKSPACE_ID"],
    }
    token_request = urllib.request.Request(
        "https://api.anthropic.com/v1/oauth/token",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(token_request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result["access_token"]
