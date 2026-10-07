"""Local durability costs, separate from provider-dependent coding evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

import psutil

from rio.durable import Session, Store
from rio.durable.owner import Owner
from rio.durable.prompt import SCHEMA, SYSTEM


async def measure(samples: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="rio-durability-eval-") as directory:
        root = Path(directory)
        path = root / "session.sqlite3"
        startup, script, inbox = [], [], []
        with Store(path) as store:
            session = Session.create(store, "measure startup", root)
            async with Owner(session) as owner:
                for index in range(1, samples + 1):
                    turn = session.prepare_turn(SYSTEM, SCHEMA)
                    session.accept(
                        turn["id"],
                        [
                            {"op": "test", "path": "/revision", "value": turn["state"]["revision"]},
                            {
                                "op": "add",
                                "path": "/cells/-",
                                "value": {
                                    "id": index,
                                    "previous_id": None,
                                    "kind": "code",
                                    "runtime": "python",
                                    "source": (
                                        "# /// script\n# dependencies = []\n# ///\nprint('ready')\n"
                                    ),
                                },
                            },
                        ],
                    )
                    after = store.sequence
                    while session.pending():
                        await owner.tick()
                        await asyncio.sleep(0.01)
                    await owner.tick()
                    events = {e["type"]: e["timestamp"] for e in store.events(after)}
                    startup.append(events["worker_registered"] - events["execution_started"])
                    script.append(events["execution_finished"] - events["worker_registered"])
                    start = time.perf_counter()
                    session.prepare_turn(SYSTEM, SCHEMA)
                    inbox.append(time.perf_counter() - start)
                idle_rss = psutil.Process().memory_info().rss
                assert owner.process is None
            commits = list(store.commit_seconds)
            # Inject an interruption at the committed authorization boundary. The separate
            # process-kill pytest cases exercise actual surviving workers and child cleanup.
            turn = session.prepare_turn(SYSTEM, SCHEMA)
            session.accept(
                turn["id"],
                [
                    {"op": "test", "path": "/revision", "value": turn["state"]["revision"]},
                    {
                        "op": "add",
                        "path": "/cells/-",
                        "value": {
                            "id": samples + 1,
                            "previous_id": None,
                            "kind": "code",
                            "runtime": "python",
                            "source": (
                                "# /// script\n# dependencies = []\n# ///\nraise AssertionError()\n"
                            ),
                        },
                    },
                ],
            )
            session.authorize(samples + 1, {"token": "never-launched"})
        with Store(path) as store:
            recovered = Session(store)
            start = time.perf_counter()
            async with Owner(recovered):
                result = recovered.data["executions"][str(samples + 1)]
                assert result["result"]["type"] == "UnknownExecution"
                assert recovered.meta["pause"] is not None
            recovery = time.perf_counter() - start
            return {
                "samples": samples,
                "commit_p50_seconds": statistics.median(commits),
                "commit_p95_seconds": sorted(commits)[int((len(commits) - 1) * 0.95)],
                "rebuild_seconds": store.rebuild_seconds,
                "worker_startup_p50_seconds": statistics.median(startup),
                "uv_script_p50_seconds": statistics.median(script),
                "inbox_fold_p50_seconds": statistics.median(inbox),
                "idle_owner_rss_bytes": idle_rss,
                "idle_cell_processes": 0,
                "recovery_seconds": recovery,
                "interrupted_cells": 1,
                "unknown_executions": 1,
                "automatic_reexecutions": 0,
                "power_loss_tested": False,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path(".eval-results/durability.json"))
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("--samples must be between 1 and 100")
    result = asyncio.run(measure(args.samples))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
