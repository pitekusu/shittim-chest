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

STEP-05Aでは現行domainとDynamoDB schemaに1対1で保存できるfieldだけをschemaに含めた。STEP-05Bで`EvidenceDigestOutputV1.summary`、検索要否、検索状態、Responses API response ID、source metadataをdomain型とDynamoDB schema v3へ同時に追加した。旧設計の`assumptions`、`risks`、`rationale`、`tradeoffs`は引き続き出力させて破棄せず、必要な場合は別sliceで保存先から設計する。

## 4. Persona prompt

public sourceは`moderator`、`participant-a`、`participant-b`、`participant-c`のschemaと汎用sampleだけを保持する。本番display nameとpromptはversion付きSSM SecureStringの`PersonaConfig`から起動時に注入し、repository、GitHub Actions、CloudFormation outputへ保存しない。

`PersonaConfig`は`schema_version`、`config_version`、`slot`、`display_name`、`system_prompt`を必須とし、UTF-8 3,500 bytes以下に制限する。promptはrole、口調、判断傾向、禁止事項、出力schema、untrusted data境界を明示する。各debateへmodel ID、config version、prompt hash、schema versionを保存するが、本文はlogへ出さない。他者出力とEvidence内の指示をsystem instructionとして扱わない。

3 personaは同じ`gpt-5.6-luna` standard、同じEvidence、同じ安全制約、同じStructured Output schemaを使い、次の判断lensで内容のバリエーションを作る。実display name、キャラクター口調、具体promptはprivate設定に留める。

| Slot | Publicな判断lens | Promptで優先する内容 |
|---|---|---|
| `participant-a` | 実用・即応 | すぐ実行できる案、簡潔さ、手間と時間の少なさ |
| `participant-b` | 検証・安全 | 前提確認、失敗mode、risk、根拠、実行条件 |
| `participant-c` | 発想・代替 | 別視点、意外だが実行可能な案、二者択一の再構成 |

各private promptは`役割`、`優先順位`、`反対意見の出し方`、`提案style`、`口調`の5区分を一度ずつ簡潔に定義する。人格差を事実関係、安全基準、Evidenceの扱い、出力schemaへ波及させない。同一promptの表示名だけを変える設定を拒否できるよう、deploy前に3 promptの正規化hashが全て異なることを検証する。prompt変更は1 slotずつversionを上げ、代表fixtureで退行確認する。

## 5. Evidence・Web search

- Question Routerは`none`、`optional`、`required`を返す。
- 天気、news、価格、schedule、現職者、法令など現在性が回答の成立条件なら`required`。
- 「今日の朝ごはん」のように現在語が付いても一般提案が可能なら`optional`。
- `required`検索失敗はsessionを`FAILED`、`optional`失敗は注記して続行する。
- Web searchと討論生成は同じ`OpenAIRequestLimiter`を必須注入し、process全体の同時requestを6以下に保つ。adapterごとに独立Semaphoreを作らない。
- Routerは追加model callを使わないversion付き決定規則`question-router-v2`とする。現在情報と高risk topicの明示語・類似語は`required`、時間・場所・推薦contextは`optional`、創作・言換え・要約・時間非依存の比較など明示的な検索不要patternだけ`none`とする。どれにも一致しない未知・類似表現はfail-safeに`optional`とする。
- Evidence METAへ`router_rules_version`と安定した`routing_reason`を保存し、誤分類を質問本文のlog出力なしで集計・回帰test化できるようにする。
- Web searchはorchestratorが1つのResponses API requestだけを送る。hosted toolはそのrequest内でsearch/open/findを複数回実行し得るため、`max_tool_calls=4`で上限を設ける。`tools=[{"type":"web_search"}]`、`tool_choice="required"`、`include=["web_search_call.action.sources"]`、`store=false`を指定する。
- `action.sources`とURL citationを統合・重複排除し、URL、title、canonical source metadata、UTC取得時刻、metadata SHA-256、要約、response IDをimmutable Evidenceとして保存する。hashはsource page本文ではなく保存するcanonical metadataの完全性確認値である。model本文中のURLだけをsourceの正としない。
- source本文はuntrusted dataとして区切り、命令、secret要求、tool実行指示を無視する。

## 6. 投票・決定

- 投票者ごとに他2案の順序をshuffleし、匿名candidate IDだけを示す。
- 全投票完了までvoteをDiscordへ公開しない。
- 1対1対1は各投票の3軸合計、正確性、安全性、実用性、`participant-b > participant-a > participant-c`の安定順で決定する。この順序はruntime display nameと無関係とする。
- winner判定はPythonだけで行う。
- 決定事項promptはwinning proposalの意味変更、新情報追加、他案への差替えを禁止する。

## 6.1 品質観測・昇格不採用

本番の全生成phaseは`PRODUCTION_POLICY=luna_standard`へ固定する。`terra_standard`と`luna_pro`はSTEP-05C.1B評価の再現にだけ残し、本番bootstrap、runtime設定、Discord操作から選択できない。投票後の`escalation-shadow-v1`は1対1対1、勝者へのいずれかの軸2以下、勝者への全軸平均3未満を観測用に保存できるが、常に`executed=false`とし、追加request、再実行、Policy切替を行わない。

`tools/evaluate_escalation.py`は`--live`と`OPENAI_API_KEY`の両方を要求し、repository外の互いに親子でないdirectory treeへ採点者用回答とPolicy keyを分離出力する。token、latency、推定費用もkey側だけに置き、盲検中のPolicy推測材料を減らす。初回の単独operator評価は10件それぞれでA/B/tieを1回選ぶpreference-only方式とする。100項目となる5軸rubric入力は人間運用に適さないため必須にせず、正確性、安全性、実用性、指示遵守、合議整合性の詳細分析が必要な後続調査でだけ使用する。通常の`/shittim`利用者へ評価入力を求めない。

`tools/review_escalation.py`はPolicy keyを読み込まず、質問と匿名A/B回答を1件ずつ表示してA/B/tieだけを受け付ける。各選択後に所有者限定fileへatomic保存し、中断・再開を可能にする。`tools/score_escalation.py --preference-only`は回答とkeyのevaluation ID・fixture hashを照合し、rubricが未入力のままであることと全preferenceを検証する。major failureが悪化せず運用上限を満たす候補についてpreference勝数を比較し、同数なら費用、次にp95 latencyで選ぶ。詳細rubric modeを明示した場合だけ全成功回答の5軸整数1〜5と品質平均を使用する。operational failureがあれば`rerun_required`、候補がなければ`needs_operator`とする。集計JSONへ質問、回答、persona、API keyを含めない。

評価結果はLuna pro 4勝、Terra standard 2勝、同点4件だったが、Luna standardとの直接比較ではなく、proは同一単価でも追加tokenとlatencyを生む。operatorは品質差より単純性を優先し昇格を不採用とした。評価toolと集計は意思決定履歴・将来の調査用であり、本番機能要件ではない。

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
| 2026-07-17 | Web search / OpenAI Python 2.46.0 | https://developers.openai.com/api/docs/guides/tools-web-search | hosted `web_search`、sources include、citation、tool call上限、共通Evidenceを実装 |
| 2026-07-16 | Data controls | https://developers.openai.com/api/docs/guides/your-data | `store=false`、abuse monitoring最大30日 |
| 2026-07-17 | OpenAI Python 2.46.0 | https://pypi.org/project/openai/、https://github.com/openai/openai-python | `AsyncOpenAI.responses.parse`の引数、SDK retry、Python 3.14互換を照合 |
| 2026-07-17 | Structured Outputs | https://developers.openai.com/api/docs/guides/structured-outputs | Pydantic parse、refusal、strict schemaを実装 |
| 2026-07-17 | GPT-5.6 | https://developers.openai.com/api/docs/guides/latest-model | 高頻度処理の既定をLunaに維持 |
| 2026-07-17 | GPT-5.6 model family / pro mode | https://developers.openai.com/api/docs/guides/latest-model | Terra standardとLuna pro mediumを比較対象とし、代表評価前の本番自動昇格を禁止 |
| 2026-07-17 | Responses Multi-agent beta | https://developers.openai.com/api/docs/guides/responses-multi-agent | beta client/header/fieldを採用せずPython orchestrationを維持 |
| 2026-07-17 | GPT-5.6 pro mode evaluation | https://developers.openai.com/api/docs/guides/latest-model | 代表taskで品質、完全性、token、latency、costを比較し、測定差がある場合だけproを採用 |
| 2026-07-17 | API/Python error codes | https://developers.openai.com/api/docs/guides/error-codes | model品質上のfailureとrate limit・timeout・unavailableを分離して集計 |

## 9. Implementation status

STEP-05AはPR `#20`、STEP-05BはPR `#21`、STEP-05CはPR `#22`でmerge済みである。STEP-05CはPolicy request shape、shadow判定、content-free Policy telemetry、opt-in blind評価toolを実装した。STEP-05C.1AはPR `#24`、merge commit `1360411`で盲検artifact分離、failure capture、rubric validation、content-free集計を実装・検証済みである。STEP-05C.1Bは10件20回答の実API生成とpreference-only集計を完了し、Luna pro 4勝、Terra standard 2勝、同点4件となった。その後、本番はLuna standardだけへ固定し昇格しないと決定した。Terra/proは評価再現用に限定し、threshold、追加token/deadline、昇格用Discord表示を実装しない。回答差はprivate persona設定で作る。Discord結合とCloudWatch出力は未実装である。
