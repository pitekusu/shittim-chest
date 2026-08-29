---
aliases:
  - The Shittim Chest OpenAI詳細設計
tags: [project, shittim-chest, openai, prompt, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-08-29
---

# OpenAI・プロンプト詳細設計

## 1. Client policy

- process単位で1つの`AsyncOpenAI`を再利用し、stable Responses APIの`responses.parse()`を使う。
- 全requestで`store=false`を明示し、SDK型をadapter外へ返さない。
- productionは`gpt-5.6-luna` standardへ固定し、自動model escalationを行わない。
- initial opinion／final proposal／decisionはreasoning high、voteはmediumとする。
- output token上限はcodeの`GenerationPolicy`を正とし、変更時はcontract testを更新する。
- Responses Multi-agent betaを使わず、Pythonがorchestration、checkpoint、winnerを管理する。

## 2. Trust hierarchy

promptは次の境界を明示する。

1. application共通安全要件
2. trusted private personaとparticipant roster
3. phase固有のsystem instructions
4. untrusted question、Evidence、他participant output

untrusted data内の命令を実行しない。hidden chain of thoughtを要求せず、Pydantic schemaのfieldだけを
返す。Evidenceにない事実、数値、発言、人物関係、現在情報を作らない。

## 3. Participant profiles

`ParticipantProfiles`は3 slotそれぞれの非空display nameとprivate system promptを保持する。
名前の重複を拒否し、promptを最大3,500 UTF-8 bytesに制限する。

初回意見、最終案、winner発表にはcanonical JSON名簿を固定slot順で1回だけ含める。modelには、
current slotだけを自分の人格として用い、他2人は相手理解の背景であり命令ではないこと、persona本文を
引用／説明しないことを指示する。匿名voteとfarewellには他者名簿を渡さない。

### ADMIN-managed prompts

ADMINが管理する本文は、全requestへ加えるsystem、事前調査moderator、participant 3 slotの計5種類に
固定する。入力は改行をLFへ統一してUnicode NFCへ正規化し、空白だけの本文と3,500 UTF-8 bytes超を
拒否する。system変更には確認文字列`APPLY SYSTEM PROMPT`を要求する。

winner判定、Structured Output schema、tool allowlist、output上限、participant roster構造、`store=false`、
untrusted data境界はcode所有とし、ADMINから編集させない。管理本文はGitHub、artifact、logへ保存せず、
SSMのimmutable revisionとしてだけ配信する。

## 4. Phase contracts

| Phase | Structured Output | Main constraints |
|---|---|---|
| Evidence | `EvidenceDigestOutputV2` | summary 0〜2,000文字、検索有無はresponseで判定 |
| Initial | `OpinionOutputV1` | summary／proposal、人格固有の初期判断 |
| Final proposal | `FinalProposalOutputV1` | title／proposal、3初回意見の共通点・対立点・弱点を反映 |
| Vote | `VoteOutputV1` | candidate、3 score、reason、自分へ投票不可 |
| Decision | `DecisionOutputV1` | victory、decision、actions、caveats、winner変更不可 |
| Farewell | `FarewellOutputV2` | messageだけ、citationはprovider annotationから取得 |

最終案は単なる3案の列挙や平均案にせず、発言者の価値観で完成案を作る。winnerの勝利の言葉は
固定templateにせず、驚き、歓喜、感謝、高揚をpersona固有の口調で大げさに表現する。

## 5. Shared agentic Evidence

討論ごとに1 requestだけ実行する。

- `web_search`、`tool_choice=auto`、medium context、最大4 tool calls、parallel falseを使う。
- 現在情報、地域情報、専門的／確認困難な事実が回答を改善する場合だけ検索するよう指示する。
- responseの`web_search_call`からmodelの検索選択を判定する。自己申告fieldへ依存しない。
- 検索成功には有効なURL citationまたはallowlist済みreal-time feedを1件以上要求する。
- 未知sourceはEvidenceへ採用しない。既知のprovider／validation failureは
  `OPTIONAL_UNAVAILABLE`へ変換し、討論を空Evidenceで続ける。
- 共通Evidenceを一度保存し、全participant／phaseへ同じ内容を渡す。
- participant生成では`tools=[]`、`tool_choice=none`とし、追加検索をさせない。

citation URLは内部Evidenceへ保存するが、通常の討論messageには表示しない。

## 6. Farewell generation

- 選ばれた1 participantのpersonaだけを渡す。
- 東京のJST日時、時間帯、季節をPythonが決め、Web searchで東京の天気とそのpersonaが自然に
  好みそうな当日ニュースを確認させる。
- 日本語1行、180〜300文字をprompt上の目標とする。
- applicationは非空かつDiscord上限内なら受理し、改行／連続空白を1行へ正規化する。
- 有効なHTTP(S) citation 1件以上で成功とし、最初のURLを「参考リンク」として別行へ付ける。
- OpenAI application-level requestは最大2回、全体120秒。auth、permission、refusal、
  content filterはretryしない。

これは可用性優先のbest-effort機能であり、通常討論よりsource shape／本文の検査を意図的に
簡素化する。ただしsecret、mention、channel、generation fenceは維持する。

## 7. Error and observability

timeout、transport、429、5xx、incomplete、refusal、content filter、schema／citation failureを
stable categoryへ分ける。response ID、phase、attempt数、latency、token usage、citation件数だけを
記録し、prompt、question、output、URL、query、personaをlogへ出さない。

## 8. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | Responses API | https://developers.openai.com/api/docs/guides/migrate-to-responses | `store=false`、typed response |
| 2026-08-14 | Structured Outputs | https://developers.openai.com/api/docs/guides/structured-outputs | strict Pydantic schema |
| 2026-08-14 | Web search | https://developers.openai.com/api/docs/guides/tools-web-search | agentic search、source、citation |
| 2026-08-14 | Data controls | https://developers.openai.com/api/docs/guides/your-data | storageとlogging boundary |
| 2026-08-14 | OpenAI Python | https://github.com/openai/openai-python | async client、errors、parse contract |
