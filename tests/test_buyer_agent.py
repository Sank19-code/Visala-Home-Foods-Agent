# The buyer agent loop, driven by a scripted stand-in for the LiteLLM call, so no API key or
# network is needed.
# The store side is REAL: the in-process MCP server, guardrails, audit log and a fake Razorpay.
import asyncio
import json

from litellm import ModelResponse

from src.buyer_agent.agent import HOST_TOOL_NAMES, BuyerAgent
from src.buyer_agent.human import ScriptedUser
from src.buyer_agent.toolbox import InProcessToolbox
from src.db.models import AuditEvent, Order

PROFILE = {"name": "Test Buyer", "phone": "9876543210", "customer_ref": "user_1",
           "default_pincode": "560001"}


def _msg(blocks, stop="tool_calls"):
    # blocks: {"type": "text", ...} and _use(...) items -> one OpenAI-format response, as LiteLLM
    # returns it.
    text = " ".join(b["text"] for b in blocks if b.get("type") == "text") or None
    calls = [b for b in blocks if b.get("type") == "function"]
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return ModelResponse(
        model="anthropic/claude-sonnet-5",
        choices=[{"index": 0, "message": message, "finish_reason": stop}],
        usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
    )


def _use(name, **args):
    return {"id": f"call_{name}_{len(json.dumps(args))}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM:
    # Stands in for litellm.acompletion. Each step: function(last_tool_results) -> ModelResponse.
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    async def __call__(self, **kwargs):
        self.requests.append(kwargs)
        messages = kwargs["messages"]
        results, i = {}, len(messages) - 1
        while i >= 0 and messages[i]["role"] == "tool":
            i -= 1
        if i >= 0 and messages[i]["role"] == "assistant":
            names = {c["id"]: c["function"]["name"] for c in messages[i].get("tool_calls", [])}
            for m in messages[i + 1:]:
                results[names[m["tool_call_id"]]] = json.loads(m["content"])
        return self.steps.pop(0)(results)


def _happy_steps(state):
    def s1(_):
        return _msg([{"type": "text", "text": "Looking for mango pickle."},
                     _use("search_products", query="mango pickle")])

    def s2(_):
        return _msg([_use("create_cart", customer_ref="user_1")])

    def s3(r):
        state["cart_id"] = r["create_cart"]["cart_id"]
        return _msg([_use("add_to_cart", cart_id=state["cart_id"], product_id="PKL-MNG-250", qty=2)])

    def s4(_):
        return _msg([_use("request_purchase_approval", cart_id=state["cart_id"], pincode="560001")])

    def s5(r):
        a = r["request_purchase_approval"]
        state["approval"] = a
        if not a.get("approved"):  # a misbehaving model that orders anyway
            return _msg([_use("create_order", cart_id=state["cart_id"], customer_name="Test Buyer",
                              customer_phone="9876543210", pincode="560001",
                              idempotency_key="ik_no_consent_01")])
        return _msg([_use("create_order", cart_id=state["cart_id"], customer_name="Test Buyer",
                          customer_phone="9876543210", pincode="560001",
                          idempotency_key=a["idempotency_key"],
                          confirmation_token=a["confirmation_token"])])

    def s6(r):
        state["order"] = r["create_order"]
        if not r["create_order"]["ok"]:
            return _msg([{"type": "text", "text": "I could not place the order."}], stop="stop")
        return _msg([_use("wait_for_payment", order_id=r["create_order"]["order_id"],
                          timeout_seconds=5)])

    def s7(r):
        state["payment"] = r["wait_for_payment"]
        return _msg([{"type": "text", "text": "Ordered 2 mango pickles, ₹409, paid."}],
                    stop="stop")

    return [s1, s2, s3, s4, s5, s6, s7]


def _run(steps, user):
    async def go():
        async with InProcessToolbox() as toolbox:
            agent = BuyerAgent(toolbox, user, completion=FakeLLM(steps), model="anthropic/claude-sonnet-5",
                               poll_interval=0)
            return await agent.run("order 2 mango pickles to 560001", PROFILE), agent
    return asyncio.run(go())


def test_happy_path_order_is_placed_and_paid(store):
    factory, gateway = store
    state = {}
    user = ScriptedUser(on_payment_link=lambda url, order_id: gateway.mark(
        next(iter(gateway.links)), "paid"))
    result, agent = _run(_happy_steps(state), user)

    assert result.error is None and result.stop_reason == "stop"
    assert len(result.order_ids) == 1 and state["payment"]["status"] == "paid"
    assert result.approvals == [{"cart_id": state["cart_id"], "amount_paise": 40900,
                                 "approved": True}]
    # The human saw the STORE's summary, not text written by the model.
    shown = dict(user.transcript)["approval_shown"]
    assert "total ₹409" in shown
    # Trace never contains the token.
    create = result.calls("create_order")[0]
    assert create.ok and create.input["confirmation_token"] == "***"
    assert result.cost_usd > 0 and result.turns == 7
    with factory() as s:
        assert s.get(Order, result.order_ids[0]).status == "paid"


def test_declined_approval_blocks_the_order_even_if_the_model_tries(store):
    factory, gateway = store
    state = {}
    result, _ = _run(_happy_steps(state), ScriptedUser(approve_decisions=[False]))

    assert state["approval"]["approved"] is False
    assert state["order"]["error"]["code"] == "confirmation_required"
    assert result.order_ids == [] and gateway.calls == []
    with factory() as s:
        assert s.query(Order).count() == 0
        blocked = s.query(AuditEvent).filter_by(action="create_order").one()
    assert blocked.outcome == "blocked"


def test_model_sees_store_tools_and_host_tools(store):
    fake = FakeLLM([lambda _: _msg([{"type": "text", "text": "What budget?"}], stop="stop")])

    async def go():
        async with InProcessToolbox() as toolbox:
            agent = BuyerAgent(toolbox, ScriptedUser(), completion=fake,
                               model="anthropic/claude-sonnet-5")
            return await agent.run("send me something spicy", PROFILE)

    result = asyncio.run(go())
    req = fake.requests[0]
    names = {t["function"]["name"] for t in req["tools"]}
    assert HOST_TOOL_NAMES <= names and {"search_products", "create_order"} <= names
    assert "confirm" not in " ".join(names - {"request_purchase_approval"})
    assert req["model"] == "anthropic/claude-sonnet-5"
    assert req["messages"][0]["role"] == "system"
    assert req["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert result.final_text == "What budget?"


def test_api_failure_is_reported_not_raised(store):
    async def broken(**_):
        raise RuntimeError("overloaded")

    async def go():
        async with InProcessToolbox() as toolbox:
            return await BuyerAgent(toolbox, ScriptedUser(), completion=broken,
                                    model="anthropic/claude-sonnet-5").run("hi", PROFILE)

    result = asyncio.run(go())
    assert result.error and "overloaded" in result.error


def test_bad_tool_arguments_go_back_to_the_model(store):
    def s1(_):
        call = _use("search_products")
        call["function"]["arguments"] = "{not json"
        return _msg([call])

    def s2(r):
        assert r["search_products"]["error"]["code"] == "invalid_arguments"
        return _msg([{"type": "text", "text": "Retrying."}], stop="stop")

    result, _ = _run([s1, s2], ScriptedUser())
    assert result.tool_calls[0].error_code == "invalid_arguments" and result.error is None


def test_model_string_comes_from_settings(monkeypatch):
    from src import config

    s = config.settings.model_copy(update={"llm_provider": "anthropic", "llm_model": "claude-sonnet-5",
                                           "llm_api_base": ""})
    assert s.litellm_model == "anthropic/claude-sonnet-5"
    proxy = s.model_copy(update={"llm_api_base": "http://localhost:4000", "llm_model": "buyer-agent"})
    assert proxy.litellm_model == "litellm_proxy/buyer-agent"
