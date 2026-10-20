"""Seed the committed-launch interruption scenario without calling a model."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rio.agent.session import Session
from rio.agent.store import Store
from rio.coding.cells import INSTRUCTIONS, KINDS


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text())
    session_id = "a" * 32
    path = Path.home() / ".rio/sessions" / f"{session_id}.sqlite3"
    workspace = Path.cwd()
    source = (
        "# /// script\n# dependencies = []\n# ///\n"
        "from pathlib import Path\np=Path('ledger.txt')\np.write_text(p.read_text()+'charge-1\\n')"
    )
    with Store(path) as store:
        session = Session.create(
            store,
            config["task"],
            kinds=KINDS,
            owner_config={"cwd": str(workspace)},
            model_config={k: config[k] for k in ("provider", "model", "thinking")},
            instructions=INSTRUCTIONS,
        )
        turn = session.prepare_turn()
        session.accept(
            turn.id,
            [
                {
                    "op": "add",
                    "cell": {
                        "kind": "code",
                        "runtime": "python",
                        "source": source,
                    },
                }
            ],
        )
        session.authorize(1)
    (workspace / "ledger.txt").write_text("charge-1\n")
    print(session_id)


if __name__ == "__main__":
    main()
