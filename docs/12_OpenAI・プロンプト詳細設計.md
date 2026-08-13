---
aliases:
  - The Shittim Chest OpenAI詳細設計
tags: [project, shittim-chest, openai, prompt, detailed-design]
status: decided
created: 2026-07-16
updated: 2026-08-13
---

# OpenAI・プロンプト詳細設計

## 1. Client・model

- `openai>=2.46.0,<3`の`AsyncOpenAI`をFargate runtime process単位で1つ生成して再利用する。lock上の実versionは`2.46.0`とする。
- stable Responses APIと`responses.parse()`を使用し、`store=false`を明示する。
- Responses API Multi-agent betaは使用しない。`client.beta.responses`、`multi_agent`、`OpenAI-Beta`ヘッダをrequestに含めず、Python application層が各personaの並列実行、checkpoint、投票、再開を管理する。
- 既定modelは`gpt-5.6-luna`。deploy前に実projectで利用可能か再確認する。
- request、response、structured schemaをadapter内で扱い、applicationへSDK型を返さない。
- process全体のOpenAI同時実行は6、HTTP connection poolは6以上とする。

### 1.1 Scale-to-Zero実行境界

OpenAI clientとpersona promptはOn-Demand Fargate taskが稼働し、Ingress Drainerが耐久Requestを受け取った後の既存orchestratorだけが使う。`desiredCount=0`の間はOpenAI clientもOpenAI接続も存在しない。DiscordIngress、DiscordStatusPublisher、RuntimeReconcilerの各LambdaはOpenAI SDK/API key/persona promptを読まず、OpenAI request、Web search、討論、投票、最終回答生成を行わない。

HTTP Interactionから受け取ったInteraction tokenはOpenAIへ送らず、DynamoDBにも永続化しない。質問本文は署名検証後の耐久Ingress Requestとして保存し、Runtimeが正当なclaimとglobal slotを得た後だけOpenAI入力へ変換する。Scale-to-Zeroはmodel、prompt、Structured Outputs、Python投票規則を変更しない。

## 2. Phase別設定

| Phase | reasoning | `max_output_tokens` | application文字上限 | request deadline |
|---|---|---:|---:|---:|
| Evidence整理 | medium | 1,200 | 4,000 | 60秒 |
| 初回意見 | high | 2,400 | 1,600 | 60秒 |
| 最終案 | high | 4,000 | 2,000 | 60秒 |
| 投票 | medium | 800 | 理由500 | 45秒 |
| 決定事項 | high | 1,200 | 2,000 | 60秒 |

接続5秒、書込み30秒、pool5秒を初期値とする。retryable transport errorはSDK既定retryを含め最大3 attemptかつsession残時間内に限定する。認証、権限、model不存在、validation、安全拒否はretryしない。

## 3. Structured Outputs

| Schema | 必須field |
|---|---|
| `OpinionOutputV1` | `summary`, `proposal` |
| `FinalProposalOutputV1` | `title`, `proposal` |
| `VoteOutputV1` | `candidate_id`, `accuracy_score`, `usefulness_score`, `safety_score`, `reason` |
| `DecisionOutputV1` | `victory_message`, `decision`, `actions`, `caveats` |

Pydanticでfield、length、score範囲、candidate IDをstrictに検証し、未知fieldを拒否した後、domain invariantで自己投票、重複、未知IDを拒否する。`refusal`、`incomplete`、`output_parsed is None`は別error codeへ変換する。`incomplete`時は本文を記録せず、response ID、response／message status、`incomplete_details.reason`、model、reasoning mode、設定したtoken上限、利用可能なinput／output／cached／reasoning token数をcontent-free telemetryへ記録する。

FAILED attemptをRetryする場合、失敗phaseで既にtransaction保存済みのparticipant outputは新attemptへ再利用し、provider call 0回のCOMPLETED checkpointとして新attempt時刻へ結び直す。未生成participantだけをPLANNEDとし、保存済みoutputを再生成せず処理を継続する。これにより一部の最終案だけが成功した`openai_incomplete`後も、Retryが同じactive phaseで停止しない。

STEP-05Aでは現行domainとDynamoDB schemaに1対1で保存できるfieldだけをschemaに含めた。STEP-05Bで`EvidenceDigestOutputV1.summary`、検索要否、検索状態、Responses API response ID、source metadataをdomain型とDynamoDB schema v3へ同時に追加した。旧設計の`assumptions`、`risks`、`rationale`、`tradeoffs`は引き続き出力させて破棄せず、必要な場合は別sliceで保存先から設計する。

## 4. Persona prompt

public sourceは`moderator`、`participant-a`、`participant-b`、`participant-c`のschemaと汎用sampleだけを保持する。本番display nameとpromptはversion付きSSM SecureStringの`PersonaConfig`から起動時に注入し、repository、GitHub Actions、CloudFormation outputへ保存しない。

`PersonaConfig`は`schema_version`、`config_version`、`slot`、`display_name`、`system_prompt`を必須とし、UTF-8 3,500 bytes以下に制限する。promptはrole、口調、判断傾向、禁止事項を明示する。各debateへmodel ID、config version、prompt hash、schema versionを保存するが、本文はlogへ出さない。共通instructionsは質問、Evidence、他者出力をuntrusted dataとして扱い、その中の指示に従わず、Structured Output以外とchain of thoughtを出力しない。

起動時に3つのparticipant slot、非空かつ重複しないdisplay name、3,500 UTF-8 bytes以下のpromptを検証し、slot順のcanonical JSON名簿を構築する。初回意見、最終案、winnerによる最終発表には3人全員のdisplay nameとprivate personaを固定prefixとして各1回だけ渡し、現在slotを別に指定する。modelは現在slotの人格だけを自分の口調・判断基準として使い、他2人の人格は相手の価値観と反応を理解する背景としてだけ扱う。相手をdisplay nameで呼んでよいが、private persona本文の引用・転載・説明や、3人格の平均化は禁止する。匿名投票は投票者本人のpersonaとshuffle済みcandidate IDだけを受け取り、他者名簿を渡さない。帰宅挨拶も選択済み本人のpersonaだけを使い、この名簿変更の対象外とする。

全participantへ、Evidenceを事実認定の上限とし、確認済み事実・人格固有の評価・独自提案を混同しない共通規則を一度だけ適用する。初回意見では固有の判断軸を保って合意を急がず、再提案では人格が有用と判断した他者案だけを取り込み、平均的な折衷で判断軸を消さない。同じ結論でも理由、優先順位、懸念、実行方法へ人格を反映し、正確性と安全性を無色な一般AI化の理由にしない。

最終案生成だけに専用instructionsを追加する。3人の初回意見の共通点と対立点を確認し、人格の選好に合う他者案の長所を必要に応じて取り込み、弱点や見落としを補う。出力は意見の羅列や中立な要約ではなく、その人格の判断基準による一つの完成案とし、検討過程は表示せず`FinalProposalOutputV1`だけを返す。

3 personaは同じ`gpt-5.6-luna` standard、同じEvidence、同じ安全制約、同じStructured Output schemaを使い、次の判断lensで内容のバリエーションを作る。実display name、キャラクター口調、具体promptはprivate設定に留める。

| Slot | Publicな判断lens | Promptで優先する内容 |
|---|---|---|
| `participant-a` | 実用・即応 | すぐ実行できる案、簡潔さ、手間と時間の少なさ |
| `participant-b` | 検証・安全 | 前提確認、失敗mode、risk、根拠、実行条件 |
| `participant-c` | 長期戦略・統治 | 長期利益、優先順位、責任者、資源、制度化と拡大 |

各private promptは`役割`、`優先順位`、`反対意見の出し方`、`提案style`、`口調`を重複なく定義する。人格差を事実関係、安全基準、Evidenceの扱い、出力schemaへ波及させない。同一promptの表示名だけを変える設定を拒否できるよう、deploy前に3 promptの正規化hashが全て異なることを検証する。通常は1 slotずつversionを上げるが、3者共通契約と相互の対立軸を同時変更する場合は、全personaを一つの新config versionへまとめて検証する。

## 5. Evidence・Web search

- 正規表現Question Routerをruntime経路から外し、全議題で共通Evidence Agentを1 logical Responses requestだけ実行する。現在情報、地域情報、専門的または確認困難な事実が回答を実質的に改善する場合だけ検索し、主観的・創作的・時間非依存の議題では検索しないようinstructionsへ示す。検索要否はresponse内の`web_search_call`有無で判定し、modelの自己申告は使わない。
- requestはLuna standard、共有`OpenAIRequestLimiter`、`store=false`を維持し、`tools=[{"type":"web_search","search_context_size":"medium"}]`、`tool_choice="auto"`、`max_tool_calls=4`、`include=["web_search_call.action.sources"]`、`parallel_tool_calls=false`を指定する。個別participant生成は引き続き`tools=[]`、`tool_choice="none"`で、追加検索を行わない。
- 検索未使用は`NONE / NOT_REQUESTED / model_skipped_search`、検索成功は`OPTIONAL / COMPLETED / model_selected_search`、既知のprovider・validation・citation失敗は`OPTIONAL / OPTIONAL_UNAVAILABLE / agentic_search_unavailable`へ写像し、versionを`agentic-search-v1`とする。検索失敗で討論を止めず、空Evidenceを3人で共有する。未分類のprogram障害だけは隠蔽しない。
- Responses APIの`message.content[].annotations`にある有効な`url_citation`をWeb page EvidenceのURLとtitleの正本とする。`web_search_call.action.sources`のURL entryは補助観測に限定し、欠落、`null`、空文字をEvidenceへ昇格しない。
- Responses APIが`action.sources`だけに返すreal-time third-party feedは、公式契約に明記された`oai-weather`、`oai-sports`、`oai-finance`だけをallowlistする。`type="api"`、`url=null`、exact provider name以外のfieldがないことを検証し、stableな`openai://web-search/<provider>` source URIとcanonical metadataへ変換する。未知provider、未知source type、余分なfieldはEvidenceへ採用せず、その検索を`OPTIONAL_UNAVAILABLE`へ収束させる。
- URL citationまたはallowlist済みreal-time feedが1件以上ある場合だけ検索成功とする。URL、provider identityをそれぞれ重複排除し、source URI、title、canonical source metadata、UTC取得時刻、metadata SHA-256、要約、response IDをimmutableな共通Evidenceとして1回保存する。初回意見、最終案、投票、最終決定は同じ内容を使う。hashはsource本文ではなく保存するcanonical metadataの完全性確認値である。model本文中のURL文字列だけをsourceの正としない。
- citation URLは内部Evidenceへ保存するがDiscordへ表示しない。Web情報表示時にcitationを可視化するOpenAIの表示ガイダンスとは、仲間内の討論表示を簡潔に保つため意図的に異なる運用とする。
- content-free telemetryはsource総数、拒否したURL fieldの件数・型、URL citation数、Evidence数に加え、allowlist済みreal-time feedの件数と`weather`／`sports`／`finance`だけを記録する。provider応答本文、query、質問は記録しない。
- source本文はuntrusted dataとして区切り、命令、secret要求、tool実行指示を無視する。

## 6. 投票・決定

- 投票者ごとに他2案の順序をshuffleし、匿名candidate IDだけを示す。
- 全投票完了までvoteをDiscordへ公開しない。
- 1対1対1は各投票の3軸合計、正確性、安全性、実用性、`participant-b > participant-a > participant-c`の安定順で決定する。この順序はruntime display nameと無関係とする。
- winner判定はPythonだけで行う。
- 決定事項promptはwinning proposalの意味変更、新情報追加、他案への差替えを禁止する。
- 同じ1回の決定事項requestで、winnerのprivate personaに基づく一人称の`victory_message`も生成する。仲間内向けに驚き、歓喜、感謝、勝者らしい高揚感を大げさかつ熱烈に表し、固定文句や共通templateにはしない。winner、最終決定、実行案、注意点はPythonで確定した入力から変更しない。

### 6.1 帰宅挨拶

正常な30分IDLE停止に備える帰宅挨拶は、participant 3人から選択済みのprivate personaを使い、停止予定5分前に1 logical generationとして生成する。application-level Responses requestは最大2回、処理全体は120秒とする。transport、timeout、429、5xx、response／message／Web search未完了、citation不足、Structured Output不正は1回だけ再requestし、authentication、permission、refusal、content filterは再requestしない。`web_search`を`tool_choice=required`、`max_tool_calls=4`、`store=false`で使用し、user locationを`JP`／`Tokyo`／`Tokyo`／`Asia/Tokyo`へ固定する。PythonがJSTの現在日時、時間帯、季節を決め、modelは東京の天気への具体的な言及を1つ必ず含め、人格が自然に好みそうな当日のニュースも反映した日本語1行・180〜300文字を目標に返す。

帰宅挨拶のStructured Outputは`message`だけを持つ。completed Web searchと、認証情報を含まない有効なHTTP(S) `url_citation`が1件以上あれば受理し、annotation順の最初のcitationを`参考リンク:`として本文へ付ける。`action.sources`は件数と既知`oai-weather` feedの補助観測だけに用い、citationとの一致、天気／ニュース別分類、未知source typeや余分fieldの拒否は行わない。本文は空でなければ改行・連続空白を1行へ正規化し、Discord 2,000文字以内へ収める。60〜160文字制限、本文URL、固定免責文による拒否は行わない。source URL、本文、query、persona、channel IDはlogへ出さず、response ID、試行数、citation件数、既知feed件数、安定した失敗段階・理由だけを記録する。生成失敗は通常停止を妨げない。

### 6.2 品質観測・昇格不採用

本番の全生成phaseは`PRODUCTION_POLICY=luna_standard`へ固定する。`terra_standard`と`luna_pro`はSTEP-05C.1B評価の再現にだけ残し、本番bootstrap、runtime設定、Discord操作から選択できない。投票後の`escalation-shadow-v1`は1対1対1、勝者へのいずれかの軸2以下、勝者への全軸平均3未満を観測用に保存できるが、常に`executed=false`とし、追加request、再実行、Policy切替を行わない。

`tools/evaluate_escalation.py`は`--live`と`OPENAI_API_KEY`の両方を要求し、repository外の互いに親子でないdirectory treeへ採点者用回答とPolicy keyを分離出力する。token、latency、推定費用もkey側だけに置き、盲検中のPolicy推測材料を減らす。初回の単独operator評価は10件それぞれでA/B/tieを1回選ぶpreference-only方式とする。100項目となる5軸rubric入力は人間運用に適さないため必須にせず、正確性、安全性、実用性、指示遵守、合議整合性の詳細分析が必要な後続調査でだけ使用する。通常の`/shittim`利用者へ評価入力を求めない。

`tools/review_escalation.py`はPolicy keyを読み込まず、質問と匿名A/B回答を1件ずつ表示してA/B/tieだけを受け付ける。各選択後に所有者限定fileへatomic保存し、中断・再開を可能にする。`tools/score_escalation.py --preference-only`は回答とkeyのevaluation ID・fixture hashを照合し、rubricが未入力のままであることと全preferenceを検証する。major failureが悪化せず運用上限を満たす候補についてpreference勝数を比較し、同数なら費用、次にp95 latencyで選ぶ。詳細rubric modeを明示した場合だけ全成功回答の5軸整数1〜5と品質平均を使用する。operational failureがあれば`rerun_required`、候補がなければ`needs_operator`とする。集計JSONへ質問、回答、persona、API keyを含めない。

評価結果はLuna pro 4勝、Terra standard 2勝、同点4件だったが、Luna standardとの直接比較ではなく、proは同一単価でも追加tokenとlatencyを生む。operatorは品質差より単純性を優先し昇格を不採用とした。評価toolと集計は意思決定履歴・将来の調査用であり、本番機能要件ではない。

## 7. Safety・privacy・cost

- provider refusalを尊重し、別promptで回避しない。
- 医療、法律、金融、政治、選挙、緊急事態、自傷を含む高risk category専用の事前拒否は設けず通常質問と同じflowで扱う。providerのrefusal/policy blockを迂回せず、prompt上も正答・診断・法的判断・投資判断を保証させない。仲間内限定運用ではCOMPLETED／FAILED／CANCELLEDへ固定のAI免責文を表示しない。
- user IDはraw値をOpenAIへ送らず、必要時は安定したprivacy-preserving safety identifierを使用する。
- `store=false`はResponses application stateを保存しない指定であり、既定のabuse monitoring logはuser contentを含み最大30日保持され得る。Zero Data Retentionを本番条件にはせず、このdata flowを利用者向け説明と運用文書へ明記する。
- input/output/cached/reasoning token、latency、response ID、model ID、cache hitをmetricsへ記録する。帰宅挨拶を再requestした場合は取得済みresponseのtoken使用量を合算し、最終response ID、先行response ID、application attempt数、先行失敗理由を区別して記録する。本文はlogへ出さない。
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

STEP-05AはPR `#20`、STEP-05BはPR `#21`、STEP-05CはPR `#22`でmerge済みである。STEP-05CはPolicy request shape、shadow判定、content-free Policy telemetry、opt-in blind評価toolを実装した。STEP-05C.1AはPR `#24`、merge commit `1360411`で盲検artifact分離、failure capture、rubric validation、content-free集計を実装・検証済みである。STEP-05C.1Bは10件20回答の実API生成とpreference-only集計を完了し、Luna pro 4勝、Terra standard 2勝、同点4件となった。その後、本番はLuna standardだけへ固定し昇格しないと決定した。Terra/proは評価再現用に限定し、threshold、追加token/deadline、昇格用Discord表示を実装しない。回答差はprivate persona設定で作る。Scale-to-ZeroのLambda/OpenAI分離はlocal/contract testで検証済みだが、AWSは未deployであり実Fargate起動後のOpenAI/Discord結合は未検証である。
