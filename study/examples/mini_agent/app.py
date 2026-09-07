"""CLI composition root; copy this whole directory anywhere to run it."""
import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from provider import ScriptedProvider
from runner import Runner
from session import SessionStore
from tools import make_tools


async def execute(args, root):
    runner = Runner(ScriptedProvider(), make_tools(), SessionStore(root))
    answer, trace = await runner.run(args.session, args.text)
    for event in trace:
        print(event)
    print("answer:", answer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default="add 2 3")
    parser.add_argument("--session", default="demo")
    parser.add_argument("--state-dir", type=Path,
                        help="Persist state here; default uses a temporary demo directory.")
    args = parser.parse_args()
    if args.state_dir is None:
        with TemporaryDirectory(prefix="mini-agent-") as directory:
            asyncio.run(execute(args, Path(directory)))
    else:
        asyncio.run(execute(args, args.state_dir))


if __name__ == "__main__":
    main()
