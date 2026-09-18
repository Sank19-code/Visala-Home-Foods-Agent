# Tools: create_order(cart_id, customer, idempotency_key) -> {order_id, payment_link_url}
#        get_order_status(order_id) -> pending | paid | failed | expired
#
# create_order MUST, in this order:
#   1. re-check every line item's live price and stock (never trust the agent's earlier view)
#   2. enforce spend caps           (src.guardrails.spend_limits)
#   3. require explicit confirmation (src.guardrails.confirmation)
#   4. dedupe on idempotency_key     (src.guardrails.idempotency)
#   5. create the Razorpay payment link (test mode)
#   6. write an audit event
# TODO
