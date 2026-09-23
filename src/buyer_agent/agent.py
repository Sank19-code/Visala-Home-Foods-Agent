# The buyer agent: Claude, called through LiteLLM, in a tool-use loop over the commerce MCP server.
#
# LiteLLM is the model gateway: the loop speaks the OpenAI chat format and LiteLLM translates to
# Anthropic (or any provider). Point LLM_API_BASE at a LiteLLM proxy to route through a shared
# gateway with its own keys, budgets, fallbacks and logging; leave it empty to call the provider
# directly from this process. See src/config.py and litellm_config.yaml.
#
#   understand -> search -> check budget/stock -> build cart -> check delivery -> quote
#   -> ASK THE HUMAN (request_purchase_approval) -> create_order -> wait_for_payment -> summary
#
# Two kinds of tools are given to the model:
#   * store tools   — every tool the MCP server lists (search_products ... get_order_status)
#   * host tools    — implemented HERE, on the human's side of the table:
#       ask_user                   clarifying questions and substitute consent
#       request_purchase_approval  shows the STORE's quote to the human; only a "yes" mints the
#                                  confirmation token (the model cannot mint one itself)
#       wait_for_payment           hands the payment link to the human and polls order status
#
# The loop is plain and explicit (no agent framework) so every step is visible in the trace that
# evals score: tool calls, errors, questions, approvals, tokens, cost and latency.
import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Keep LiteLLM's per-call INFO lines out of the shopping conversation.
os.environ.setdefault("LITELLM_LOG", "ERROR")
logging.getLogger("LiteLLM").setLevel(logging.ERROR)

from src.buyer_agent.human import UserChannel  # noqa: E402
from src.buyer_agent.prompts import SYSTEM_PROMPT, customer_context  # noqa: E402
from src.buyer_agent.toolbox import Toolbox  # noqa: E402

HOST_TOOLS = [
    {
        "name": "ask_user",
        "description": (
            "Ask the customer ONE short question and get their answer. Use it for missing or "
            "vague details, and to get consent before any substitution or plan change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "request_purchase_approval",
        "description": (
            "Show the customer the store's exact quote for this cart and pincode and ask them to "
            "approve it. Returns approved=true with confirmation_token and idempotency_key to pass "
            "to create_order, or approved=false with what the customer said."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"cart_id": {"type": "string"}, "pincode": {"type": "string"}},
            "required": ["cart_id", "pincode"],
        },
    },
    {
        "name": "wait_for_payment",
        "description": (
            "Give the customer the payment link for this order and wait until they pay (or the "
            "timeout passes). Returns the order status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 900},
            },
            "required": ["order_id"],
        },
    },
]
HOST_TOOL_NAMES = {t["name"] for t in HOST_TOOLS}


@dataclass
class ToolCall:
    name: str
    input: dict
    ok: bool
    error_code: str | None
    latency_ms: int


@dataclass
class RunResult:
    run_id: str
    request: str
    model: str
    final_text: str = ""
    stop_reason: str = ""
    turns: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None

    @property
    def order_ids(self) -> list[str]:
        return sorted({o["order_id"] for o in self.orders})

    def calls(self, name: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == name]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["order_ids"] = self.order_ids
        return data


def _cost(response: Any) -> float:
    # LiteLLM's model price map; 0 when the model is unknown to it (e.g. a proxy alias).
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        return 0.0


def _openai_tools(specs: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
        }}
        for t in specs
    ]


def _redact(args: dict) -> dict:
    return {k: ("***" if "token" in k else v) for k, v in args.items()}


class BuyerAgent:
    def __init__(
        self,
        toolbox: Toolbox,
        user: UserChannel,
        *,
        completion: Any = None,
        model: str | None = None,
        max_turns: int | None = None,
        max_tokens: int = 2048,
        poll_interval: float = 5.0,
        default_payment_timeout: int = 180,
    ) -> None:
        from src.config import settings

        self.toolbox = toolbox
        self.user = user
        self.model = model or settings.litellm_model
        self.max_turns = max_turns or settings.agent_max_turns
        self.max_tokens = max_tokens
        self.poll_interval = poll_interval
        self.default_payment_timeout = default_payment_timeout
        # Injected in tests; otherwise litellm.acompletion.
        self._completion = completion
        self._llm_kwargs = {"num_retries": 2, "timeout": 120}
        if settings.llm_api_key:
            self._llm_kwargs["api_key"] = settings.llm_api_key
        if settings.llm_api_base:
            self._llm_kwargs["api_base"] = settings.llm_api_base

    # --- host tools ------------------------------------------------------------------------

    async def _ask_user(self, result: RunResult, question: str) -> dict:
        result.questions.append(question)
        answer = await asyncio.to_thread(self.user.ask, question)
        return {"ok": True, "answer": answer}

    async def _request_approval(self, result: RunResult, cart_id: str, pincode: str) -> dict:
        quote = await self.toolbox.call("get_checkout_quote", {"cart_id": cart_id, "pincode": pincode})
        if not quote.get("ok"):
            return quote
        record = {"cart_id": cart_id, "amount_paise": quote["amount_paise"], "approved": False}
        result.approvals.append(record)
        if not quote.get("within_spend_limits", True):
            record["reason"] = "over_spend_limit"
            return {"ok": True, "approved": False, "reason": "Total is above the spend limit; "
                    "the customer was not asked. Reduce the cart or explain."}

        approved, words = await asyncio.to_thread(
            self.user.approve, quote["summary"], quote["amount_display"]
        )
        if not approved:
            record["user_said"] = words
            return {"ok": True, "approved": False, "user_said": words}

        # Minted by the host after the human said yes. Recomputed from live data, so it covers
        # exactly what the store will charge, whatever the model believes the cart contains.
        from src.commerce_mcp.runtime import open_session
        from src.commerce_mcp.tools.orders import approve_quote

        with open_session() as session:
            minted = approve_quote(session, cart_id, pincode)
        if minted["amount_paise"] != quote["amount_paise"]:
            return {"ok": False, "error": {"code": "price_changed",
                                           "message": "The total changed while approving.",
                                           "hint": "Quote again and ask again."}}
        record["approved"] = True
        return {
            "ok": True,
            "approved": True,
            "confirmation_token": minted["confirmation_token"],
            "idempotency_key": f"ik_{uuid.uuid4().hex}",
            "amount_display": quote["amount_display"],
            "summary": quote["summary"],
        }

    async def _wait_for_payment(self, order_id: str, timeout_seconds: int | None) -> dict:
        status = await self.toolbox.call("get_order_status", {"order_id": order_id})
        if not status.get("ok"):
            return status
        if status["status"] == "pending" and status.get("payment_link_url"):
            await asyncio.to_thread(
                self.user.payment_link, status["payment_link_url"], status["amount_display"], order_id
            )
        deadline = time.monotonic() + (timeout_seconds or self.default_payment_timeout)
        while status.get("ok") and status["status"] == "pending" and time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            status = await self.toolbox.call("get_order_status", {"order_id": order_id})
        if status.get("ok") and status["status"] == "pending":
            status["message"] = "Not paid yet. The link stays valid for about 30 minutes."
        return status

    async def _run_tool(self, result: RunResult, name: str, args: dict) -> dict:
        if name == "ask_user":
            return await self._ask_user(result, args.get("question", ""))
        if name == "request_purchase_approval":
            return await self._request_approval(result, args.get("cart_id", ""), args.get("pincode", ""))
        if name == "wait_for_payment":
            return await self._wait_for_payment(args.get("order_id", ""), args.get("timeout_seconds"))
        output = await self.toolbox.call(name, args)
        if name == "create_order" and output.get("ok"):
            result.orders.append({"order_id": output["order_id"], "status": output["status"],
                                  "amount_paise": output["amount_paise"],
                                  "replayed": output.get("replayed", False)})
        return output

    # --- loop ------------------------------------------------------------------------------

    async def _complete(self, **kwargs) -> Any:
        if self._completion is not None:
            return await self._completion(**kwargs)
        import litellm

        litellm.suppress_debug_info = True
        return await litellm.acompletion(**kwargs, **self._llm_kwargs)

    async def run(self, request: str, profile: dict | None = None) -> RunResult:
        started = time.monotonic()
        result = RunResult(run_id=f"run_{uuid.uuid4().hex[:10]}", request=request, model=self.model)

        tools = _openai_tools(await self.toolbox.list_tools() + HOST_TOOLS)
        opening = f"{customer_context(profile or {})}\n\nCustomer request: {request}"
        messages: list[dict] = [
            # cache_control is passed through by LiteLLM to Anthropic prompt caching; other
            # providers ignore it. The system prompt + tools are identical on every turn.
            {"role": "system", "content": [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ]},
            {"role": "user", "content": opening},
        ]

        try:
            while result.turns < self.max_turns:
                result.turns += 1
                response = await self._complete(
                    model=self.model, messages=messages, tools=tools, max_tokens=self.max_tokens
                )
                usage = getattr(response, "usage", None)
                result.usage["input_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
                result.usage["output_tokens"] += getattr(usage, "completion_tokens", 0) or 0
                result.cost_usd += _cost(response)

                choice = response.choices[0]
                message = choice.message
                text = (message.content or "").strip()
                tool_calls = list(message.tool_calls or [])
                result.stop_reason = choice.finish_reason or ""

                assistant: dict = {"role": "assistant", "content": message.content or ""}
                if tool_calls:
                    assistant["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments or "{}"}}
                        for tc in tool_calls
                    ]
                messages.append(assistant)

                if not tool_calls:
                    result.final_text = text
                    if text:
                        await asyncio.to_thread(self.user.say, text)
                    break
                if text:  # narration between tool calls
                    await asyncio.to_thread(self.user.say, text)

                for tc in tool_calls:
                    t0 = time.monotonic()
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("arguments must be a JSON object")
                    except ValueError as exc:
                        args, output = {}, {"ok": False, "error": {
                            "code": "invalid_arguments", "message": f"Bad tool arguments: {exc}"}}
                    else:
                        output = await self._run_tool(result, name, args)
                    ok = bool(output.get("ok", True))
                    result.tool_calls.append(ToolCall(
                        name=name, input=_redact(args), ok=ok,
                        error_code=(output.get("error") or {}).get("code") if not ok else None,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                    ))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(output, ensure_ascii=False, default=str),
                    })
            else:
                result.stop_reason = "max_turns"
        except Exception as exc:  # provider/gateway errors, network — reported, never swallowed
            result.error = f"{type(exc).__name__}: {exc}"[:500]

        result.cost_usd = round(result.cost_usd, 6)
        result.latency_s = round(time.monotonic() - started, 2)
        return result


async def run_request(
    request: str,
    user: UserChannel,
    *,
    profile: dict | None = None,
    in_process: bool = False,
    **agent_kwargs,
) -> RunResult:
    from src.buyer_agent.toolbox import InProcessToolbox, StdioToolbox

    session_id = f"agent_{uuid.uuid4().hex[:10]}"
    toolbox = InProcessToolbox() if in_process else StdioToolbox(session_id)
    async with toolbox:
        agent = BuyerAgent(toolbox, user, **agent_kwargs)
        return await agent.run(request, profile)
