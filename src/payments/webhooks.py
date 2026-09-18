# FastAPI app: receives Razorpay webhooks and exposes order status.
# POST /webhooks/razorpay -> verify X-Razorpay-Signature (HMAC-SHA256 over the RAW body,
#                            compared with hmac.compare_digest), then mark the order paid.
# GET  /orders/{order_id}  -> status for the agent to poll.
# Unverified payloads are rejected with 400 and logged as a guardrail event.
# TODO

from fastapi import FastAPI

app = FastAPI(title="Visala Agentic Commerce")
