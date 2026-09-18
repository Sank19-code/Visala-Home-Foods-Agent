# Design decisions

Each entry: the decision, the alternatives, and why.

## 1. MCP as the agent interface (not a REST API or browser automation)
_TODO_

## 2. Razorpay Payment Links, with a human approving payment
The agent never holds card or UPI credentials. It prepares the transaction; a human authorises
the money. _TODO: expand._

## 3. Built on Razorpay's official MCP server / direct API — and what this layer adds
_TODO: be explicit. The value here is the commerce layer, the guardrails and the evaluation,
not a wrapper around the payments API._

## 4. Idempotency keys on order creation
Learned from production: in the live Visala Home Foods app, one receipt ID could create several
Razorpay orders. _TODO: describe the fix and the unique constraint._

## 5. Test mode only, enforced in code
`src/config.py` refuses to boot on an `rzp_live_` key, so a public repo can never move real money.

## 6. Separate demo database
The demo reads a copy of the catalogue, never the production Firestore, so no real customer
orders can be touched.
