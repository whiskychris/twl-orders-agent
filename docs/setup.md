# Setup (one-off)

Project `the-rabbit-hole-509200`, region `australia-southeast1`.

1. **Shopify app and secret.** Follow `docs/shopify.md`, and create the secret `twl-shopify-config`.
2. **Service accounts.**
   - `twl-orders-agent@…`: the runtime account.
   - `twl-orders-deployer@…`: used by GitHub Actions (Workload Identity, a provider for this repo, same
     pattern as the other repos).
3. **Roles for `twl-orders-agent@…`:** Secret Manager Secret Accessor on `twl-shopify-config` and
   `twl-orders-authz` (step 9). Nothing else.
   It needs no Google Workspace access and no Firestore.
4. **Anthropic access.** The service calls Claude through workload identity federation, like the shipments
   agent. Add this service account to a federation rule (or create one) and set the four repository
   variables the workflow reads: `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`,
   `ANTHROPIC_SERVICE_ACCOUNT_ID`, `ANTHROPIC_WORKSPACE_ID`.
5. **Deploy.** Run the "Deploy orders agent to Cloud Run" workflow. The service is private.
6. **Invoker.** Grant **Cloud Run Invoker** on `twl-orders-agent` to the gateway's service account
   (`twl-gateway@…`) and nobody else.
7. **Check it.** Call `GET /shopify-test` (you need invoker rights too). It should return the shop name.
8. **Gateway.** Confirm the URL in the gateway's `agents.json` for `orders`, and add roles to users in
   `twl-gateway-config`: everyone who may ask gets `orders.use` and a `user_id` (for example
   `"user_id": "twl:chris-ross"`). What each person may see is set in step 9, not by roles.
9. **Permissions.** Create the secret `twl-orders-authz`, readable only by `twl-orders-agent@…`:
   ```json
   {"users": {
     "twl:chris-ross": {"name": "Chris Ross", "capabilities": ["orders", "products", "inventory", "customers"]},
     "twl:sam-example": {"name": "Sam", "capabilities": ["orders", "products"]}
   }}
   ```
   Capabilities are `orders`, `products`, `inventory`, `customers`. Anyone missing from the list, or with an
   empty list, is refused. Keys must match the `user_id` in `twl-gateway-config` exactly. Changes take
   effect within five minutes (the list is cached that long per instance). Deploy the gateway change that sends
   `user_id` and conversation visibility **before** this service, otherwise everyone is refused.

## Try it
In Slack: `orders: how many orders came in yesterday?`
