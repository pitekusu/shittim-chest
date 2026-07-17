"""Deterministic and auditable routing for external evidence lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass

from shittim_chest.domain import SearchRequirement

_REQUIRED_TOPICS = re.compile(
    r"天気|空模様|気温|降水|台風|災害|地震|津波|ニュース|報道|株価|為替|"
    r"価格|値段|料金|相場|レート|投資|時刻表|ダイヤ|運行|試合結果|順位|日程|"
    r"法律|法令|規制|医療|症状|薬|選挙|現職|大統領|首相|総理|知事|市長|"
    r"weather|forecast|news|headline|stock|exchange rate|price|schedule|score|"
    r"standings|law|regulation|current president|current prime minister",
    re.IGNORECASE,
)
_CURRENT_FACT = re.compile(
    r"最新|現在|現時点|今の|直近|速報|今年|今日の(?:天気|ニュース|価格|予定)|"
    r"latest|current|right now|today(?:'s)? (?:weather|news|price|schedule)",
    re.IGNORECASE,
)
_OPTIONAL_CONTEXT = re.compile(
    r"今日|今夜|明日|今週|最近|近く|周辺|おすすめ|どこ|"
    r"today|tonight|tomorrow|this week|recent|nearby|recommend|where",
    re.IGNORECASE,
)
_NO_SEARCH = re.compile(
    r"物語|詩|短歌|俳句|創作|アイデアを出|言い換え|要約して|"
    r"(?:比較|違いを説明|仕組みを説明|一般的な.+方法)|"
    r"write (?:a |an )?(?:story|poem)|rewrite|summari[sz]e|brainstorm|"
    r"compare|explain (?:the )?(?:difference|concept)|general method",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class QuestionRoute:
    """Persistable classification evidence for one router decision."""

    requirement: SearchRequirement
    rules_version: str
    reason: str


@dataclass(frozen=True, slots=True)
class DeterministicQuestionRouter:
    """Classify questions without an extra model call or mutable rules."""

    rules_version: str = "question-router-v2"

    def route(self, question: str) -> QuestionRoute:
        """Return a fail-safe route and stable reason without a model call."""

        normalized = " ".join(question.split())
        if not normalized:
            raise ValueError("question must not be empty")
        if _REQUIRED_TOPICS.search(normalized) or _CURRENT_FACT.search(normalized):
            return QuestionRoute(SearchRequirement.REQUIRED, self.rules_version, "current_fact")
        if _OPTIONAL_CONTEXT.search(normalized):
            return QuestionRoute(SearchRequirement.OPTIONAL, self.rules_version, "context_may_help")
        if _NO_SEARCH.search(normalized):
            return QuestionRoute(SearchRequirement.NONE, self.rules_version, "explicitly_timeless")
        return QuestionRoute(SearchRequirement.OPTIONAL, self.rules_version, "unknown_expression")
