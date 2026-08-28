import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";

const RECORD_ID = "r".repeat(43);
const DARK_THEME_SNAPSHOT_MAX_DIFF_RATIO = 0.0001;
const AUTHENTICATED_ROUTE_CHUNK_NAMES = [
  "RecordsHome",
  "RecordDetail",
  "RankingsPage",
  "AdminPage",
] as const;

function observeAssetRequests(page: Page): Set<string> {
  const requestedAssets = new Set<string>();
  page.on("request", (request) => {
    if (request.resourceType() !== "script" && request.resourceType() !== "stylesheet") return;
    requestedAssets.add(new URL(request.url()).pathname);
  });
  return requestedAssets;
}

function matchingChunkAssets(requestedAssets: ReadonlySet<string>, chunkName: string): string[] {
  const chunkPrefix = `/assets/${chunkName}-`;
  return [...requestedAssets].filter(
    (assetPath) =>
      assetPath.startsWith(chunkPrefix) &&
      (assetPath.endsWith(".js") || assetPath.endsWith(".css")),
  );
}

const placeholder = (displayName: string, fallbackVariant: string) => ({
  kind: "placeholder",
  url: null,
  alt: `${displayName}のアバター`,
  fallbackVariant,
});

const participants = [
  ["participant-a", "アロナ", "cyan"],
  ["participant-b", "プラナ", "pink"],
  ["participant-c", "安倍晋三AI", "lavender"],
].map(([slot, displayName, fallbackVariant]) => ({
  slot,
  displayName,
  avatar: placeholder(displayName, fallbackVariant),
}));

const detail = {
  schemaVersion: 1,
  recordId: RECORD_ID,
  completedAt: "2026-08-15T06:00:00Z",
  question: "休日に家で過ごすなら、映画を見るかゲームをするか。それぞれの価値観から話し合う",
  requester: { displayName: "パワー系ウナギ", avatar: placeholder("依頼者", "cyan") },
  participants,
  initialOpinions: participants.map(({ slot }, index) => ({
    participant: slot,
    summary: ["物語へ集中する", "一緒に遊べる", "翌日の疲労を抑える"][index],
    proposal: [
      "映画を一本じっくり観て、物語の余韻まで落ち着いて楽しみます。",
      "協力ゲームを二時間ほど遊び、全員で同じ達成感を共有します。",
      "短編映画を選び、翌日の予定に疲れを残さない範囲で楽しみます。",
    ][index],
  })),
  finalProposals: participants.map(({ slot }, index) => ({
    participant: slot,
    title: ["映画で整える夜", "ゲームで盛り上がる夜", "余白を残す映画案"][index],
    proposal: [
      "映画を観てから印象に残った場面を振り返り、感想をゆっくり話します。",
      "協力ゲームを二時間遊び、途中で休憩を入れながら全員で最後まで進めます。",
      "短編映画を一本選び、見終わった後に余裕を持って早めに休みます。",
    ][index],
  })),
  votes: [
    { voter: "participant-a", candidate: "participant-b", reason: "皆で参加できるため" },
    { voter: "participant-b", candidate: "participant-a", reason: "落ち着いて楽しめるため" },
    { voter: "participant-c", candidate: "participant-a", reason: "明日の負担が少ないため" },
  ],
  result: {
    winner: "participant-a",
    voteCounts: [
      { participant: "participant-a", count: 2 },
      { participant: "participant-b", count: 1 },
      { participant: "participant-c", count: 0 },
    ],
    tieBreakApplied: false,
  },
  finalDecision: {
    winner: "participant-a",
    victoryMessage: "みんなの一票が本当にうれしいです！",
    decision:
      "今夜は二時間以内の映画を一本観て、見終わった後に印象に残った場面や感想をゆっくり話し合います。",
    actions: ["好みの飲み物を用意する", "全員が楽しめる二時間以内の映画を選ぶ"],
    caveats: ["翌日の予定に疲れを残さない時間までに終える"],
  },
};

const rankings = {
  schemaVersion: 1,
  wins: [
    { rank: 1, displayName: "アロナ", avatar: participants[0]!.avatar, count: 20 },
    { rank: 2, displayName: "プラナ", avatar: participants[1]!.avatar, count: 18 },
    { rank: 3, displayName: "安倍晋三AI", avatar: participants[2]!.avatar, count: 16 },
  ],
  requests: [
    {
      rank: 1,
      displayName: "パワー系ウナギ",
      avatar: placeholder("パワー系ウナギ", "cyan"),
      count: 12,
    },
    {
      rank: 1,
      displayName: "吹雪型JC",
      avatar: placeholder("吹雪型JC", "pink"),
      count: 12,
    },
    { rank: 3, displayName: "先生", avatar: placeholder("先生", "lavender"), count: 8 },
  ],
  generatedAt: "2026-08-22T00:00:00Z",
};

const costs = {
  schemaVersion: 1,
  period: "week",
  timeZone: "Asia/Tokyo",
  startDate: "2026-08-17",
  endDate: "2026-08-23",
  currency: "JPY",
  total: "123.456789",
  breakdown: {
    fargate: "10.000000",
    lambda: "2.000000",
    openai: "100.000000",
    otherAws: "11.456789",
  },
  conversion: {
    source: "frankfurter-v2",
    method: "daily-reference-rate",
    baseCurrency: "USD",
    updatedAt: "2026-08-23T12:17:00+09:00",
  },
  updatedAt: "2026-08-23T12:17:00+09:00",
  status: "partial",
};

const adminFunctionKeys = [
  "discord_ingress",
  "discord_status",
  "image_admission",
  "runtime_reconciler",
  "records_projector",
  "records_backfill",
  "records_auth",
  "records_read",
  "records_ranking",
  "records_cost",
  "records_admin_status",
] as const;

const adminStatus = {
  schemaVersion: 1,
  generatedAt: "2026-08-27T01:20:00Z",
  expiresAt: "2026-08-27T01:21:00Z",
  stale: false,
  overall: { state: "warning", criticalAlarms: 0, warningAlarms: 1, partial: false },
  sections: [
    {
      service: "ecs",
      state: "healthy",
      summary: "IDLE",
      metrics: [
        { name: "running_count", value: 0 },
        { name: "desired_count", value: 0 },
        { name: "pending_count", value: 0 },
        { name: "deployment_count", value: 1 },
        { name: "service_status", value: "ACTIVE" },
        { name: "scheduling_strategy", value: "REPLICA" },
        { name: "launch_mode", value: "FARGATE" },
        { name: "platform_version", value: "1.4.0" },
        { name: "task_definition_revision", value: 42 },
        { name: "rollout_state", value: "COMPLETED" },
        { name: "failed_task_count", value: 0 },
        { name: "deployment_updated_at", value: "2026-08-27T00:58:00Z" },
        { name: "deployment_controller", value: "ECS" },
        { name: "minimum_healthy_percent", value: 0 },
        { name: "maximum_percent", value: 100 },
        { name: "circuit_breaker_enabled", value: true },
        { name: "circuit_breaker_rollback", value: true },
        { name: "execute_command_enabled", value: false },
        { name: "active_debates", value: 0 },
        { name: "outbox_pending", value: 0 },
        { name: "runtime_prompt_revision", value: null },
        { name: "heartbeat_age_seconds", value: null },
      ],
    },
    {
      service: "ecr",
      state: "healthy",
      summary: "承認済みイメージとrepository保護を確認しました。",
      metrics: [
        { name: "tag_mutability", value: "IMMUTABLE" },
        { name: "encryption_type", value: "AES256" },
        { name: "repository_created_at", value: "2026-07-10T03:00:00Z" },
        { name: "scan_on_push", value: false },
        { name: "repository_image_count", value: 6 },
        { name: "repository_tagged_image_count", value: 2 },
        { name: "repository_untagged_image_count", value: 4 },
        { name: "repository_total_size_bytes", value: 152043520 },
        { name: "repository_latest_pushed_at", value: "2026-08-24T14:16:36Z" },
        { name: "normal_image_present", value: true },
        { name: "normal_pushed_at", value: "2026-08-24T14:16:20Z" },
        { name: "normal_last_pulled_at", value: "2026-08-26T07:20:00Z" },
        { name: "normal_size_bytes", value: 66305166 },
        { name: "normal_tag_count", value: 1 },
        { name: "normal_media_type", value: "OCI_IMAGE" },
      ],
    },
    {
      service: "inspector",
      state: "warning",
      summary: "検出結果とECR scan coverageを確認しました。",
      metrics: [
        { name: "active_critical", value: 0 },
        { name: "active_high", value: 0 },
        { name: "active_medium", value: 12 },
        { name: "active_low", value: 0 },
        { name: "active_untriaged", value: 6 },
        { name: "coverage_active", value: 4 },
        { name: "last_scanned_at", value: "2026-08-27T00:50:00Z" },
      ],
    },
    {
      service: "s3",
      state: "healthy",
      summary: "Bucket保護設定を確認しました。",
      metrics: ["web", "media", "release"].flatMap((key) => [
        { name: `${key}_versioning`, value: "Enabled" },
        { name: `${key}_encrypted`, value: true },
        { name: `${key}_public_access_blocked`, value: true },
      ]),
    },
    {
      service: "dynamodb",
      state: "healthy",
      summary: "Table状態と保護設定を確認しました。",
      metrics: [
        ...["debate", "archive", "statistics", "session"].flatMap((key, index) => [
          { name: `${key}_status`, value: "ACTIVE" },
          { name: `${key}_pitr`, value: "ENABLED" },
          { name: `${key}_deletion_protection`, value: true },
          { name: `${key}_ttl`, value: key === "session" ? "ENABLED" : "DISABLED" },
          { name: `${key}_item_count`, value: [2231, 684, 12, 7][index] },
          { name: `${key}_read_throttles`, value: 0 },
          { name: `${key}_write_throttles`, value: 0 },
        ]),
        { name: "debate_stream_enabled", value: true },
        { name: "debate_stream_view_type", value: "NEW_IMAGE" },
      ],
    },
    {
      service: "lambda",
      state: "healthy",
      summary: "Lambda状態と直近1時間の指標を確認しました。",
      metrics: adminFunctionKeys.flatMap((key, index) => [
        { name: `${key}_state`, value: "Active" },
        { name: `${key}_update`, value: "Successful" },
        { name: `${key}_hour_invocations`, value: index % 3 },
        { name: `${key}_hour_errors`, value: 0 },
        { name: `${key}_hour_throttles`, value: 0 },
        { name: `${key}_hour_duration`, value: index % 3 === 0 ? "42.180" : null },
      ]),
    },
    {
      service: "cloudfront",
      state: "healthy",
      summary: "Distributionと証明書を確認しました。",
      metrics: [
        { name: "enabled", value: true },
        { name: "deployment_status", value: "Deployed" },
        { name: "tls_policy", value: "TLSv1.3_2025" },
        { name: "certificate_key_algorithm", value: "EC-prime256v1" },
        { name: "certificate_expires_at", value: "2027-03-07T23:59:59Z" },
        { name: "hour_requests", value: 28 },
        { name: "hour_4xx_rate", value: "0.000" },
        { name: "hour_5xx_rate", value: "0.000" },
      ],
    },
    {
      service: "sqs",
      state: "healthy",
      summary: "DLQは空で保護設定も正常です。",
      metrics: [
        { name: "visible_messages", value: 0 },
        { name: "inflight_messages", value: 0 },
        { name: "delayed_messages", value: 0 },
        { name: "oldest_message_age_seconds", value: "0.000" },
        { name: "encrypted", value: true },
        { name: "retention_seconds", value: 1_209_600 },
      ],
    },
    {
      service: "apigateway",
      state: "healthy",
      summary: "HTTP APIと直近1時間の応答を確認しました。",
      metrics: ["discord", "records"].flatMap((key, index) => [
        { name: `${key}_protocol`, value: "HTTP" },
        { name: `${key}_auto_deploy`, value: true },
        { name: `${key}_hour_requests`, value: 12 + index * 8 },
        { name: `${key}_hour_4xx`, value: 0 },
        { name: `${key}_hour_5xx`, value: 0 },
        { name: `${key}_hour_latency`, value: index === 0 ? "44.500" : "86.125" },
        {
          name: `${key}_hour_integration_latency`,
          value: index === 0 ? "31.250" : "62.500",
        },
      ]),
    },
    {
      service: "eventbridge",
      state: "healthy",
      summary: "定期実行とイベント配信を確認しました。",
      metrics: [
        { name: "runtime_state", value: "ENABLED" },
        { name: "runtime_expression", value: "rate(1 minute)" },
        { name: "runtime_retry_attempts", value: 2 },
        ...["ranking", "aws_fx", "openai", "abnormal_stop"].flatMap((key, index) => [
          { name: `${key}_state`, value: "ENABLED" },
          {
            name: `${key}_expression`,
            value: index === 3 ? "event pattern" : "cron(17 3 * * ? *)",
          },
          { name: `${key}_day_invocations`, value: [96, 1, 24, 0][index] },
          { name: `${key}_day_failures`, value: 0 },
        ]),
      ],
    },
    {
      service: "cloudformation",
      state: "healthy",
      summary: "8 Stackの状態と最後のdrift結果を確認しました。",
      metrics: [
        "stateful",
        "release_identity",
        "runtime",
        "operations",
        "cost_governance",
        "records_stateful",
        "records_application",
        "records_edge",
      ].flatMap((key) => [
        { name: `${key}_status`, value: "UPDATE_COMPLETE" },
        { name: `${key}_drift`, value: "IN_SYNC" },
        { name: `${key}_termination_protection`, value: true },
        { name: `${key}_updated_at`, value: "2026-08-27T00:45:00Z" },
      ]),
    },
    {
      service: "sns",
      state: "healthy",
      summary: "運用通知の購読と直近24時間の配信を確認しました。",
      metrics: [
        { name: "confirmed_subscriptions", value: 1 },
        { name: "pending_subscriptions", value: 0 },
        { name: "day_delivered", value: 2 },
        { name: "day_failed", value: 0 },
      ],
    },
    {
      service: "ssm",
      state: "healthy",
      summary: "必要な設定metadataを確認しました。",
      metrics: [
        ...[
          ["discord", 5],
          ["runtime", 6],
          ["records", 6],
          ["cost", 2],
        ].flatMap(([key, count]) => [
          { name: `${key}_ready`, value: count },
          { name: `${key}_required`, value: count },
        ]),
        { name: "runtime_prompt_pointer_present", value: false },
        { name: "latest_modified_at", value: "2026-08-26T21:30:00Z" },
      ],
    },
    {
      service: "cost_governance",
      state: "healthy",
      summary: "予算使用率とCost Anomaly通知を確認しました。",
      metrics: [
        { name: "project_actual_percent", value: "24.5" },
        { name: "project_forecast_percent", value: "31.2" },
        { name: "project_health", value: "HEALTHY" },
        { name: "account_actual_percent", value: "19.8" },
        { name: "account_forecast_percent", value: "27.4" },
        { name: "account_health", value: "HEALTHY" },
        { name: "anomaly_subscription", value: true },
        { name: "anomaly_frequency", value: "DAILY" },
        { name: "anomaly_subscribers", value: 1 },
        { name: "anomaly_confirmed_subscribers", value: 1 },
      ],
    },
    {
      service: "signer",
      state: "healthy",
      summary: "コンテナ署名profileを確認しました。",
      metrics: [
        { name: "status", value: "Active" },
        { name: "platform", value: "Notation-OCI-SHA384-ECDSA" },
        { name: "validity_value", value: 12 },
        { name: "validity_unit", value: "MONTHS" },
      ],
    },
    {
      service: "external",
      state: "healthy",
      summary: "OpenAIとFrankfurterを含む集計鮮度を確認しました。",
      metrics: ["aws", "openai", "frankfurter"].flatMap((key, index) => [
        { name: `${key}_initial_complete`, value: true },
        { name: `${key}_fresh`, value: true },
        { name: `${key}_last_success_at`, value: `2026-08-27T0${index}:37:00Z` },
        { name: `${key}_last_failure_at`, value: null },
        { name: `${key}_failure_code`, value: null },
      ]),
    },
  ],
};

const PRODUCTION_CSP = [
  "default-src 'self'",
  "base-uri 'self'",
  "connect-src 'self'",
  "font-src 'self'",
  "img-src 'self' data:",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "script-src 'self'",
  "style-src 'self'",
].join("; ");

async function mockAuthenticatedApi(
  page: Page,
  recordDetail = detail,
  recordDelayMs = 0,
  isAdmin = false,
): Promise<void> {
  let authenticated = true;
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: authenticated
        ? {
            schemaVersion: 1,
            authenticated: true,
            isAdmin,
            user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
            csrfToken: "csrf-token",
          }
        : {
            schemaVersion: 1,
            authenticated: false,
            isAdmin: false,
            user: null,
            csrfToken: null,
          },
    }),
  );
  await page.route("**/api/v1/logout", (route) => {
    authenticated = false;
    return route.fulfill({ status: 204 });
  });
  await page.route("**/api/v1/records?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        items: [
          {
            schemaVersion: 1,
            recordId: recordDetail.recordId,
            completedAt: recordDetail.completedAt,
            questionPreview: recordDetail.question,
            requester: recordDetail.requester,
            participants: recordDetail.participants,
            result: recordDetail.result,
          },
        ],
        nextCursor: null,
      },
    }),
  );
  await page.route(`**/api/v1/records/${RECORD_ID}`, async (route) => {
    if (recordDelayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, recordDelayMs));
    }
    return route.fulfill({ json: recordDetail });
  });
  await page.route("**/api/v1/insights/rankings", (route) => route.fulfill({ json: rankings }));
  await page.route("**/api/v1/insights/costs?*", (route) => {
    const period = new URL(route.request().url()).searchParams.get("period") ?? "week";
    return route.fulfill({ json: { ...costs, period } });
  });
  await page.route("**/api/v1/admin/status*", (route) => route.fulfill({ json: adminStatus }));
}

test("authenticated member can browse the completed archive", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/");

  const recordsHeading = page.getByRole("heading", { name: "議論の記録" });
  await expect(recordsHeading).toBeVisible();
  expect(
    await recordsHeading.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    ),
  ).toBeLessThanOrEqual(45);
  const logoff = page.getByRole("button", { name: "LOGOFF" });
  await expect(logoff).toHaveCSS("font-family", /Delogy/);
  await expect(logoff).toHaveCSS("border-top-style", "solid");
  await expect(logoff).toHaveAttribute("lang", "en");
  const logoffBox = await logoff.boundingBox();
  expect(logoffBox).not.toBeNull();
  expect(logoffBox!.height).toBeGreaterThanOrEqual(44);
  const card = page.getByRole("article");
  await expect(card).toContainText(detail.question);
  const cardDecoration = card.locator("[data-card-decoration]");
  await expect(cardDecoration).toBeVisible();
  await expect(cardDecoration).toHaveAttribute("aria-hidden", "true");
  expect(
    await cardDecoration.evaluate((element) => {
      const cardBounds = element.parentElement!.getBoundingClientRect();
      const decorationBounds = element.getBoundingClientRect();
      return {
        clippedByCard:
          decorationBounds.right >= cardBounds.right &&
          decorationBounds.bottom >= cardBounds.bottom,
        pointerEvents: getComputedStyle(element).pointerEvents,
      };
    }),
  ).toEqual({ clippedByCard: true, pointerEvents: "none" });
  await expect(card.getByText("2026年8月15日 15:00")).toHaveAttribute(
    "datetime",
    detail.completedAt,
  );
  await expect(page.getByRole("button", { name: "依頼者" })).toContainText("すべて");
  await page.getByRole("button", { name: "依頼者" }).click();
  const requesterOption = page.getByRole("option", { name: "パワー系ウナギ" });
  await expect(requesterOption.locator("[aria-hidden=true]").first()).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await requesterOption.click();
  await expect(card).toContainText(detail.question);
  await page.getByRole("button", { name: "勝者" }).click();
  await expect(
    page.getByRole("option", { name: "アロナ" }).locator("[aria-hidden=true]").first(),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.locator("body").click({ position: { x: 4, y: 4 } });
  const newestSort = page.getByRole("radio", { name: "新しい順" });
  const oldestSort = page.getByRole("radio", { name: "古い順" });
  const sortSegment = page.locator("[data-sort]");
  await expect(newestSort).toBeChecked();
  await expect(oldestSort).not.toBeChecked();
  const sortBoxBefore = await sortSegment.boundingBox();
  const transformBefore = await sortSegment.evaluate(
    (element) => getComputedStyle(element, "::before").transform,
  );
  expect(
    await sortSegment.evaluate((element) =>
      getComputedStyle(element, "::before")
        .transitionDuration.split(",")
        .map((value) => value.trim()),
    ),
  ).toContain("0.2s");
  await sortSegment.getByText("OLD", { exact: true }).click();
  await expect(sortSegment).toHaveAttribute("data-sort", "oldest");
  await page.waitForTimeout(240);
  expect(
    await sortSegment.evaluate((element) => getComputedStyle(element, "::before").transform),
  ).not.toBe(transformBefore);
  expect(await sortSegment.boundingBox()).toEqual(sortBoxBefore);
  await oldestSort.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(newestSort).toBeChecked();
  await expect(sortSegment).toHaveAttribute("data-sort", "newest");
  await expect(page.getByLabel("開始日")).toHaveCount(0);
  await expect(page.getByLabel("終了日")).toHaveCount(0);
  const typographySupport = await page.evaluate(() => ({
    autoPhrase: CSS.supports("word-break", "auto-phrase"),
    autospace: CSS.supports("text-autospace", "normal"),
    language: document.documentElement.lang,
  }));
  expect(typographySupport).toEqual({ autoPhrase: true, autospace: true, language: "ja" });
  const questionTypography = await card.getByRole("heading").evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      lineBreak: style.getPropertyValue("line-break"),
      overflowWrap: style.overflowWrap,
      textAutospace: style.getPropertyValue("text-autospace"),
      wordBreak: style.wordBreak,
    };
  });
  expect(questionTypography).toEqual({
    lineBreak: "strict",
    overflowWrap: "anywhere",
    textAutospace: "normal",
    wordBreak: "auto-phrase",
  });
  await expect(page).toHaveScreenshot("records-home.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });
  const cardLink = page.getByRole("link", {
    name: `「${detail.question}」の記録を読む`,
  });
  await expect(cardLink).toContainText("記録を読む");
  const cardBox = await card.boundingBox();
  expect(cardBox).not.toBeNull();
  await card.click({ position: { x: cardBox!.width - 12, y: cardBox!.height - 12 } });
  const questionHeading = page.getByRole("heading", { name: detail.question });
  const opinionsHeading = page.getByRole("heading", { name: "3人の意見" });
  await expect(questionHeading).toBeVisible();
  await expect(opinionsHeading).toBeVisible();
  expect(await questionHeading.evaluate((element) => getComputedStyle(element).fontSize)).toBe(
    await opinionsHeading.evaluate((element) => getComputedStyle(element).fontSize),
  );
  await expect(page.getByRole("heading", { name: "アロナ → プラナ" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "最終決定" })).toBeVisible();
  await expect(page.getByText(detail.finalDecision.victoryMessage)).toBeVisible();
  await expect(page.getByText("2026年8月15日 15:00")).toHaveAttribute(
    "datetime",
    detail.completedAt,
  );
  await expect(page.getByText(/所要時間|Evidence|外部根拠/)).toHaveCount(0);
  await expect(page.getByText(detail.finalDecision.decision)).toHaveCSS("text-wrap", "pretty");

  await expect(page).toHaveScreenshot("records-detail.png", {
    animations: "disabled",
    fullPage: true,
    // Self-hosted font rasterization differs by a few edge pixels between the
    // pinned local and GitHub-hosted Chromium environments.
    maxDiffPixels: 30,
  });

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("record card opens from its native keyboard link", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/");

  const cardLink = page.getByRole("link", {
    name: `「${detail.question}」の記録を読む`,
  });
  await cardLink.focus();
  await expect(cardLink).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(`/records/${RECORD_ID}`);
  await expect(page.getByRole("heading", { name: detail.question })).toBeVisible();
});

test("final proposals align when initial opinions have different lengths", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const unevenDetail = {
    ...detail,
    initialOpinions: detail.initialOpinions.map((opinion, index) =>
      index === 0
        ? {
            ...opinion,
            proposal: `${opinion.proposal}\n${"文章量が異なっても、最終案の開始位置は三人で揃います。".repeat(8)}`,
          }
        : opinion,
    ),
  };
  await mockAuthenticatedApi(page, unevenDetail);
  await page.goto(`/records/${RECORD_ID}`);
  await expect(page.getByRole("heading", { name: detail.question })).toBeVisible();

  const finalProposalTops = await page
    .getByRole("heading", { name: "最終案" })
    .evaluateAll((headings) => headings.map((heading) => heading.getBoundingClientRect().top));

  expect(finalProposalTops).toHaveLength(3);
  expect(Math.max(...finalProposalTops) - Math.min(...finalProposalTops)).toBeLessThanOrEqual(1);
});

test("record identities produce varied and stable card ornaments", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.setViewportSize({ width: 1680, height: 950 });
  await page.addInitScript(() => localStorage.setItem("shittim-records-theme-v1", "dark"));
  const records = [0, 1, 3, 4, 5, 6].map((identity, index) => {
    const winnerIndex = index % participants.length;
    return {
      schemaVersion: 1,
      recordId: createHash("sha256").update(`visual-card-${identity}`).digest("base64url"),
      completedAt: `2026-08-${String(22 - index).padStart(2, "0")}T06:00:00Z`,
      questionPreview: [
        "明日の放課後に何をして過ごす？",
        "三人で選ぶなら、どんな映画がいい？",
        "夏の夜に似合う飲み物を決めよう",
        "休日に読む一冊を選ぶなら？",
        "小さな旅へ持っていくものは何？",
        "今夜の献立を楽しく決めて",
      ][index],
      requester: detail.requester,
      participants,
      result: {
        winner: participants[winnerIndex]!.slot,
        voteCounts: participants.map(({ slot }, participantIndex) => ({
          participant: slot,
          count:
            participantIndex === winnerIndex
              ? 2
              : participantIndex === (winnerIndex + 1) % 3
                ? 1
                : 0,
        })),
        tieBreakApplied: false,
      },
    };
  });
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: true,
        isAdmin: false,
        user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
        csrfToken: "csrf-token",
      },
    }),
  );
  await page.route("**/api/v1/records?*", (route) =>
    route.fulfill({ json: { schemaVersion: 1, items: records, nextCursor: null } }),
  );

  await page.goto("/");
  const decorations = page.locator("[data-card-decoration]");
  await expect(decorations).toHaveCount(records.length);
  const beforeReload = await decorations.evaluateAll((elements) =>
    elements.map((element) => ({
      accent: element.getAttribute("data-card-decoration-accent"),
      frame: element.getAttribute("data-card-decoration-frame"),
      mirrored: element.getAttribute("data-card-decoration-mirrored"),
      variant: element.getAttribute("data-card-decoration"),
    })),
  );
  expect(new Set(beforeReload.map(({ variant }) => variant)).size).toBe(records.length);
  expect(new Set(beforeReload.map(({ frame }) => frame)).size).toBeGreaterThanOrEqual(3);

  await page.reload();
  await expect(decorations).toHaveCount(records.length);
  expect(
    await decorations.evaluateAll((elements) =>
      elements.map((element) => ({
        accent: element.getAttribute("data-card-decoration-accent"),
        frame: element.getAttribute("data-card-decoration-frame"),
        mirrored: element.getAttribute("data-card-decoration-mirrored"),
        variant: element.getAttribute("data-card-decoration"),
      })),
    ),
  ).toEqual(beforeReload);
  await expect(page).toHaveScreenshot("records-card-decorations-dark.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });
});

test("branded route motion stays short and coordinates all internal routes", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await mockAuthenticatedApi(page, detail, 1_200);
  await page.goto("/");

  const initialScene = page.locator('[data-route-scene="/"]');
  await expect(initialScene).toHaveAttribute("data-route-motion", "idle");

  const insightsMotionTiming = page.waitForFunction(
    (selector) => {
      const scene = document.querySelector<HTMLElement>(selector);
      if (scene?.getAttribute("data-route-motion") !== "active") return null;
      const panel = scene.querySelector<HTMLElement>("section[aria-labelledby]");
      if (!panel) return null;
      const parseSeconds = (value: string) => Number.parseFloat(value) || 0;
      const sceneStyle = getComputedStyle(scene);
      const panelStyle = getComputedStyle(panel);
      return {
        panelTotal:
          parseSeconds(panelStyle.animationDuration) + parseSeconds(panelStyle.animationDelay),
        sceneDuration: parseSeconds(sceneStyle.animationDuration),
      };
    },
    '[data-route-scene="/insights"]',
    { polling: "raf", timeout: 3_000 },
  );
  await page.getByRole("link", { name: /^いろいろ/ }).click();
  const timing = await (await insightsMotionTiming).jsonValue();
  expect(timing).not.toBeNull();
  if (!timing) throw new Error("insights route motion did not become active");
  const insightsScene = page.locator('[data-route-scene="/insights"]');
  const insightsHeading = page.getByRole("heading", { name: "いろいろな記録" });
  const wins = page.getByRole("region", { name: "勝利回数ランキング" });
  await expect(insightsScene.locator("[data-route-brand]")).toHaveCount(0);
  await expect(insightsHeading).toBeFocused();
  await expect(wins).toBeVisible();

  expect(timing.sceneDuration).toBeLessThanOrEqual(0.42);
  expect(timing.panelTotal).toBeLessThanOrEqual(0.42);

  const panelLayoutBefore = await wins.evaluate((element) => ({
    height: (element as HTMLElement).offsetHeight,
    left: (element as HTMLElement).offsetLeft,
    top: (element as HTMLElement).offsetTop,
    width: (element as HTMLElement).offsetWidth,
  }));
  await page.waitForTimeout(430);
  await expect(insightsScene).toHaveAttribute("data-route-motion", "settled");
  expect(
    await wins.evaluate((element) => ({
      height: (element as HTMLElement).offsetHeight,
      left: (element as HTMLElement).offsetLeft,
      top: (element as HTMLElement).offsetTop,
      width: (element as HTMLElement).offsetWidth,
    })),
  ).toEqual(panelLayoutBefore);

  await page.goBack();
  const archiveHeading = page.getByRole("heading", { name: "議論の記録" });
  await expect(page.locator('[data-route-stage][data-route-kind="archive"]')).toBeVisible();
  await expect(archiveHeading).toBeFocused();

  await page.getByRole("link", { name: `「${detail.question}」の記録を読む` }).click();
  const detailScene = page.locator(
    '[data-route-stage][data-route-kind="detail"] [data-route-scene]',
  );
  await expect(detailScene).toHaveAttribute("data-route-motion", "waiting");
  const detailMotionBecameActive = detailScene.evaluate(
    (scene) =>
      new Promise<boolean>((resolve) => {
        const observe = () => {
          if (scene.getAttribute("data-route-motion") !== "active") return false;
          observer.disconnect();
          resolve(true);
          return true;
        };
        const observer = new MutationObserver(observe);
        if (observe()) return;
        observer.observe(scene, {
          attributeFilter: ["data-route-motion"],
          attributes: true,
        });
        window.setTimeout(() => {
          observer.disconnect();
          resolve(false);
        }, 3_000);
      }),
  );
  await page.waitForTimeout(430);
  await expect(page.getByText("議論の記録を開いています。")).toBeVisible();
  await expect(detailScene).toHaveAttribute("data-route-motion", "waiting");
  const detailHeading = page.getByRole("heading", { name: detail.question });
  await expect(page.locator('[data-route-stage][data-route-kind="detail"]')).toBeVisible();
  await expect(detailHeading).toBeFocused();
  expect(await detailMotionBecameActive).toBe(true);
  await expect(detailScene).toHaveAttribute("data-route-motion", "settled");

  await page.getByRole("link", { name: "← 記録一覧へ" }).click();
  await expect(archiveHeading).toBeVisible();
  await page.getByRole("link", { name: "いろいろな記録" }).click();
  await expect(page).toHaveURL("/insights");
  await expect(page.locator('[data-route-scene="/insights"]')).toHaveCount(1);
});

test("archive controls remain usable across responsive breakpoints", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/");

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 1024, height: 768 },
    { width: 768, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    const sortBox = await page.locator("[data-sort]").boundingBox();
    const sortLegendBox = await page.getByText("並び順", { exact: true }).boundingBox();
    const cardBox = await page
      .getByRole("link", { name: `「${detail.question}」の記録を読む` })
      .boundingBox();

    expect(sortBox).not.toBeNull();
    expect(sortLegendBox).not.toBeNull();
    expect(sortBox!.height).toBeGreaterThanOrEqual(46);
    expect(sortLegendBox!.y + sortLegendBox!.height + 4).toBeLessThanOrEqual(sortBox!.y);
    expect(sortBox!.x + sortBox!.width).toBeLessThanOrEqual(viewport.width);
    expect(cardBox).not.toBeNull();
    expect(cardBox!.x + cardBox!.width).toBeLessThanOrEqual(viewport.width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
  }
});

test("dark theme covers login, archive, detail, and rankings", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      },
    }),
  );
  await page.goto("/login");

  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute("content", "#071724");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("records-dark-login.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: DARK_THEME_SNAPSHOT_MAX_DIFF_RATIO,
  });

  await page.unroute("**/api/v1/session?*");
  await mockAuthenticatedApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "議論の記録" })).toBeVisible();
  await expect(page.getByRole("switch", { name: "ダークモード" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("records-dark-home.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: DARK_THEME_SNAPSHOT_MAX_DIFF_RATIO,
  });

  await page.getByRole("link", { name: `「${detail.question}」の記録を読む` }).click();
  await expect(page.getByRole("heading", { name: detail.question })).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("records-dark-detail.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: DARK_THEME_SNAPSHOT_MAX_DIFF_RATIO,
  });

  await page.goto("/insights");
  await expect(page.getByRole("heading", { name: "いろいろな記録" })).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("records-dark-insights.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: DARK_THEME_SNAPSHOT_MAX_DIFF_RATIO,
  });
});

test("manual theme survives reload and logoff while the mobile switch stays usable", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await mockAuthenticatedApi(page);
  await page.goto("/");

  const desktopSwitch = page.getByRole("switch", { name: "ダークモード" });
  await expect(desktopSwitch).toHaveAttribute("aria-checked", "true");
  expect(
    await page.locator("[data-sort]").evaluate((element) =>
      getComputedStyle(element, "::before")
        .transitionDuration.split(",")
        .every((value) => Number.parseFloat(value) <= 0.01),
    ),
  ).toBe(true);
  await desktopSwitch.focus();
  await page.keyboard.press("Space");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("shittim-records-theme-v1"))).toBe("light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await desktopSwitch.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.setViewportSize({ width: 390, height: 844 });
  const mobileNavigation = page.getByRole("navigation", { name: "モバイルナビゲーション" });
  const mobileSwitch = mobileNavigation.getByRole("switch", { name: "ダークモード" });
  await expect(mobileSwitch).toBeVisible();
  expect((await mobileSwitch.boundingBox())?.height).toBeGreaterThanOrEqual(48);
  await expect(mobileSwitch).toHaveAttribute("aria-checked", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await expect(page).toHaveScreenshot("records-dark-mobile-390.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: DARK_THEME_SNAPSHOT_MAX_DIFF_RATIO,
  });

  await page.setViewportSize({ width: 320, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  await expect(mobileSwitch).toBeVisible();
  await mobileNavigation.getByRole("button", { name: "LOGOFF" }).click();
  await expect(page.getByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("logoff shows the goodbye transition before returning to login", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "LOGOFF" }).first().click();

  const transition = page.getByLabel("ログオフしました");
  await expect(transition).toBeVisible();
  await expect(transition.getByText("GOODBYE, SENSEI.")).toHaveCSS("font-family", /Delogy/);
  await expect(transition).toHaveCSS("animation-duration", "2s");
  await expect(page.getByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible({
    timeout: 3_000,
  });
  await expect(transition).toHaveCount(0);
});

test("authenticated member can review responsive rankings", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/insights");

  const sidebarProductName = page.locator('[aria-label="The Shittim Chest Archive"]');
  await expect(sidebarProductName.locator(":scope > span")).toHaveText([
    "THE SHITTIM",
    "CHEST ARCHIVE",
  ]);
  await expect(sidebarProductName).toHaveCSS("font-family", /Delogy/);
  await expect(sidebarProductName).toHaveAttribute("lang", "en");
  const sidebarType = await sidebarProductName.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      fontSize: Number.parseFloat(style.fontSize),
      lineHeight: Number.parseFloat(style.lineHeight),
    };
  });
  expect(sidebarType.lineHeight / sidebarType.fontSize).toBeGreaterThanOrEqual(1.4);
  expect(
    await sidebarProductName.evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
  await expect(sidebarProductName.locator("..").locator('[aria-hidden="true"]').first()).toHaveCSS(
    "width",
    "44px",
  );
  const insightsHeading = page.getByRole("heading", { name: "いろいろな記録" });
  await expect(insightsHeading).toBeVisible();
  expect(
    await insightsHeading.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    ),
  ).toBeLessThanOrEqual(45);
  const wins = page.getByRole("region", { name: "勝利回数ランキング" });
  const requests = page.getByRole("region", { name: "依頼回数ランキング" });
  await expect(wins.getByText("VICTORIES", { exact: true })).toBeVisible();
  await expect(requests.getByText("REQUESTS", { exact: true })).toBeVisible();
  for (const [panel, label] of [
    [wins, "VICTORIES"],
    [requests, "REQUESTS"],
  ] as const) {
    const emblemBox = await panel.locator("header [aria-hidden='true']").first().boundingBox();
    const headingBox = await panel.getByText(label, { exact: true }).locator("..").boundingBox();
    expect(emblemBox).not.toBeNull();
    expect(headingBox).not.toBeNull();
    expect(
      Math.abs(emblemBox!.y + emblemBox!.height / 2 - (headingBox!.y + headingBox!.height / 2)),
    ).toBeLessThanOrEqual(2);
  }
  await expect(wins.getByRole("listitem")).toHaveCount(3);
  await expect(wins.getByRole("meter")).toHaveCount(3);
  await expect(
    wins.getByRole("meter", { name: "アロナ: 20回（最多20回との比較）" }),
  ).toHaveAttribute("value", "20");
  await expect(wins.getByRole("status", { name: "勝利回数ランキングの合計" })).toContainText(
    "54回",
  );
  await expect(requests.getByText("1位", { exact: true })).toHaveCount(2);
  await expect(
    requests.getByRole("status", { name: "依頼回数ランキングの上位合計" }),
  ).toContainText("32回");
  await expect(requests.getByRole("meter")).toHaveCount(3);
  await expect(page.getByText("2026年8月22日 09:00")).toBeVisible();
  const costDashboard = page.getByRole("region", { name: "概算費用" });
  await expect(costDashboard).toContainText("¥123.456789");
  await expect(costDashboard).toContainText("Fargate");
  await expect(costDashboard).toContainText("Lambda");
  await expect(costDashboard).toContainText("OpenAI");
  await expect(costDashboard).toContainText("その他AWS");
  await expect(costDashboard).toContainText("一部集計中");
  await expect(costDashboard).toContainText("Route 53は含みません");
  const podium = wins.locator('[data-podium-layout="ranked"]');
  const podiumBox = await podium.boundingBox();
  const winsBox = await wins.boundingBox();
  expect(podiumBox).not.toBeNull();
  expect(winsBox).not.toBeNull();
  expect(podiumBox!.x).toBeGreaterThanOrEqual(winsBox!.x);
  expect(podiumBox!.x + podiumBox!.width).toBeLessThanOrEqual(winsBox!.x + winsBox!.width);
  await expect(podium).toHaveCSS("overflow", "clip");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("records-insights.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });
});

test("English login product name keeps the approved two-line break at narrow widths", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      },
    }),
  );
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 808, height: 730 },
  ]) {
    await page.setViewportSize(viewport);
    const heading = page.getByRole("heading", { name: "The Shittim Chest Archive" });
    await expect(heading).toBeVisible();
    const lines = await heading.locator("span").evaluateAll((elements) =>
      elements.map((element) => {
        const bounds = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          clientWidth: element.clientWidth,
          display: style.display,
          scrollWidth: element.scrollWidth,
          top: bounds.top,
          whiteSpace: style.whiteSpace,
        };
      }),
    );
    expect(lines).toHaveLength(2);
    expect(lines[0]?.display).toBe("block");
    expect(lines[0]?.whiteSpace).toBe("nowrap");
    expect(lines[1]!.top).toBeGreaterThan(lines[0]!.top);
    expect(
      lines.every((line) => line.scrollWidth <= line.clientWidth),
      JSON.stringify({ viewport, lines }),
    ).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
    await expect(heading).toHaveCSS("text-align", "center");
    const headingType = await heading.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        fontSize: Number.parseFloat(style.fontSize),
        lineHeight: Number.parseFloat(style.lineHeight),
      };
    });
    expect(headingType.lineHeight / headingType.fontSize).toBeGreaterThanOrEqual(1.2);
    const authenticate = page.getByRole("link", { name: "AUTHENTICATE" });
    const authenticateType = await authenticate.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        fontSize: Number.parseFloat(style.fontSize),
        paddingInline: Number.parseFloat(style.paddingInlineStart),
      };
    });
    expect(authenticateType.fontSize).toBeLessThanOrEqual(10.6);
    expect(authenticateType.paddingInline).toBeGreaterThanOrEqual(16);
    await expect(page).toHaveScreenshot(`display-font-login-${viewport.width}.png`, {
      animations: "disabled",
      fullPage: true,
      maxDiffPixels: 20,
    });
  }
});

test("loads the next archive page automatically near the end of the loaded cards", async ({
  page,
}) => {
  const firstPage = Array.from({ length: 12 }, (_, index) => ({
    schemaVersion: 1,
    recordId: String.fromCharCode(65 + index).repeat(43),
    completedAt: detail.completedAt,
    questionPreview: `読み込み済みの議論 ${index + 1}`,
    requester: detail.requester,
    participants: detail.participants,
    result: detail.result,
  }));
  const nextRecord = {
    ...firstPage[0]!,
    recordId: "Z".repeat(43),
    questionPreview: "自動で追加された議論",
  };
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: true,
        isAdmin: false,
        user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
        csrfToken: "csrf-token",
      },
    }),
  );
  await page.route("**/api/v1/records?*", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    return route.fulfill({
      json: {
        schemaVersion: 1,
        items: cursor === "next-page" ? [nextRecord] : firstPage,
        nextCursor: cursor === "next-page" ? null : "next-page",
      },
    });
  });

  await page.goto("/");
  await expect(page.getByText("読み込み済みの議論 12")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));

  const appendedCard = page.getByRole("link", {
    name: "「自動で追加された議論」の記録を読む",
  });
  await expect(appendedCard).toBeVisible();
  await expect(appendedCard).toHaveCSS("animation-duration", "0.18s");
  await expect(appendedCard).toHaveCSS("animation-delay", "0s");

  const search = page.getByRole("searchbox", { name: "フリーワード検索" });
  await search.fill("読み込み済みの議論 1");
  await expect(appendedCard).toHaveCount(0);
  await search.clear();
  await expect(appendedCard).toBeVisible();
  await expect(appendedCard).toHaveCSS("animation-name", "none");
  await expect(page.getByRole("button", { name: "さらに読み込む" })).toHaveCount(0);
});

test("record detail stays inside the mobile viewport with long Japanese content", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  const longText =
    "最初に必要な道具と時間を整理してから小さく試し、途中で休憩を入れながら、参加する全員が無理なく楽しめる進め方を選びます。最後に感想を共有して次回の工夫へつなげます。";
  const longDetail = {
    ...detail,
    question:
      "新しい趣味を始めるなら、庭で植物を育てるか室内で工作を楽しむか、それぞれの価値観から話し合って決める",
    initialOpinions: detail.initialOpinions.map((opinion) => ({
      ...opinion,
      summary: "準備を整えてから全員で無理なく楽しめる方法を選ぶ",
      proposal: longText,
    })),
    finalProposals: detail.finalProposals.map((proposal) => ({
      ...proposal,
      title: "小さく試して長く続ける共同趣味の計画",
      proposal: longText,
    })),
  };
  await mockAuthenticatedApi(page, longDetail);
  await page.goto(`/records/${RECORD_ID}`);
  await expect(page.getByRole("heading", { name: longDetail.question })).toBeVisible();

  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});

test("vote graph keeps distinct routes, interactions, and responsive layouts", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockAuthenticatedApi(page);
  await page.goto(`/records/${RECORD_ID}`);
  await page.evaluate(() => document.fonts.ready);

  const graph = page.getByTestId("vote-graph");
  await graph.scrollIntoViewIfNeeded();
  await expect(graph).toHaveAttribute("data-layout", "wide");
  await expect(graph).toHaveAttribute("data-revealed", "true");
  await expect(graph.getByLabel("アロナがプラナに投票")).toBeVisible();
  await expect(graph.getByLabel("プラナがアロナに投票")).toBeVisible();
  await expect(graph.getByLabel("安倍晋三AIがアロナに投票")).toBeVisible();

  const beforeAnimation = await graph.boundingBox();
  await page.waitForTimeout(1_500);
  const afterAnimation = await graph.boundingBox();
  expect(beforeAnimation).not.toBeNull();
  expect(afterAnimation).not.toBeNull();
  expect(Math.abs(afterAnimation!.width - beforeAnimation!.width)).toBeLessThan(0.5);
  expect(Math.abs(afterAnimation!.height - beforeAnimation!.height)).toBeLessThan(0.5);
  await expect(graph.locator('[data-part="line"]').first()).toHaveCSS("stroke-dasharray", "none");

  await expect(graph).toHaveScreenshot("vote-graph-desktop.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });

  const aronaNode = graph.getByRole("button", { name: "アロナに関係する投票を強調" });
  await aronaNode.hover();
  await expect(graph.locator('[data-vote-key="participant-a-participant-b"]')).toHaveAttribute(
    "data-relation",
    "outgoing",
  );
  await expect(graph).toHaveScreenshot("vote-graph-desktop-hover.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });

  await page.setViewportSize({ width: 1024, height: 900 });
  await expect(graph).toHaveAttribute("data-layout", "wide");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1024);

  await page.setViewportSize({ width: 768, height: 900 });
  await expect(graph).toHaveAttribute("data-layout", "compact");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(graph).toHaveAttribute("data-layout", "compact");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await expect(graph).toHaveScreenshot("vote-graph-mobile-390.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.reload();
  await graph.scrollIntoViewIfNeeded();
  await expect(graph.locator('[data-part="line"]').first()).toHaveCSS("animation-name", "none");
  await expect(graph.locator('[data-part="flow"]').first()).toHaveCSS("display", "none");
  await expect(graph.locator('[data-part="arrow"]').first()).toHaveCSS("opacity", "1");
  await expect(graph).toHaveScreenshot("vote-graph-reduced-motion.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });
});

test("touch activation keeps a vote selection until the same target is tapped again", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  await mockAuthenticatedApi(page);
  await page.goto(`/records/${RECORD_ID}`);

  const graph = page.getByTestId("vote-graph");
  await graph.scrollIntoViewIfNeeded();
  const node = graph.getByRole("button", { name: "アロナに関係する投票を強調" });
  const route = graph.getByLabel("アロナがプラナに投票");

  await node.dispatchEvent("touchstart");
  await node.dispatchEvent("touchmove");
  await node.dispatchEvent("touchend");
  await expect(node).toHaveAttribute("aria-pressed", "false");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "default");

  await node.tap();
  await expect(node).toHaveAttribute("aria-pressed", "true");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "outgoing");

  await node.tap();
  await expect(node).toHaveAttribute("aria-pressed", "false");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "default");

  await route.tap();
  await expect(route).toHaveAttribute("aria-pressed", "true");
  await expect(graph.getByRole("tooltip")).toHaveText("アロナがプラナに投票");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "active");

  await route.tap();
  await expect(route).toHaveAttribute("aria-pressed", "false");
  await expect(graph.getByRole("tooltip")).toHaveCount(0);
  await expect(route.locator("..")).toHaveAttribute("data-relation", "default");
});

test("reduced motion skips the long login and logoff transitions", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockAuthenticatedApi(page);
  await page.addInitScript(() =>
    sessionStorage.setItem("shittim-records-login-transition", "pending"),
  );
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "議論の記録" })).toBeVisible({
    timeout: 1_000,
  });
  await page.getByRole("link", { name: /^いろいろ/ }).click();
  const reducedScene = page.locator('[data-route-scene="/insights"]');
  await expect(reducedScene).toHaveCSS("animation-name", "none");
  await expect(reducedScene.locator("[data-route-brand]")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "勝利回数ランキング" })).toHaveCSS(
    "animation-name",
    "none",
  );
  await page.getByRole("button", { name: "LOGOFF" }).first().click();
  await expect(page.getByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible({
    timeout: 1_000,
  });
});

test("English display copy uses Delogy while Japanese copy keeps LINE Seed JP", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      },
    }),
  );
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);

  const loginHeading = page.getByRole("heading", { name: "The Shittim Chest Archive" });
  await expect(loginHeading).toHaveCSS("font-family", /Delogy/);
  await expect(loginHeading).toHaveAttribute("lang", "en");
  await expect(page.getByText("シッテムの箱 議事録閲覧システム")).toHaveCSS(
    "font-family",
    /LINE Seed JP/,
  );
  const authenticate = page.getByRole("link", { name: "AUTHENTICATE" });
  await expect(authenticate).toHaveCSS("font-family", /Delogy/);
  await expect(authenticate).toHaveAttribute("lang", "en");
  expect(await page.evaluate(() => document.fonts.check('16px "Delogy"'))).toBe(true);
  await expect(page).toHaveScreenshot("display-font-login.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });

  await page.unroute("**/api/v1/session?*");
  await mockAuthenticatedApi(page);
  await page.addInitScript(() =>
    sessionStorage.setItem("shittim-records-login-transition", "pending"),
  );
  await page.goto("/");
  const welcome = page.getByText("WELCOME, SENSEI.");
  await expect(welcome).toBeVisible();
  await expect(welcome).toHaveCSS("font-family", /Delogy/);
  await page.addStyleTag({
    content: "*, *::before, *::after { animation: none !important; transition: none !important; }",
  });
  await expect(page).toHaveScreenshot("display-font-transition.png", {
    animations: "allow",
    fullPage: true,
    maxDiffPixels: 20,
  });
});

test("publishes complete Open Graph metadata and a 1200 by 630 preview image", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      },
    }),
  );
  await page.goto("/");

  const propertyContent = (property: string) =>
    page.locator(`meta[property="${property}"]`).getAttribute("content");
  const namedContent = (name: string) =>
    page.locator(`meta[name="${name}"]`).getAttribute("content");
  const description =
    "シッテムの箱 議事録閲覧システム。吹雪型JCのつどいサーバの先生であることを認証して、議論記録を閲覧できます。";
  const imageUrl = await propertyContent("og:image");
  expect(imageUrl).toMatch(
    /^https:\/\/shittim\.pitekusu\.dev\/assets\/shittim-chest-archive-og-[a-f0-9]{12}\.png$/,
  );
  if (imageUrl === null) {
    throw new Error("og_image_metadata_missing");
  }

  await expect(page).toHaveTitle("THE SHITTIM CHEST ARCHIVE | シッテムの箱 議事録閲覧システム");
  expect(await namedContent("description")).toBe(description);
  expect(await page.locator('link[rel="canonical"]').getAttribute("href")).toBe(
    "https://shittim.pitekusu.dev/",
  );
  expect(await propertyContent("og:title")).toBe("THE SHITTIM CHEST ARCHIVE");
  expect(await propertyContent("og:description")).toBe(description);
  expect(await propertyContent("og:type")).toBe("website");
  expect(await propertyContent("og:url")).toBe("https://shittim.pitekusu.dev/");
  expect(await propertyContent("og:site_name")).toBe("シッテムの箱 議事録");
  expect(await propertyContent("og:locale")).toBe("ja_JP");
  expect(await propertyContent("og:image:secure_url")).toBe(imageUrl);
  expect(await propertyContent("og:image:type")).toBe("image/png");
  expect(await propertyContent("og:image:width")).toBe("1200");
  expect(await propertyContent("og:image:height")).toBe("630");
  expect(await propertyContent("og:image:alt")).toContain("THE SHITTIM CHEST ARCHIVE");
  expect(await propertyContent("og:image:alt")).toContain("アロナ・プラナ・安倍晋三AI");
  expect(await namedContent("twitter:card")).toBe("summary_large_image");
  expect(await namedContent("twitter:title")).toBe("THE SHITTIM CHEST ARCHIVE");
  expect(await namedContent("twitter:description")).toBe(description);
  expect(await namedContent("twitter:image")).toBe(imageUrl);
  expect(await namedContent("twitter:image:alt")).toContain("THE SHITTIM CHEST ARCHIVE");

  const imagePath = new URL(imageUrl).pathname;
  const imageResponse = await page.request.get(imagePath);
  expect(imageResponse.ok()).toBe(true);
  const imageBytes = await imageResponse.body();
  const contentHash = createHash("sha256").update(imageBytes).digest("hex").slice(0, 12);
  expect(imagePath).toBe(`/assets/shittim-chest-archive-og-${contentHash}.png`);

  const image = await page.evaluate(async (source) => {
    const element = new Image();
    element.src = source;
    await element.decode();
    return { height: element.naturalHeight, width: element.naturalWidth };
  }, imagePath);
  expect(image).toEqual({ height: 630, width: 1200 });
});

test("anonymous login page boots under the production CSP without dynamic evaluation", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/session") {
      await route.fulfill({
        json: {
          schemaVersion: 1,
          authenticated: false,
          isAdmin: false,
          user: null,
          csrfToken: null,
        },
      });
      return;
    }
    const response = await route.fetch();
    await route.fulfill({
      response,
      headers: { ...response.headers(), "content-security-policy": PRODUCTION_CSP },
    });
  });

  await page.goto("/");

  const loginButton = page.getByRole("link", { name: "AUTHENTICATE" });
  await expect(loginButton).toBeVisible();
  await loginButton.hover();
  await page.waitForTimeout(150);
  const hoverTransform = await loginButton.evaluate(
    (element) => getComputedStyle(element).transform,
  );
  await page.mouse.down();
  await expect
    .poll(() => loginButton.evaluate((element) => getComputedStyle(element).transform))
    .not.toBe(hoverTransform);
  await page.mouse.move(0, 0);
  await page.mouse.up();
  await page.evaluate(() => document.fonts.ready);
  expect(pageErrors).toEqual([]);
});

test("anonymous login does not request authenticated route assets", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const requestedAssets = observeAssetRequests(page);
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      },
    }),
  );

  await page.goto("/login");
  await expect(page.getByRole("link", { name: "AUTHENTICATE" })).toBeVisible();

  for (const chunkName of AUTHENTICATED_ROUTE_CHUNK_NAMES) {
    expect(matchingChunkAssets(requestedAssets, chunkName), chunkName).toEqual([]);
  }
});

test("management console remains reachable in the five-column 320px mobile navigation", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.setViewportSize({ width: 320, height: 720 });
  await mockAuthenticatedApi(page);

  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "ACCESS DENIED" })).toBeVisible();

  const mobileNavigation = page.getByRole("navigation", {
    name: "モバイルナビゲーション",
  });
  await expect(mobileNavigation).toBeVisible();
  await expect(mobileNavigation.locator(":scope > a, :scope > button")).toHaveCount(5);
  const adminLink = mobileNavigation.getByRole("link", { name: "管理コンソール" });
  await expect(adminLink).toBeVisible();
  await expect(adminLink).toHaveText("管理コンソール");
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("management console presents localized visual status", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.addInitScript(() => localStorage.setItem("shittim-records-theme-v1", "dark"));
  await mockAuthenticatedApi(page, detail, 0, true);

  await page.goto("/admin");

  const heading = page.getByRole("heading", { name: "管理コンソール" });
  await expect(heading).toBeVisible();
  expect(
    await heading.evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize)),
  ).toBeLessThanOrEqual(45);
  await expect(page.getByText("AWSの稼働状態を、安全な境界の内側で確認します。")).toHaveCount(0);
  await expect(page.getByText("アクセス", { exact: true })).toHaveCount(0);
  await expect(page.getByText("desired_count", { exact: true })).toHaveCount(0);
  await expect(page.getByText("タスク稼働", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "ECS構成とデプロイ状態" })).toBeVisible();
  const ecrImageTable = page.getByRole("region", { name: "承認済みECRイメージ" });
  await expect(ecrImageTable).toBeVisible();
  await expect(ecrImageTable.getByRole("rowheader", { name: "本番版" })).toBeVisible();
  await expect(ecrImageTable.getByRole("rowheader", { name: "緊急版" })).toHaveCount(0);
  expect(
    await ecrImageTable.evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
  await expect(page.getByRole("region", { name: "S3保護設定" })).toBeVisible();
  await expect(page.getByRole("region", { name: "DynamoDBテーブル状態" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Lambda関数状態" })).toBeVisible();
  await expect(page.getByRole("region", { name: "API Gateway状態" })).toBeVisible();
  await expect(page.getByRole("region", { name: "定期実行とイベント配信" })).toBeVisible();
  await expect(page.getByRole("region", { name: "CloudFormation Stack状態" })).toBeVisible();
  await expect(page.getByRole("region", { name: "予算状態" })).toBeVisible();
  await expect(page.getByRole("region", { name: "外部集計状態" })).toBeVisible();
  await expect(page.getByText("Discord連携", { exact: true })).toBeVisible();
  for (const value of ["1.4.0", "rev. 42", "145 MiB"]) {
    const fontFamily = await page
      .getByText(value, { exact: true })
      .evaluate((element) => getComputedStyle(element).fontFamily);
    expect(fontFamily).toContain("LINE Seed JP");
    expect(fontFamily).not.toContain("Delogy");
  }
  const s3NumericGlyph = page.getByRole("heading", { name: "S3" }).locator("span");
  await expect(s3NumericGlyph).toHaveText("3");
  expect(await s3NumericGlyph.evaluate((element) => getComputedStyle(element).fontFamily)).toMatch(
    /^Delogy/u,
  );
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("admin-console-dark.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });
});

test("management console contains wide status tables on mobile", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("shittim-records-theme-v1", "dark"));
  await mockAuthenticatedApi(page, detail, 0, true);

  await page.goto("/admin");

  await expect(page.getByRole("heading", { name: "管理コンソール" })).toBeVisible();
  await expect(page.getByRole("region", { name: "S3保護設定" })).toBeVisible();
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

for (const directRoute of [
  { path: "/", chunkName: "RecordsHome", heading: "議論の記録" },
  { path: `/records/${RECORD_ID}`, chunkName: "RecordDetail", heading: detail.question },
  { path: "/insights", chunkName: "RankingsPage", heading: "いろいろな記録" },
  { path: "/admin", chunkName: "AdminPage", heading: "ACCESS DENIED" },
] as const) {
  test(`direct ${directRoute.path} navigation loads its route chunk`, async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium");
    const requestedAssets = observeAssetRequests(page);
    await mockAuthenticatedApi(page);

    await page.goto(directRoute.path);
    await expect(page.getByRole("heading", { name: directRoute.heading })).toBeVisible();

    const requiredRouteAssets = matchingChunkAssets(requestedAssets, directRoute.chunkName);
    expect(
      requiredRouteAssets.filter((assetPath) => assetPath.endsWith(".js")),
      `${directRoute.chunkName} JavaScript`,
    ).toHaveLength(1);
    expect(
      requiredRouteAssets.filter((assetPath) => assetPath.endsWith(".css")),
      `${directRoute.chunkName} CSS`,
    ).toHaveLength(1);
    for (const unrelatedChunkName of AUTHENTICATED_ROUTE_CHUNK_NAMES.filter(
      (chunkName) => chunkName !== directRoute.chunkName,
    )) {
      expect(matchingChunkAssets(requestedAssets, unrelatedChunkName), unrelatedChunkName).toEqual(
        [],
      );
    }
    await expect(page.getByTestId("vote-graph")).toHaveCount(
      directRoute.chunkName === "RecordDetail" ? 1 : 0,
    );
  });
}
