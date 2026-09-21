# Repository structure

`visala-agentic-commerce` — 55 tracked files · last updated 19 Sep 2026

Legend: ✅ working · 🟡 stub (contract written, no code) · 📝 docs to write · ⬜ empty package marker

```
visala-agentic-commerce/
├── README.md                          📝  skeleton, TODOs to fill
├── Makefile                           ✅  setup · seed · run · mcp · demo · eval · test
├── pyproject.toml                     ✅  dependency list
├── .env.example                       ✅  test-key template
├── .gitignore                         ✅  .env, *.db excluded
├── .github/workflows/ci.yml           ✅  ruff + pytest on every push
│
├── data/
│   └── catalog.json                   ✅  6 products, 1 deliberately out of stock
│
├── docs/
│   ├── architecture.png / .svg        ✅  system diagram
│   ├── structure.png / .svg / .md     ✅  this file
│   ├── architecture.md                📝  needs the order sequence diagram
│   ├── decisions.md                   📝  6 decisions listed, unwritten
│   └── README.md                      📝  asset checklist
│
├── demo/
│   └── README.md                      📝  pitch-video plan
│
├── evals/
│   ├── run_evals.py                   🟡  NotImplementedError
│   ├── scenarios/                     ✅  6 scenarios, fully written
│   │   ├── happy_path.yaml            ✅
│   │   ├── over_budget.yaml           ✅
│   │   ├── out_of_stock.yaml          ✅
│   │   ├── duplicate_submit.yaml      ✅
│   │   ├── forged_webhook.yaml        ✅
│   │   └── ambiguous_request.yaml     ✅
│   └── results/latest.md              📝  empty metrics table
│
├── src/
│   ├── config.py                      ✅  the only working code — refuses rzp_live_ keys
│   ├── money.py                       ✅  integer paise, floats refused
│   ├── db/                                ✅ done
│   │   ├── models.py                  ✅  6 tables, constraints enforce the guarantees
│   │   ├── session.py                 ✅  engine, FK pragma, append-only audit triggers
│   │   └── seed.py                    ✅  idempotent upsert · --reset
│   ├── commerce_mcp/                      agent-facing storefront
│   │   ├── server.py                  🟡  registers the 7 MCP tools
│   │   └── tools/
│   │       ├── catalog.py             🟡  search_products · get_product
│   │       ├── cart.py                🟡  create_cart · add_to_cart
│   │       ├── delivery.py            🟡  check_delivery
│   │       └── orders.py              🟡  create_order — 6-step contract written
│   ├── payments/                          money lane
│   │   ├── razorpay_client.py         🟡  payment links, paise conversion
│   │   └── webhooks.py                🟡  FastAPI app exists, 0 routes
│   ├── guardrails/                        enforced in code, not in prompts
│   │   ├── idempotency.py             🟡  one order per key
│   │   ├── spend_limits.py            🟡  per-order / per-day caps
│   │   ├── confirmation.py            🟡  no order without a consent token
│   │   └── audit_log.py               🟡  append-only tool-call log
│   ├── buyer_agent/                       the AI customer
│   │   ├── graph.py                   🟡  LangGraph flow
│   │   ├── prompts.py                 🟡  system prompts
│   │   └── cli.py                     🟡  python -m src.buyer_agent.cli
│   ├── merchant_agent/                    the growth half
│   │   ├── abandoned_carts.py         🟡
│   │   └── daily_summary.py           🟡
│   └── __init__.py  (×8)              ⬜  package markers
│
└── tests/                                 24 tests passing
    ├── conftest.py                    ✅  in-memory DB fixtures
    ├── test_db.py                     ✅  11 tests
    ├── test_money.py                  ✅  10 tests
    ├── test_config_test_mode.py       ✅  3 tests
    ├── test_idempotency.py            🟡
    ├── test_webhook_signature.py      🟡
    ├── test_spend_limits.py           🟡
    └── test_order_flow.py             🟡
```

## Where the layout comes from

| Folder | Responsibility | Why it is separate |
|---|---|---|
| `commerce_mcp/` | The only way an agent can reach the store | Keeps the agent interface auditable — no scraping, no browser driving |
| `payments/` | Razorpay test API + webhook receipt | Money handling stays in one place, never inside agent logic |
| `guardrails/` | Spend caps, confirmation, idempotency, audit | Enforced in code, so a prompt injection cannot talk its way past them |
| `buyer_agent/` | The AI customer | Plans and asks; never holds credentials, never authorises payment |
| `merchant_agent/` | Growth side: nudges, summaries | Separate actor, separate permissions |
| `db/` | Demo catalogue, carts, orders, audit log | A copy, never the production Firestore of the live app |
| `evals/` | Scenario suite and metrics | Claims in the README must be reproducible |

## Build order

1. ~~`db/models.py` + `db/seed.py`~~ — done 21 Sep
2. `payments/razorpay_client.py` — first real test-mode payment link
3. `commerce_mcp/tools/*` then `server.py` — catalogue and cart before orders
4. `guardrails/*` — wired into `tools/orders.py` as it is written, not bolted on later
5. `payments/webhooks.py` — signature verification plus marking an order paid
6. `buyer_agent/*` — the end-to-end demo
7. `evals/run_evals.py` + `tests/*` — the numbers that go in the README
8. `merchant_agent/*` — cut this first if time runs short

## Status

| | Count |
|---|---|
| Working | 13 files |
| Stub | 29 files |
| Docs to write | 5 files |
| Empty package markers | 8 files |
