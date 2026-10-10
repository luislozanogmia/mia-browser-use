"""Development reload cannot infer idle from the bounded task list sent to the panel."""
import asyncio

import ghost_chat
from tests.test_build_runtime import runtime


def test_development_idle_includes_hidden_tasks_and_unfinished_job_handles(tmp_path):
    async def main():
        hub, _, first = runtime(tmp_path)
        first.status = "working"
        for n in range(2, ghost_chat.MAX_SHOWN_TASKS + 3):
            task = ghost_chat.Task(n, "Finished", "Read a page", first.url,
                                   "ask", "parallel", first.model, first.agent)
            task.status = "done"
            hub.tasks[task.id] = task
        assert first.id not in {t["id"] for t in hub.state()["tasks"]}
        assert hub.state()["development_idle"] is False
        first.status = "done"
        first.job = asyncio.get_running_loop().create_future()
        assert hub.state()["development_idle"] is False
        first.job.set_result(None)
        assert hub.state()["development_idle"] is True
        hub.planning = 1
        assert hub.state()["development_idle"] is False
        hub.planning = 0
        hub.claude_busy = "signing_in"
        assert hub.state()["development_idle"] is False
        hub.claude_busy = ""
        first.status = "needs_you"
        assert hub.state()["development_idle"] is False
    asyncio.run(main())
