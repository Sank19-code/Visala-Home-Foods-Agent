# Architecture

_TODO: export this as docs/architecture.png for the README._

```
  User ("order 2 pickles under 600")
        |
   Buyer agent (LangGraph)
        |  MCP tool calls
   Commerce MCP server ──► Guardrails (spend cap, confirmation, idempotency, audit)
        |                        |
   Demo DB (catalogue,     Audit log
   carts, orders)
        |
   Razorpay TEST API ──► payment link ──► user pays
        |
   Webhook (signature verified) ──► order marked paid ──► agent confirms
```

## Order sequence
_TODO: sequence diagram -> docs/order_sequence.png_
