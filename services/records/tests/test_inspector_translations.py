"""Inspector description translation, cache, and provider-boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from shittim_chest.adapters.dynamodb.codec import marshal_item

import shittim_records.inspector_translation_adapters as adapters
from shittim_records.inspector_translation_adapters import (
    AwsInspectorDescriptionSource,
    DynamoInspectorTranslationStore,
    InspectorTranslationConfigurationRepository,
    OpenAIInspectorSummaryTranslator,
)
from shittim_records.inspector_translations import (
    INSPECTOR_SUMMARY_MAX_CHARS,
    INSPECTOR_SUMMARY_MIN_CHARS,
    INSPECTOR_TRANSLATIONS_PER_RUN,
    InspectorJapaneseSummary,
    InspectorTranslationService,
    InspectorTranslationUnavailable,
    inspector_description,
    normalize_inspector_summary,
)

NOW = datetime(2026, 8, 29, 4, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
DESCRIPTION = (
    "A boundary validation flaw allows a remote attacker to submit malformed input and cause "
    "the process to read outside its intended memory region."
)
SUMMARY_JA = (
    "入力値の境界確認が不十分なため、遠隔の攻撃者が細工したデータを送ると、対象プロセスが本来の範囲外にある"
    "メモリを読み取る可能性があります。その結果、処理の異常終了や、プロセス内で扱われる情報の一部が意図せず"
    "露出するおそれがある脆弱性です。"
)


class Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.pages


def finding(*, description: str = DESCRIPTION, digest: str = DIGEST) -> dict[str, Any]:
    return {
        "description": description,
        "severity": "CRITICAL",
        "resources": [
            {
                "type": "AWS_ECR_CONTAINER_IMAGE",
                "details": {"awsEcrContainerImage": {"imageHash": digest}},
            }
        ],
        "packageVulnerabilityDetails": {
            "vulnerabilityId": "CVE-2026-12345",
            "vulnerablePackages": [{"name": "example", "version": "1.0"}],
        },
    }


def summary_for(source: Any, *, text: str = SUMMARY_JA) -> InspectorJapaneseSummary:
    return InspectorJapaneseSummary(
        key=source.key,
        vulnerability_id=source.vulnerability_id,
        source_sha256=source.source_sha256,
        summary_ja=text,
        translated_at=NOW,
    )


def test_summary_contract_is_100_to_300_unicode_characters() -> None:
    assert INSPECTOR_SUMMARY_MIN_CHARS == 100
    assert INSPECTOR_SUMMARY_MAX_CHARS == 300
    assert 100 <= len(normalize_inspector_summary(SUMMARY_JA)) <= 300
    with pytest.raises(ValueError, match="Japanese summary"):
        normalize_inspector_summary("短すぎます。")
    with pytest.raises(ValueError, match="Japanese summary"):
        normalize_inspector_summary("長" * 301)


def test_description_identity_is_stable_after_normalization() -> None:
    first = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=f"  {DESCRIPTION}\r\n",
    )
    second = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=f"{DESCRIPTION}\n",
    )

    assert first == second
    assert DESCRIPTION not in first.key
    assert len(first.key) == 64
    assert len(first.source_sha256) == 64


def test_service_translates_only_first_bounded_unseen_items_in_batches() -> None:
    sources = tuple(
        inspector_description(
            vulnerability_id=f"CVE-2026-{index:05d}",
            description=f"{DESCRIPTION} Finding {index}.",
        )
        for index in range(INSPECTOR_TRANSLATIONS_PER_RUN + 3)
    )

    class Source:
        def list_descriptions(self) -> tuple[Any, ...]:
            return sources

    class Store:
        def __init__(self) -> None:
            self.saved: list[InspectorJapaneseSummary] = []

        def load(self, keys: tuple[str, ...]) -> dict[str, InspectorJapaneseSummary]:
            assert keys == tuple(source.key for source in sources)
            return {sources[0].key: summary_for(sources[0])}

        def save(self, summaries: tuple[InspectorJapaneseSummary, ...]) -> None:
            self.saved.extend(summaries)

    class Translator:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def translate(
            self,
            descriptions: tuple[Any, ...],
            *,
            translated_at: datetime,
        ) -> tuple[InspectorJapaneseSummary, ...]:
            assert translated_at == NOW
            self.batch_sizes.append(len(descriptions))
            return tuple(summary_for(source) for source in descriptions)

    store = Store()
    translator = Translator()
    result = InspectorTranslationService(
        source=Source(),
        translator=translator,
        store=store,
    ).refresh(now=NOW)

    assert result.discovered == INSPECTOR_TRANSLATIONS_PER_RUN + 3
    assert result.cached == 1
    assert result.translated == INSPECTOR_TRANSLATIONS_PER_RUN
    assert result.remaining == 2
    assert translator.batch_sizes == [10, 10, 10, 10, 10]
    assert len(store.saved) == INSPECTOR_TRANSLATIONS_PER_RUN


def test_aws_source_keeps_only_tagged_active_critical_or_high_descriptions() -> None:
    ecr_pages = Paginator(
        [
            {
                "imageDetails": [
                    {"imageDigest": DIGEST, "imageTags": ["release", "stable"]},
                ]
            }
        ]
    )
    untagged_digest = "sha256:" + "b" * 64
    inspector_pages = Paginator(
        [
            {
                "findings": [
                    finding(),
                    finding(),
                    finding(description="Not selected", digest=untagged_digest),
                    {**finding(description="Not selected"), "severity": "MEDIUM"},
                ]
            }
        ]
    )

    class Ecr:
        def get_paginator(self, name: str) -> Paginator:
            assert name == "describe_images"
            return ecr_pages

    class Inspector:
        def get_paginator(self, name: str) -> Paginator:
            assert name == "list_findings"
            return inspector_pages

    sources = AwsInspectorDescriptionSource(
        ecr=cast(Any, Ecr()),
        inspector=cast(Any, Inspector()),
        repository_name="shittim-chest",
    ).list_descriptions()

    assert len(sources) == 1
    assert sources[0].description == DESCRIPTION
    assert ecr_pages.calls == [
        {
            "repositoryName": "shittim-chest",
            "filter": {"tagStatus": "TAGGED"},
            "PaginationConfig": {"PageSize": 100},
        }
    ]
    assert inspector_pages.calls[0]["filterCriteria"]["findingStatus"] == [
        {"comparison": "EQUALS", "value": "ACTIVE"}
    ]
    assert inspector_pages.calls[0]["filterCriteria"]["severity"] == [
        {"comparison": "EQUALS", "value": "CRITICAL"},
        {"comparison": "EQUALS", "value": "HIGH"},
    ]
    assert inspector_pages.calls[0]["PaginationConfig"] == {"PageSize": 100}


def test_openai_translator_uses_luna_structured_stateless_output() -> None:
    source = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=DESCRIPTION,
    )
    calls: list[dict[str, Any]] = []

    class Responses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            parsed = adapters._ProviderTranslatedBatch.model_validate(
                {"summaries": [{"key": source.key, "summary_ja": SUMMARY_JA}]}
            )
            return SimpleNamespace(output_parsed=parsed)

    client = SimpleNamespace(responses=Responses())
    configuration = InspectorTranslationConfigurationRepository(
        cast(Any, SimpleNamespace()),
        "/shittim-chest/production/records/openai/inspector-translation-api-key",
    )
    summaries = OpenAIInspectorSummaryTranslator(
        configuration,
        client=client,
    ).translate((source,), translated_at=NOW)

    assert summaries == (summary_for(source),)
    assert calls[0]["model"] == "gpt-5.6-luna"
    assert calls[0]["store"] is False
    assert calls[0]["tools"] == []
    assert calls[0]["reasoning"] == {"effort": "none"}
    assert calls[0]["text_format"] is adapters._ProviderTranslatedBatch
    provider_schema = json.dumps(calls[0]["text_format"].model_json_schema())
    assert all(
        constraint not in provider_schema
        for constraint in ("minLength", "maxLength", "minItems", "maxItems", "pattern")
    )
    payload = json.loads(calls[0]["input"])
    assert payload["items"][0]["description"] == DESCRIPTION


def test_openai_translator_rejects_semantically_invalid_structured_output() -> None:
    source = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=DESCRIPTION,
    )

    calls = 0

    class Responses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            parsed = kwargs["text_format"].model_validate(
                {"summaries": [{"key": source.key, "summary_ja": "短すぎます。"}]}
            )
            return SimpleNamespace(output_parsed=parsed)

    configuration = InspectorTranslationConfigurationRepository(
        cast(Any, SimpleNamespace()),
        "/shittim-chest/production/records/openai/inspector-translation-api-key",
    )
    translator = OpenAIInspectorSummaryTranslator(
        configuration,
        client=SimpleNamespace(responses=Responses()),
    )

    with pytest.raises(InspectorTranslationUnavailable) as caught:
        translator.translate((source,), translated_at=NOW)

    assert caught.value.code == "provider_output_invalid"
    assert calls == 2


def test_openai_translator_retries_one_semantically_invalid_output() -> None:
    source = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=DESCRIPTION,
    )
    calls = 0

    class Responses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            summary = "短すぎます。" if calls == 1 else SUMMARY_JA
            parsed = kwargs["text_format"].model_validate(
                {"summaries": [{"key": source.key, "summary_ja": summary}]}
            )
            return SimpleNamespace(output_parsed=parsed)

    configuration = InspectorTranslationConfigurationRepository(
        cast(Any, SimpleNamespace()),
        "/shittim-chest/production/records/openai/inspector-translation-api-key",
    )
    translator = OpenAIInspectorSummaryTranslator(
        configuration,
        client=SimpleNamespace(responses=Responses()),
    )

    summaries = translator.translate((source,), translated_at=NOW)

    assert summaries == (summary_for(source),)
    assert calls == 2


def test_dynamo_cache_round_trip_never_stores_the_english_description() -> None:
    source = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=DESCRIPTION,
    )
    summary = summary_for(source)
    table_name = "statistics"
    sdk = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )
    stored = {
        "PK": "ADMIN#INSPECTOR_TRANSLATION",
        "SK": f"SUMMARY#{source.key}",
        "schema_version": 1,
        "record_type": "inspector_translation",
        "vulnerability_id": source.vulnerability_id,
        "source_sha256": source.source_sha256,
        "summary_ja": SUMMARY_JA,
        "model": "gpt-5.6-luna",
        "translated_at": NOW.isoformat(),
    }
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "batch_get_item",
            {"Responses": {table_name: [marshal_item(stored)]}},
            {
                "RequestItems": {
                    table_name: {
                        "Keys": [
                            marshal_item(
                                {
                                    "PK": "ADMIN#INSPECTOR_TRANSLATION",
                                    "SK": f"SUMMARY#{source.key}",
                                }
                            )
                        ],
                        "ConsistentRead": True,
                    }
                }
            },
        )
        stubber.add_response(
            "put_item",
            {},
            {
                "TableName": table_name,
                "Item": marshal_item(stored),
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            },
        )
        store = DynamoInspectorTranslationStore(sdk, table_name)
        loaded = store.load((source.key,))
        store.save((summary,))

    assert loaded == {source.key: summary}
    assert DESCRIPTION not in json.dumps(stored, ensure_ascii=False)


def test_configuration_repository_rejects_missing_key_without_exposing_values() -> None:
    sdk = boto3.client(
        "ssm",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )
    name = "/shittim-chest/production/records/openai/inspector-translation-api-key"
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_parameters",
            {"Parameters": [], "InvalidParameters": [name]},
            {"Names": [name], "WithDecryption": True},
        )
        repository = InspectorTranslationConfigurationRepository(sdk, name)
        with pytest.raises(InspectorTranslationUnavailable) as caught:
            repository.load_api_key()

    assert caught.value.code == "configuration_unavailable"
