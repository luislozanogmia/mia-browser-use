import asyncio

from bridge_server import BridgeServer


def test_native_sheet_verification_budget_covers_read_backoff():
    server = object.__new__(BridgeServer)
    seen = []

    async def execute(command, args, timeout):
        seen.append((command, timeout))
        return True, {}

    server.execute = execute

    async def run():
        for command in ("ghost_sheet_append", "ghost_read", "ghost_click", "ghost_eval"):
            await server._chat_call(command, {})

    asyncio.run(run())
    assert seen == [("ghost_sheet_append", 180), ("ghost_read", 90), ("ghost_click", 45)]
