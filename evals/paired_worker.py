"""Trusted subprocess entry, imported from the materialized subject snapshot."""

import argparse
import multiprocessing as mp
import os
from pathlib import Path

from evals.common import ROOT, canonical, read, write
from evals.repeat import record_trial
from evals.run import run_trial
from evals.worker import worker_main


class CredentialContext:
    """Only the trusted provider child receives an external credential path."""

    def __init__(self, credential_file):
        self.context = mp.get_context("spawn")
        self.credential_file = credential_file

    def Pipe(self):  # noqa: N802 - multiprocessing interface
        return self.context.Pipe()

    def Process(self, *, target, args):  # noqa: N802 - multiprocessing interface
        if target is worker_main and len(args) == 2 and args[1].get("mode") == "R":
            args[1]["credentials"] = str(self.credential_file)
        return self.context.Process(target=target, args=args)


def execute(request):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("conda_nanobot_required")
    output = Path(request["output"])
    write(output / "request.json", request)
    case, plan = request["case"], request["plan"]
    isolation = ROOT / "tmp" / "eval-instances"
    isolation.mkdir(parents=True, exist_ok=True)
    result = run_trial(case, plan, output, isolation, request["mode"],
                       CredentialContext(Path(request["credential_file"])))
    row = record_trial(case, plan, output, result)
    write(output / "execution-result.json", row)
    (output / "complete.marker").write_bytes(canonical({"trial_id": plan["trial_id"]}))
    return row


def module_baseline(output):
    from evals.batches import catalog, run_case, summarize

    cases = catalog()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    write(output / "contracts.json", cases)
    for case_id in sorted(cases):
        row = run_case(cases[case_id], output, output.name)
        rows.append(row)
        with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical(row).decode() + "\n")
            stream.flush()
        print(f"current module {case_id}: {row['verdict']}", flush=True)
    summary = summarize(list(cases), rows)
    write(output / "run-result.json", {**summary, "rows": rows})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--module-baseline", type=Path)
    args = parser.parse_args()
    if bool(args.request) == bool(args.module_baseline):
        raise ValueError("one_worker_operation_required")
    if args.request:
        request = read(args.request)
        if request.get("kind") in ("phase8_agent", "phase8_module"):
            from evals.daily_worker import execute as daily_execute

            daily_execute(request)
        else:
            execute(request)
    else:
        module_baseline(args.module_baseline)


if __name__ == "__main__":
    main()
