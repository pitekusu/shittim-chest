"""Replay real voting rules against ten fixed answers; retain transparent private ballots."""

# ruff: noqa: E501, RUF001 -- Japanese report text and embedded markup.

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from shittim_chest.adapters.openai import OpenAIResponsesService
from shittim_chest.adapters.openai.composite_ballot import score_composite_ballot
from shittim_chest.adapters.openai.schemas import VoteOutputV1
from shittim_chest.domain import (
    PARTICIPANTS,
    EvidenceBundle,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    Vote,
    select_winner,
)
from shittim_chest.domain.composite_voting import CompositeBallot, select_composite_winner
from tools.compare_for_humans import NAMES, private_directory
from tools.evaluate_deliberation import _parallel, load_baseline, load_service, usage_summary
from tools.evaluate_escalation import UsageCollector

AXES = ("entertainment", "character", "originality", "responsiveness", "interaction")
WEIGHTS = (5, 5, 4, 4, 2)
AXIS_NAMES = ("面白さ", "キャラクター性", "独創性", "質問への対応", "議論への反応")


def read_source(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    source = json.loads(raw)
    if (
        source.get("state") != "完了"
        or source.get("completed") != 10
        or len(source.get("cases", [])) != 10
    ):
        raise ValueError("ten completed saved comparisons required")
    if len({x["case"] for x in source["cases"]}) != 10:
        raise ValueError("duplicate saved case")
    for case in source["cases"]:
        if not isinstance(case["question"], str) or not case["question"].strip():
            raise ValueError("invalid saved question")
        for stage in ("initial", "final"):
            rows = case["runs"]["revised"][stage]
            if tuple(x["participant"] for x in rows) != tuple(PARTICIPANTS):
                raise ValueError("invalid saved participant coverage")
    return source, hashlib.sha256(raw).hexdigest()


async def legacy_vote(
    service: OpenAIResponsesService,
    baseline: ModuleType,
    voter: ParticipantSlot,
    question: str,
    candidates: tuple[FinalProposal, ...],
) -> Vote:
    result = await service._parse(
        operation="saved_legacy_vote",
        schema=VoteOutputV1,
        instructions=baseline.private_participant_instructions(
            service.profiles.for_participant(voter).system_prompt,
            system_prompt=service.system_prompt,
        ),
        input_text=baseline.vote_input(question, EvidenceBundle(), candidates),
        settings=service.config.vote,
    )
    return Vote(
        voter,
        result.candidate_id,
        result.accuracy_score,
        result.usefulness_score,
        result.safety_score,
        result.reason,
    )


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for scheme in ("legacy", "composite"):
        complete = [
            c
            for c in cases
            if all(f"{scheme}_{order}" in c["results"] for order in ("normal", "reversed"))
        ]
        counts = {
            order: dict(Counter(c["results"][f"{scheme}_{order}"]["winner"] for c in complete))
            for order in ("normal", "reversed")
        }
        summary[scheme] = {
            "completed_cases": len(complete),
            "wins": counts,
            "winner_changed_cases": [
                c["case"]
                for c in complete
                if c["results"][f"{scheme}_normal"]["winner"]
                != c["results"][f"{scheme}_reversed"]["winner"]
            ],
        }
    all_assessments = [
        a
        for c in cases
        for key, result in c["results"].items()
        if key.startswith("composite_")
        for vote in result["ballots"]
        for a in vote["assessments"]
    ]
    summary["composite_mean_axes"] = {}
    for slot in PARTICIPANTS:
        rows = [a for a in all_assessments if a["candidate"] == slot]
        if rows:
            summary["composite_mean_axes"][slot] = {
                "observations": len(rows),
                **{axis: round(sum(a[axis] for a in rows) / len(rows), 3) for axis in AXES},
                "total": round(sum(a["total"] for a in rows) / len(rows), 3),
            }
    return summary


def render(report: dict[str, Any]) -> str:
    def e(value: object) -> str:
        return html.escape(str(value), quote=True)

    def name(value: str) -> str:
        return NAMES[value]

    summary = aggregate(report["cases"])
    parts = [
        "<!doctype html><html lang='ja'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        "<title>保存済み10回答の投票比較</title><style>body{font:16px/1.7 system-ui;background:#eef5f7;color:#17313c;margin:0}main{max-width:1300px;margin:auto;padding:24px}section,article{background:white;border:1px solid #c3d7df;border-radius:12px;padding:20px;margin:20px 0}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{text-align:left;padding:8px;border-bottom:1px solid #c3d7df}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}.scroll{overflow:auto}nav{display:flex;gap:12px;flex-wrap:wrap}a{color:#08748c}small{color:#446573}summary{cursor:pointer;font-weight:700}.note{background:#e0f0f4;padding:16px}h1,h2,h3{line-height:1.4}@media(max-width:600px){main{padding:12px}section,article{padding:12px}}</style><main>",
        "<h1>同じ10回答に対する投票方式の比較</h1>",
        f"<p>状態：{e(report['state'])} ／ 投票セット完了：{sum(len(c['results']) for c in report['cases'])} / 40</p>",
        "<p class='note'>入力は前回生成した改修v2の回答で固定し、回答の再生成はしていません。ここにある採点は実際の投票ロジックによるものです。別の評価AIによる回答の優劣判定ではありません。勝者はPythonが決めます。</p>",
        "<p>各質問で、現行投票と総合評価をそれぞれ2回実行しました。「通常順／逆順」は投票者が見る他の2人の候補の順番で、議論の順序ではありません。同じ回答なので独立した20問ではなく10問×2回です。順番の影響と確率的な揺らぎは分離できません。</p>",
        "<p>現行：投票者が1案を選び、正確性・有用性・安全性を各1～5点で採点。多数決後の同票では得点、項目別得点、最後に固定優先順を使用します。総合評価：他の2案を各5項目0～5点で採点し、重み付き合計が高い方へPythonが投票。多数決→両投票者の合計点→固定キーによる抽選で決定します。</p>",
        "<p>総合評価の重み：面白さ25%、キャラクター性25%、独創性20%、質問への対応20%、議論への反応10%。100点換算＝各素点×(5,5,4,4,2)の合計です。現行の15点満点とは直接比較できません。</p>",
        "<p>両方式で同じ現在の人格とgpt-5.6-luna、空Evidenceを使用。総合評価だけが議論への反応を採点するため初回意見も読みます。内部の判断準備資料は保存されていないため渡しません。候補名を置換しても文体や本文から作者を推測できる可能性は残ります。勝数の均等化自体を品質の目標にはしません。</p>",
        "<h2>勝数</h2><div class='scroll'><table><tr><th>投票方式・順序</th><th>アロナ</th><th>プラナ</th><th>安倍晋三AI</th></tr>",
    ]
    for scheme, label in (("legacy", "現行"), ("composite", "総合評価")):
        for order, order_name in (("normal", "通常順"), ("reversed", "逆順")):
            parts.append(
                f"<tr><td>{label}・{order_name}</td>"
                + "".join(
                    f"<td>{summary[scheme]['wins'][order].get(x, 0)}</td>" for x in PARTICIPANTS
                )
                + "</tr>"
            )
    parts.append(
        "</table></div><h2>総合評価の項目平均</h2><div class='scroll'><table><tr><th>候補</th>"
        + "".join(f"<th>{x}</th>" for x in AXIS_NAMES)
        + "<th>合計 / 100</th></tr>"
    )
    for slot, scores in summary["composite_mean_axes"].items():
        parts.append(
            f"<tr><td>{name(slot)}</td>"
            + "".join(f"<td>{scores[x]:.2f}</td>" for x in (*AXES, "total"))
            + "</tr>"
        )
    parts.append(
        "</table></div><p>項目平均は各候補が他の2人から受けた点数を、10問・2回で平均したものです。個別理由は下にすべて掲載しています。</p><nav>"
        + "".join(f"<a href='#case-{c['case']}'>質問{c['case']}</a>" for c in report["cases"])
        + "</nav>"
    )
    for case in report["cases"]:
        parts.append(
            f"<section id='case-{case['case']}'><h2>質問 {case['case']}</h2><pre>{e(case['question'])}</pre><details><summary>投票対象の回答全文を開く</summary>"
        )
        for stage, title in (("initial", "初回意見"), ("final", "最終案")):
            parts.append(f"<h3>{title}</h3>")
            for speech in case["speeches"][stage]:
                parts.append(f"<h4>{name(speech['participant'])}</h4>")
                for field in ("summary", "title", "proposal"):
                    if field in speech:
                        parts.append(f"<pre>{e(speech[field])}</pre>")
        parts.append("</details>")
        for scheme, label in (("legacy", "現行"), ("composite", "総合評価")):
            for order, order_name in (("normal", "通常順"), ("reversed", "逆順")):
                result = case["results"].get(f"{scheme}_{order}")
                if not result:
                    continue
                parts.append(
                    f"<article><h3>{label}・{order_name}：{name(result['winner'])}が勝利</h3><p>決定方法：{e(result['decided_by'])}</p>"
                )
                for ballot in result["ballots"]:
                    parts.append(f"<h4>{name(ballot['voter'])} → {name(ballot['candidate'])}</h4>")
                    if scheme == "legacy":
                        parts.append(
                            f"<p>正確性 {ballot['accuracy_score']} ／ 有用性 {ballot['usefulness_score']} ／ 安全性 {ballot['safety_score']} （合計 {ballot['total']} / 15）</p><pre>{e(ballot['reason'])}</pre>"
                        )
                    else:
                        for a in ballot["assessments"]:
                            parts.append(
                                f"<p><strong>{name(a['candidate'])}：{a['total']} / 100</strong></p><p>"
                                + " ／ ".join(
                                    f"{label} {a[axis]}"
                                    for label, axis in zip(AXIS_NAMES, AXES, strict=True)
                                )
                                + f"</p><pre>{e(a['reason'])}</pre>"
                            )
                parts.append("</article>")
        parts.append("</section>")
    return "".join(parts) + "</main></html>"


def save(directory: Path, report: dict[str, Any]) -> None:
    report["aggregate"] = aggregate(report["cases"])
    for name, content in (
        ("votes.json", json.dumps(report, ensure_ascii=False, indent=2)),
        ("votes.html", render(report)),
    ):
        pending = directory / (name + ".pending")
        fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        pending.replace(directory / name)


async def run(source_path: Path, output_parent: Path) -> None:
    source, digest = read_source(source_path)
    directory = private_directory(output_parent)
    print(json.dumps({"report_directory": str(directory)}), flush=True)
    baseline = load_baseline(source["baseline_commit"])
    service = await load_service()
    if service.config.model != source["model"]:
        await service.client.close()
        raise ValueError("saved answer model differs from current model")
    usage = UsageCollector()
    service.recorder = usage
    cases: list[dict[str, Any]] = [
        {
            "case": x["case"],
            "question": x["question"],
            "speeches": x["runs"]["revised"],
            "results": {},
        }
        for x in source["cases"]
    ]
    report: dict[str, Any] = {
        "state": "実行中",
        "source_sha256": digest,
        "model": source["model"],
        "baseline_commit": source["baseline_commit"],
        "started_at": datetime.now(UTC).isoformat(),
        "cases": cases,
    }
    try:
        save(directory, report)
        for index, case in enumerate(cases):
            initials = tuple(
                InitialOpinion(ParticipantSlot(x["participant"]), x["summary"], x["proposal"])
                for x in case["speeches"]["initial"]
            )
            proposals = tuple(
                FinalProposal(ParticipantSlot(x["participant"]), x["title"], x["proposal"])
                for x in case["speeches"]["final"]
            )
            key = f"{digest}:case:{case['case']}"
            schemes = ("legacy", "composite") if index % 2 == 0 else ("composite", "legacy")
            for order in ("normal", "reversed"):
                ordered = proposals if order == "normal" else proposals[::-1]
                for scheme in schemes:
                    if scheme == "legacy":
                        votes = await _parallel(
                            *(
                                legacy_vote(
                                    service,
                                    baseline,
                                    voter,
                                    case["question"],
                                    tuple(x for x in ordered if x.participant != voter),
                                )
                                for voter in PARTICIPANTS
                            )
                        )
                        result = select_winner(votes)
                        counts = Counter(v.candidate for v in votes)
                        record = {
                            "winner": result.winner,
                            "decided_by": "多数決"
                            if max(counts.values()) > 1
                            else "現行の同票決定規則",
                            "ballots": [{**asdict(v), "total": v.total_score} for v in votes],
                        }
                    else:
                        ballots: tuple[CompositeBallot, ...] = await _parallel(
                            *(
                                score_composite_ballot(
                                    service,
                                    voter=voter,
                                    question=case["question"],
                                    evidence=EvidenceBundle(),
                                    candidates=tuple(x for x in ordered if x.participant != voter),
                                    initial_opinions=initials,
                                )
                                for voter in PARTICIPANTS
                            )
                        )
                        result = select_composite_winner(ballots, debate_key=key)
                        record = {
                            "winner": result.winner,
                            "decided_by": {
                                "majority": "多数決",
                                "composite_score": "同票後の総合点",
                                "tie_lottery": "同点抽選",
                            }[result.decided_by],
                            "ballots": [
                                {
                                    "voter": v.voter,
                                    "candidate": v.candidate,
                                    "assessments": [
                                        {**asdict(a), "total": a.total}
                                        for a in v.ballot.assessments
                                    ],
                                }
                                for v in result.votes
                            ],
                            "totals": [asdict(x) for x in result.totals],
                        }
                    case["results"][f"{scheme}_{order}"] = record
                    report["usage"] = usage_summary(usage)
                    save(directory, report)
                    print(
                        json.dumps(
                            {
                                "case": case["case"],
                                "scheme": scheme,
                                "order": order,
                                "winner": record["winner"],
                            }
                        ),
                        flush=True,
                    )
        report["state"] = "完了"
        save(directory, report)
        print(
            json.dumps(
                {"completed": 10, "aggregate": report["aggregate"], "usage": report["usage"]}
            ),
            flush=True,
        )
    except Exception as error:
        report["state"] = "中断"
        report["error_type"] = type(error).__name__
        report["usage"] = usage_summary(usage)
        save(directory, report)
        raise
    finally:
        await service.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--private-report", action="store_true", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args.source, args.output_parent))
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "status": "stopped"}), flush=True)
        raise SystemExit(1) from None
