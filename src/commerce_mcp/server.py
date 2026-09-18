# MCP server exposing the storefront as agent-callable tools.
# Registers: search_products, get_product, create_cart, add_to_cart, check_delivery,
# create_order, get_order_status. Every tool call is written to the audit log.
# TODO: wire up MCP server + tool registration, then run over stdio.

def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
