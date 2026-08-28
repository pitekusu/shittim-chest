---
aliases:
  - The Shittim Chest GitHub詳細設計
tags: [project, shittim-chest, github, ci-cd, detailed-design]
status: production-1.0
created: 2026-07-16
updated: 2026-08-28
---

# GitHub・CI-CD詳細設計

## 1. Repository policy

- public repositoryのdefault branchは`main`。
- source／IaC／tool／sampleはMIT、`docs/`と`AGENTS.md`はreserved documentとする。
- production secret、private Discord identifier、personaをrepository、artifact名、workflow summaryへ
  含めない。
- normal PR、required checks、CodeQL、squash mergeを用い、mainへ直接pushしない。
- Actionsとthird-party toolはversion／digest／full commit SHAでpinする。

## 2. Continuous Integration

`ci.yml`はPR、main push、manual dispatchで次の8 jobを実行する。

| Job | Scope |
|---|---|
| quality | lock、format、Ruff、ty、import contract、tool policy |
| tests | full pytest、DynamoDB Local |
| security | secret scan、dependency audit、source SBOM |
| package | deterministic wheel／sdist |
| cdk | TypeScript、Vitest、cdk-nag、synth、npm audit |
| container-arm64 | production build、policy、SBOM、config digest |
| grype | raw scan、VEX、fixable／residual risk gate |
| docs-public-safety | mirror、links、public surface、license boundary |

CodeQL default setupはPython、JavaScript／TypeScript、Actionsを解析する。branch rulesetのcheck名を
workflow名と一致させ、rerunでflaky failureを隠さない。

## 3. Container evidence

- canonical ARM64 path contextとDocker exporterでproduction imageを固定SHAからbuildする。
- production targetのconfig digest、SBOM、VEX、Grype、risk resultを同一SHA／runに紐づくimage／scan
  artifact間で照合する。artifactごとの保持fieldはworkflow schemaを正とする。
- PR required gateは静的policy baselineや別runとのconfig digest完全一致を要求しない。
- risk acceptanceが必要なfindingだけ、production image kind、承認済みbuild contextごとの
  実測config digest、finding key、期限へ束縛する。同kindの独立したCI／Release buildは
  複数digestを一acceptanceに有限数登録できるが、未登録digestは拒否する。
- `fault-test` imageはproductionのbaselineやrisk acceptance対象外とする。
- manifest digestからconfig digestを推測せず、別exporterや過去runの値を流用しない。

## 4. Production Release

manual workflowは`main`の固定SHAとRuntimeConfig versionを入力にする。

```text
plan
→ image build／push／sign／attestation
→ Lambda bundle／immutable Change Set
→ release evidence
→ production Environment approval
→ deploy
→ structural smoke
→ cleanup／lock release
```

- plan前にrepository identity、OIDC claims、named main checks、CodeQL、private handle metadata、stack、
  active Change Set、deployment lock、runtime activityを確認する。
- Release内でproduction imageを再buildし、各runのconfig digest、fixable High／Critical、
  VEX後residual、production targetのrisk acceptanceを検証する。
- push前に既存referrer一覧を保存し、差分から今回追加したprovenance、SBOM、vulnerability
  attestationを各1件特定する。過去referrerを削除しない。
- Environment承認後にdeployment lockを原子的に取得し、attested Change Setだけを固定順で実行する。
- structural smokeはstack state、ECS desired／running、task image、Image Admission Lambdaを
  検証する。Discord／OpenAIのlive討論はRelease後の別受入である。
- cleanup成功で元のdeploy failureを成功扱いにしない。未実行Change Setを削除し、lockを解放する。

ReleaseIdentity自体の更新、failed runのrerun、manual CloudFormation deployは通常Releaseへ暗黙に
含めない。

## 5. Scheduled workflows

- Infrastructure Drift: 毎週火曜、5 stackをread-only検査し、単一Issueを更新する。
- Dependency Graph: 毎週火曜、managed dependency inventoryを確認する。
- Tool Versions: 毎週水曜、pinの更新候補を検出する。更新は別の通常PRで行う。
- Discord Security Digest: 毎日、Security情報の補助digestを送る。
- repository／workflow notifications: GitHub eventをDiscord Forumへ補助通知する。

scheduleのexact時刻、対象workflow、権限は`.github/workflows/`を正とする。

## 6. Artifact and secret safety

- checkoutは`persist-credentials=false`、workflow permissionはjob単位の最小権限とする。
- untrusted PR codeへAWS credential、production Environment secret、DHI credentialを渡さない。
- artifactはcontent-free名を使い、retentionを設定し、download後にdigest／schema／SHAを再検証する。
- GitHub summaryへattestation URLを出す場合はpublic repository ownerを意図せずmaskしない。

## 7. 公式資料確認記録

| 確認日 | 対象 | 公式資料 | 設計への反映 |
|---|---|---|---|
| 2026-08-14 | GitHub Actions security | https://docs.github.com/en/actions/reference/security/secure-use | pin、permission、untrusted PR境界 |
| 2026-08-14 | GitHub Environments | https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments | production approval boundary |
| 2026-08-14 | GitHub OIDC for AWS | https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws | short-lived role identity |
| 2026-08-14 | Artifact attestations | https://docs.github.com/en/actions/concepts/security/artifact-attestations | provenance／SBOM evidence |
| 2026-08-14 | CodeQL | https://docs.github.com/en/code-security/concepts/code-scanning/codeql-code-scanning | 3-language analysis |
