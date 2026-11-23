"""Run synthetic Records OGP or Lambda ZIP workloads for local profiling."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from hashlib import shake_256
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter


def measure(run: Callable[[], None], *, started: float, runs: int) -> dict[str, float]:
    run()
    first = perf_counter() - started
    warm = []
    for _ in range(runs - 1):
        start = perf_counter()
        run()
        warm.append(perf_counter() - start)
    return {"first_ms": round(first * 1000, 3), "warm_median_ms": round(median(warm) * 1000, 3)}


def profile_ogp(runs: int) -> dict[str, float]:
    started = perf_counter()
    from services.records.tools.preview_ogp import fictional_previews, preview_renderer

    renderer = preview_renderer()
    previews = fictional_previews()

    def render() -> None:
        for preview in previews.values():
            renderer.render(preview)

    return measure(render, started=started, runs=runs)


def profile_zip(runs: int) -> dict[str, float]:
    with TemporaryDirectory(prefix="records-profile-") as directory:
        work = Path(directory)
        source = work / "source"
        source.mkdir()
        (source / "fixture.json").write_text(
            json.dumps({"entries": [{"sample": i} for i in range(32_768)]}),
            encoding="utf-8",
        )
        (source / "fixture.bin").write_bytes(shake_256(b"records-zip-fixture").digest(8 * 1024**2))

        started = perf_counter()
        from tools.build_records_bundle import build_bundle

        output = work / "bundle.zip"
        return measure(lambda: build_bundle(source, output), started=started, runs=runs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", choices=("ogp", "zip"))
    parser.add_argument("--runs", type=int, default=20, help="first run plus warmed repeats")
    args = parser.parse_args()
    if args.runs < 2:
        parser.error("--runs must be at least 2 to compare the first and warmed runs")

    workloads = {"ogp": profile_ogp, "zip": profile_zip}
    times = workloads[args.workload](args.runs)
    print(json.dumps({"workload": args.workload, "runs": args.runs, **times}))


if __name__ == "__main__":
    main()
