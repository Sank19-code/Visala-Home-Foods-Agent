# One order per idempotency key.
# Store (idempotency_key -> order_id) with a UNIQUE constraint; on a repeat key return the
# ORIGINAL order instead of creating a new one. This is the fix for the duplicate-receipt-ID
# bug seen in production on Visala Home Foods.
# TODO
