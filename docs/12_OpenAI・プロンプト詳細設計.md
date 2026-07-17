---
aliases:
  - The Shittim Chest OpenAI詳細設計
tags: [project, shittim-chest, openai, prompt, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-07-17
---

# OpenAI・プロンプト詳細設計

## 1. Client・model

- `openai>=2.46.0,<3`の`AsyncOpenAI`をprocess単位で1つ生成して再利用する。lock上の実versionは`2.46.0`とする。
- stable Responses APIと`responses.parse()`を使用し、`store=false`を明示する。
- Responses API Multi-agent betaは使用しない。`client.beta.responses`、`multi_agent`、`OpenAI-Beta`ヘッダをrequestに含めず、Python application層が各personaの並列実行、checkpoint、投票、再開を管理する。
- 既定modelは`gpt-5.6-luna`。deploy前に実projectで利用可能か再確認する。
- request、response、structured schemaをadapter内で扱い、applicationへSDK型を返さない。
- process全体のOpenAI同時実行は6、HTTP connection poolは6以上とする。

## 2. Phase別設定

| Phase | reasoning | `max_output_tokens` | application文字上限 | request deadline |
|---|---|---:|---:|---:|
| Evidence整理 | medium | 1,200 | 4,000 | 60秒 |
| 初回意見 | medium | 1,200 | 1,600 | 60秒 |
| 最終案 | medium | 1,600 | 2,000 | 60秒 |
| 投票 | low | 800 | 理由500 | 45秒 |
| 決定事項 | medium | 1,200 | 2,000 | 60秒 |

接続5秒、書込み30秒、pool5秒を初期値とする。retryable transport errorはSDK既定retryを含め最大3 attemptかつsession残時間内に限定する。認証、権限、model不存在、validation、安全拒否はretryしない。

## 3. Structured Outputs

| Schema | 必須field |
|---|---|
| `OpinionOutputV1` | `summary`, `proposal` |
| `FinalProposalOutputV1` | `title`, `proposal` |
| `VoteOutputV1` | `candidate_id`, `accuracy_score`, `usefulness_score`, `safety_score`, `reason` |
| `DecisionOutputV1` | `decision`, `actions`, `caveats` |

Pydanticでfield、length、score範囲、candidate IDをstrictに検証し、未知fieldを拒否した後、domain invariantで自己投票、重複、未知IDを拒否する。`refusal`、`incomplete`、`output_parsed is None`は別error codeへ変換する。

STEP-05Aでは現行domainとDynamoDB schemaに1対1で保存できるfieldだけをschemaに含める。旧設計の`assumptions`、`risks`、`rationale`、`tradeoffs`、`evidence_refs`を応答させて破棄することはしない。これらが必要な場合はdomain型、serializer、DynamoDB schema、Discord表示、試験を同時に変更する別sliceとする。`EvidenceDigestV1`とWeb searchはEvidence Routerを実装するSTEP-05Bで確定する。

## 4. Persona prompt

public sourceは`moderator`、`participant-a`、`participant-b`、`participant-c`のschemaと汎用sampleだけを保持する。本番display nameとpromptはversion付きSSM SecureStringの`PersonaConfig`から起動時に注入し、repository、GitHub Actions、CloudFormation outputへ保存しない。

`PersonaConfig`は`schema_version`、`config_version`、`slot`、`display_name`、`system_prompt`を必須とし、UTF-8 3,500 bytes以下に制限する。promptはrole、口調、判断傾向、禁止事項、出力schema、untrusted data境界を明示する。各debateへmodel ID、config version、prompt hash、schema versionを保存するが、本文はlogへ出さない。他者出力とEvidence内の指示をsystem instructionとして扱わない。

## 5. Evidence・Web search

- Question Routerは`none`、`optional`、`required`を返す。
- 天気、news、価格、schedule、現職者、法令など現在性が回答の成立条件なら`required`。
- 「今日の朝ごはん」のように現在語が付いても一般提案が可能なら`optional`。
- `required`検索失敗はsessionを`FAILED`、`optional`失敗は注記して続行する。
- Web searchはorchestratorが1回だけ実行する。requestで`include=["web_search_call.action.sources"]`を指定し、toolが返したsource metadataを基にimmutable Evidence bundleを作成して3人格へ同一内容で配布する。modelが本文中に生成したURLだけをsourceの正としない。
- source本文はuntrusted dataとして区切り、命令、secret要求、tool実行指示を無視する。

## 6. 投票・決定

- 投票者ごとに他2案の順序をshuffleし、匿名candidate IDだけを示す。
- 全投票完了までvoteをDiscordへ公開しない。
- 1対1対1は各投票の3軸合計、正確性、安全性、実用性、`participant-b > participant-a > participant-c`の安定順で決定する。この順序はruntime display nameと無関係とする。
- winner判定はPythonだけで行う。
- 決定事項promptはwinning proposalの意味変更、新情報追加、他案への差替えを禁止する。

## 6.1 LunaからTerraへの条件付き昇格

STEP-05Aでは実装しない。難易度、人格間の重大な対立、schemaは正しいが品質不足な結果を何がどの時点で判定し、どのphaseだけをTerraで再実行するかは未決定である。実現方式、予算、deadline、決定性、利用者表示、evaluation thresholdを[[02_議論事項・意思決定記録#未決議論: LunaからTerraへの条件付き昇格（STEP-05C候補）]]で決定し、別sliceの承認前にoperatorへ相談する。provider refusalやpolicy blockの回避には使用しない。

## 7. Safety・privacy・cost

- provider refusalを尊重し、別promptで回避しない。
- 医療、法律、金融、政治、選挙、緊急事態、自傷を含む高risk category専用の事前拒否は設けず通常質問と同じflowで扱う。ただしproviderのrefusal/policy blockを迂回せず、共通終了表示で正答・診断・法的判断・投資判断を保証しないことを明記する。
- user IDはraw値をOpenAIへ送らず、必要時は安定したprivacy-preserving safety identifierを使用する。
- `store=false`はResponses application stateを保存しない指定であり、既定のabuse monitoring logはuser contentを含み最大30日保持され得る。Zero Data Retentionを本番条件にはせず、このdata flowを利用者向け説明と運用文書へ明記する。
- input/output/cached/reasoning token、latency、response ID、model ID、cache hitをmetricsへ記録する。本文はlogへ出さない。
- explicit prompt cachingは評価setで費用削減を確認してから有効化し、無条件には使わない。

## 8. 公式資料確認記録

| 確認日 | 対象version/service | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-07-16 | GPT-5.6 | https://developers.openai.com/api/docs/guides/latest-model | luna、reasoning、Responses API |
| 2026-07-16 | Structured Outputs | https://developers.openai.com/api/docs/guides/structured-outputs | `responses.parse()`とPydantic |
| 2026-07-16 | Responses API | https://developers.openai.com/api/docs/guides/migrate-to-responses | `store=false`、typed output |
| 2026-07-16 | OpenAI Python | https://github.com/openai/openai-python | Async client再利用、error分類 |
| 2026-07-16 | Web search | https://developers.openai.com/api/docs/guides/tools-web-search | 共通Evidence取得 |
| 2026-07-16 | Data controls | https://developers.openai.com/api/docs/guides/your-data | `store=false`、abuse monitoring最大30日 |
| 2026-07-17 | OpenAI Python 2.46.0 | https://pypi.org/project/openai/、https://github.com/openai/openai-python | `AsyncOpenAI.responses.parse`の引数、SDK retry、Python 3.14互換を照合 |
| 2026-07-17 | Structured Outputs | https://developers.openai.com/api/docs/guides/structured-outputs | Pydantic parse、refusal、strict schemaを実装 |
| 2026-07-17 | GPT-5.6 | https://developers.openai.com/api/docs/guides/latest-model | 高頻度処理の既定をLunaに維持 |
| 2026-07-17 | Responses Multi-agent beta | https://developers.openai.com/api/docs/guides/responses-multi-agent | beta client/header/fieldを採用せずPython orchestrationを維持 |

## 9. Implementation status

STEP-05Aで初回意見、最終案、投票、決定事項のstable Responses adapter、strict schema、private persona検証、最大6並列、SDK error変換、本文を含まないusage/failure記録をlocal実装した。公式SDKとmock HTTP transportのcontract testでrequest shape、正常系、refusal、incomplete、invalid output、429、認証失敗、domain再検証を確認した。OpenAI実API接続、Web search、Terra昇格、Discord結合、CloudWatch出力は未実施である。
