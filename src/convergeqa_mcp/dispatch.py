"""Unified console entry point: ``convergeqa-mcp <reviews|compare>``.

The MCP Registry lists both servers against this single PyPI package, so
clients launch ``uvx convergeqa-mcp reviews`` or ``uvx convergeqa-mcp compare``.
The dedicated ``convergeqa-mcp-reviews`` / ``convergeqa-mcp-compare`` scripts
remain available and behave identically.
"""

import sys

from . import compare_due_diligence_mcp, service_account_reviews_mcp

_SERVERS = {
    "reviews": service_account_reviews_mcp.main,
    "compare": compare_due_diligence_mcp.main,
}

_USAGE = (
    "usage: convergeqa-mcp <reviews|compare>\n"
    "  reviews  Critique and Iterate review tools (stdio MCP server)\n"
    "  compare  Compare due-diligence review tools (stdio MCP server)\n"
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in _SERVERS:
        sys.stderr.write(_USAGE)
        return 2
    return _SERVERS[args[0]]()


if __name__ == "__main__":
    raise SystemExit(main())
