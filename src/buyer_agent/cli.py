# Run one buyer request end to end:
#   python -m src.buyer_agent.cli "order 2 mango pickles and 1 podi under 600 rupees to 560001"
#
# Claude shops through the commerce MCP server (spawned over stdio). You answer its questions,
# approve the store's quote in this terminal, and pay the Razorpay TEST link in your browser.
#
# Options:
#   --name/--phone/--pincode/--customer-ref   the customer profile the agent may use
#   --model anthropic/claude-haiku-4-5        override LLM_PROVIDER/LLM_MODEL (LiteLLM model string)
#   --in-process                              skip the subprocess (debugging)
#   --open                                    open the payment link in your browser
#   --json out.json                           save the full run trace
import argparse
import asyncio
import json
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Visala buyer agent (Claude via LiteLLM)")
    parser.add_argument("request", help="what to buy, in plain words")
    parser.add_argument("--name", default="Demo Buyer")
    parser.add_argument("--phone", default="9876543210")
    parser.add_argument("--pincode", default="")
    parser.add_argument("--customer-ref", default="demo_user")
    parser.add_argument("--model", default=None)
    parser.add_argument("--in-process", action="store_true")
    parser.add_argument("--open", action="store_true", help="open the payment link in a browser")
    parser.add_argument("--payment-timeout", type=int, default=300)
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # ₹ on Windows consoles

    from src.buyer_agent.agent import run_request
    from src.buyer_agent.human import ConsoleUser
    from src.config import settings

    has_key = settings.llm_api_key or settings.llm_api_base or os.environ.get("ANTHROPIC_API_KEY")
    if not has_key:
        sys.exit("Set LLM_API_KEY (your Anthropic key) or LLM_API_BASE (a LiteLLM proxy) in .env.")

    profile = {
        "name": args.name,
        "phone": args.phone,
        "customer_ref": args.customer_ref,
        "default_pincode": args.pincode,
    }
    result = asyncio.run(
        run_request(
            args.request,
            ConsoleUser(open_browser=args.open),
            profile=profile,
            in_process=args.in_process,
            model=args.model,
            default_payment_timeout=args.payment_timeout,
        )
    )

    print("\n" + "=" * 60)
    if result.error:
        print(f"Run failed: {result.error}")
    print(
        f"orders: {', '.join(result.order_ids) or 'none'} | turns: {result.turns} | "
        f"tool calls: {len(result.tool_calls)} | tokens in/out: "
        f"{result.usage['input_tokens']}/{result.usage['output_tokens']} | "
        f"cost: ${result.cost_usd:.4f} | {result.latency_s}s | model: {result.model}"
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        print(f"trace saved to {args.json_out}")


if __name__ == "__main__":
    main()
