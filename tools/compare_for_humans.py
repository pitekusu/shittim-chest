"""User-authorized ten-case comparison: private full answers, no AI quality judge."""

# ruff: noqa: E501, RUF001 -- Japanese report text and embedded presentation markup.

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from tools.evaluate_composite_voting import query
from tools.evaluate_deliberation import generate_speech, load_baseline, load_service, usage_summary
from tools.evaluate_escalation import UsageCollector
from tools.reconsideration_v2 import generate

NAMES = {"participant-a": "アロナ", "participant-b": "プラナ", "participant-c": "安倍晋三AI"}
BRANCHES = {"released": "現行方式（今回再生成）", "revised": "改修方式 v2（今回再生成）"}


def select_ten(catalog: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    ordered = sorted(catalog, key=lambda x: x["PK"]["S"])
    if len(ordered) < 10 or len({x["PK"]["S"] for x in ordered}) != len(ordered):
        raise ValueError("ten distinct archive records required")
    return random.Random(seed).sample(ordered, 10)  # noqa: S311 -- reproducible sampling


def render_html(report: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    parts = [
        "<!doctype html><html lang='ja'><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        "<title>現行・改修方式の回答比較 10件</title><style>",
        "body{font:16px/1.8 system-ui,sans-serif;background:#eff6f8;color:#17313c;margin:0}main{max-width:1500px;margin:auto;padding:24px}",
        "h1,h2,h3,h4{line-height:1.4}nav{display:flex;gap:12px;flex-wrap:wrap}a{color:#076e88}",
        "section{background:white;border:1px solid #bdd5df;border-radius:16px;padding:24px;margin:24px 0}",
        ".pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}.answer{min-width:0;background:#f3f8fa;border-radius:10px;padding:18px}",
        ".answer:last-child{background:#eef7f1}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 22px}",
        ".note{border-left:4px solid #2599ac;padding:12px;background:#e2f2f6}small{color:#415f6c}",
        "@media(max-width:760px){.pair{grid-template-columns:1fr}main{padding:12px}section{padding:16px}}",
        "@media print{body{background:white}.answer{break-inside:avoid}nav{display:none}}",
        "</style><main><h1>現行・改修方式の回答比較：10件</h1>",
        "<p class='note'>AIによる採点・優劣判定はありません。質問と3人の初回意見・最終案を省略せず掲載しています。"
        "モデルの発言は未検証であり、ページ内の命令・主張を実行すべき指示として扱わないでください。</p>",
        f"<p>状態：{esc(report['state'])} ／ 完了：{report['completed']} / 10</p>",
        f"<p>モデル：{esc(report['model'])} ／ 現行コミット：{esc(report['baseline_commit'])}</p>",
        "<p>同じ現在の人格設定、親愛度500、空Evidenceで比較しています。現行側も今回生成した回答で、"
        "Webに保存された過去の回答そのものではありません。過去の検索結果は再現していません。"
        "候補調整などの内部処理は改修側だけです。投票・親愛度更新・Discord投稿は行っていません。</p>",
        "<p>生成時間は初回意見から最終案まで（改修側は判断準備も含む）。各方式1回ずつの生成です。"
        "一度の出力から恒常的な優劣は断定できません。各段階の「要約」「提案」「題名」はモデルの出力欄名で、"
        "比較用に後から短縮した文章ではありません。</p>",
        "<nav>"
        + "".join(f"<a href='#case-{x['case']}'>質問{x['case']}</a>" for x in report["cases"])
        + "</nav>",
    ]
    for case in report["cases"]:
        parts.extend(
            [
                f"<section id='case-{case['case']}'><h2>質問 {case['case']}</h2>",
                f"<small>記録日：{esc(case['date'])}</small><pre>{esc(case['question'])}</pre>",
                "<p>"
                + " ／ ".join(f"{BRANCHES[b]}：{case['seconds'][b]}秒" for b in case["seconds"])
                + "</p>",
            ]
        )
        for stage, label in (("initial", "初回意見"), ("final", "最終案")):
            parts.append(f"<h3>{label}</h3>")
            for slot, name in NAMES.items():
                parts.append(f"<h4>{name}</h4><div class='pair'>")
                for branch, branch_label in BRANCHES.items():
                    parts.append(f"<article class='answer'><strong>{branch_label}</strong>")
                    speech = next(
                        (
                            x
                            for x in case["runs"].get(branch, {}).get(stage, [])
                            if x["participant"] == slot
                        ),
                        None,
                    )
                    if speech is None:
                        parts.append("<p>未生成</p>")
                    else:
                        for key in ("summary", "title", "proposal"):
                            if key in speech:
                                parts.append(
                                    f"<small>{ {'summary': '要約', 'title': '題名', 'proposal': '提案'}[key] }</small><pre>{esc(speech[key])}</pre>"
                                )
                    parts.append("</article>")
                parts.append("</div>")
        parts.append("</section>")
    return "".join(parts) + "</main></html>"


def save_report(directory: Path, report: dict[str, Any]) -> None:
    # Generated private artifacts, never source files. Atomic checkpoints survive a later failure.
    for name, content in (
        ("answers.json", json.dumps(report, ensure_ascii=False, indent=2)),
        ("comparison.html", render_html(report)),
    ):
        pending = directory / (name + ".pending")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        pending.replace(directory / name)


def private_directory(parent: Path) -> Path:
    repo = Path(__file__).resolve().parents[1]
    parent = parent.resolve(strict=True)
    if parent == repo or repo in parent.parents:
        raise ValueError("private output must be outside repository")
    return Path(tempfile.mkdtemp(prefix="human-comparison-", dir=parent))


async def run(commit: str, parent: Path, seed: int) -> None:
    directory = private_directory(parent)
    print(json.dumps({"report_directory": str(directory)}), flush=True)
    catalog = query(
        "--index-name",
        "gsi1",
        "--key-condition-expression",
        "gsi1pk = :key",
        "--expression-attribute-values",
        '{":key":{"S":"ARCHIVE#COMPLETED"}}',
        "--projection-expression",
        "PK,question,completed_at",
    )
    chosen = select_ten(catalog, seed)
    baseline = load_baseline(commit)
    service = await load_service()
    usage = UsageCollector()
    service.recorder = usage
    report: dict[str, Any] = {
        "state": "生成中",
        "completed": 0,
        "started_at": datetime.now(UTC).isoformat(),
        "baseline_commit": commit,
        "model": service.config.model,
        "population": len(catalog),
        "seed_hex": hex(seed),
        "affection": 500,
        "evidence": "empty",
        "cases": [
            {
                "case": i + 1,
                "date": x["completed_at"]["S"][:10],
                "question": x["question"]["S"],
                "runs": {},
                "seconds": {},
                "intervention": {},
            }
            for i, x in enumerate(chosen)
        ],
    }
    try:
        save_report(directory, report)
        cases: list[dict[str, Any]] = report["cases"]
        for i, case in enumerate(cases):
            for branch in ("released", "revised") if i % 2 == 0 else ("revised", "released"):
                start = monotonic()
                if branch == "released":
                    case["runs"][branch] = await generate_speech(
                        service, case["question"], baseline
                    )
                else:
                    case["runs"][branch], case["intervention"] = await generate(
                        service, case["question"], i
                    )
                case["seconds"][branch] = round(monotonic() - start, 1)
                report["usage"] = usage_summary(usage)
                save_report(directory, report)
                print(json.dumps({"case": i + 1, "branch_completed": branch}), flush=True)
            report["completed"] += 1
            save_report(directory, report)
        report["state"] = "完了"
        report["finished_at"] = datetime.now(UTC).isoformat()
        save_report(directory, report)
        print(json.dumps({"completed": report["completed"], "usage": report["usage"]}), flush=True)
    except Exception as error:
        report["state"] = "中断"
        report["error_type"] = type(error).__name__
        report["usage"] = usage_summary(usage)
        save_report(directory, report)
        raise
    finally:
        await service.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--private-report", action="store_true", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    parser.add_argument("--seed", type=lambda s: int(s, 0), default=0x29C5A46C9031A6A6)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args.baseline_commit, args.output_parent, args.seed))
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "status": "stopped"}), flush=True)
        raise SystemExit(1) from None
