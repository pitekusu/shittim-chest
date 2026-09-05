import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { beforeAll, describe, expect, test } from "vitest";

import { RecordsApplicationStack } from "../lib/records-application-stack";
import { RecordsStatefulStack } from "../lib/records-stateful-stack";

const recordsFunctionNames = [
  "projector",
  "backfill",
  "auth",
  "ranking",
  "cost",
  "inspector-translation",
  "read",
  "admin-config",
  "admin-status",
  "memorial-api",
  "memorial-worker",
].map((name) => `shittim-chest-production-records-${name}`);

type PolicyStatement = {
  readonly Action: string | string[];
  readonly Condition?: Record<string, unknown>;
  readonly Effect?: string;
  readonly Resource: unknown;
};

function actionsOf(statement: PolicyStatement): string[] {
  return Array.isArray(statement.Action) ? statement.Action : [statement.Action];
}

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly stack: RecordsApplicationStack;
  readonly template: Template;
} {
  const app = new App();
  const env = { account: "000000000000", region: "ap-northeast-1" };
  const stateful = new RecordsStatefulStack(app, "RecordsStateful", {
    env,
    stackName: "ShittimChest-Prod-RecordsStateful",
    terminationProtection: true,
  });
  const stack = new RecordsApplicationStack(app, "RecordsApplication", {
    env,
    stackName: "ShittimChest-Prod-RecordsApplication",
    terminationProtection: true,
  });
  for (const target of [stateful, stack]) {
    Tags.of(target).add("Project", "shittim-chest");
    Tags.of(target).add("Environment", "production");
    Tags.of(target).add("ManagedBy", "cdk");
  }
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { checks, stack, template: Template.fromStack(stack) };
}

describe("RecordsApplicationStack", () => {
  let fixture: ReturnType<typeof synthesize>;
  beforeAll(() => {
    fixture = synthesize();
  });

  test("synthesizes when the deployment account is unresolved", () => {
    const app = new App();
    const stack = new RecordsApplicationStack(app, "RecordsApplication", {
      env: { region: "ap-northeast-1" },
      stackName: "ShittimChest-Prod-RecordsApplication",
      terminationProtection: true,
    });
    const checks = new AwsSolutionsChecks(app, { verbose: true });
    Validations.of(app).addPlugins(checks);

    expect(() => app.synth()).not.toThrow();
    expect(checks.validateScope(stack).success).toBe(true);
  });

  test("creates eleven Python 3.14 ARM64 functions from one immutable S3 version", () => {
    const { stack, template } = fixture;

    expect(stack.terminationProtection).toBe(true);
    template.resourceCountIs("AWS::Lambda::Function", 11);
    for (const functionName of recordsFunctionNames) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Architectures: ["arm64"],
        Code: {
          S3Bucket: { Ref: "RecordsBundleBucketName" },
          S3Key: { Ref: "RecordsBundleObjectKey" },
          S3ObjectVersion: { Ref: "RecordsBundleObjectVersion" },
        },
        FunctionName: functionName,
        MemorySize: functionName.endsWith("memorial-worker") ? 1024 : 512,
        Runtime: "python3.14",
      });
    }
  });

  test("keeps fixed ADMIN inventory out of the Lambda environment", () => {
    const { template } = fixture;
    const adminStatusFunction = Object.values(
      template.findResources("AWS::Lambda::Function"),
    ).find(
      (resource) =>
        resource.Properties.FunctionName ===
        "shittim-chest-production-records-admin-status",
    );
    const variables = adminStatusFunction?.Properties.Environment?.Variables;

    expect(variables).toBeDefined();
    for (const name of [
      "ADMIN_STATUS_FUNCTIONS_JSON",
      "ADMIN_STACKS_JSON",
      "ADMIN_STATUS_PARAMETERS_JSON",
      "ADMIN_BUDGETS_JSON",
    ]) {
      expect(variables).not.toHaveProperty(name);
    }
    expect(Buffer.byteLength(JSON.stringify(variables), "utf8")).toBeLessThan(3000);
  });

  test("binds each function to one explicit 90-day log group", () => {
    const { template } = fixture;
    const logGroups = template.findResources("AWS::Logs::LogGroup");

    template.resourceCountIs("AWS::Logs::LogGroup", 12);
    for (const functionName of recordsFunctionNames) {
      const [logGroupLogicalId] = Object.entries(logGroups).find(
        ([, resource]) =>
          resource.Properties.LogGroupName === `/aws/lambda/${functionName}` &&
          resource.Properties.RetentionInDays === 90,
      ) ?? [undefined];
      const functionResource = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((resource) => resource.Properties.FunctionName === functionName);

      expect(logGroupLogicalId).toBeDefined();
      expect(functionResource?.Properties.LoggingConfig).toEqual({
        LogFormat: "JSON",
        LogGroup: { Ref: logGroupLogicalId },
      });
    }
    for (const resource of Object.values(logGroups)) {
      expect(resource.Properties.RetentionInDays).toBe(90);
    }
  });

  test("publishes five isolated aliases behind exactly twenty-one HTTP API routes", () => {
    const { template } = fixture;

    template.resourceCountIs("AWS::Lambda::Version", 5);
    template.resourceCountIs("AWS::Lambda::Alias", 5);
    template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
    template.resourceCountIs("AWS::ApiGatewayV2::Route", 21);
    template.resourceCountIs("AWS::ApiGatewayV2::Stage", 1);
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      AutoDeploy: true,
      DefaultRouteSettings: {
        ThrottlingBurstLimit: 20,
        ThrottlingRateLimit: 10,
      },
      StageName: "$default",
    });
    const stage = Object.values(
      template.findResources("AWS::ApiGatewayV2::Stage"),
    )[0];
    const accessLogDestination = JSON.stringify(
      stage?.Properties.AccessLogSettings.DestinationArn,
    );
    expect(accessLogDestination).toContain(":log-group:");
    expect(accessLogDestination).toContain("RecordsApiAccessLogs");
    expect(accessLogDestination).not.toContain("Fn::GetAtt");
    expect(accessLogDestination).not.toContain(":*");
    expect(JSON.stringify(stage?.DependsOn)).toContain("RecordsApiAccessLogs");
    const serialized = JSON.stringify(template.toJSON());
    expect(serialized).toContain("GET /api/v1/insights/rankings");
    expect(serialized).toContain("GET /api/v1/insights/affection-rankings");
    expect(serialized).toContain("GET /api/v1/insights/costs");
    expect(serialized).toContain("GET /api/v1/admin/prompts");
    expect(serialized).toContain("POST /api/v1/admin/prompts/apply");
    expect(serialized).toContain("GET /api/v1/admin/prompts/revisions");
    expect(serialized).toContain("GET /api/v1/admin/prompts/revisions/{revision}");
    expect(serialized).toContain("POST /api/v1/admin/prompts/rollback");
    expect(serialized).toContain("GET /api/v1/admin/status");
    expect(serialized).toContain("POST /api/v1/admin/status/refresh");
    expect(serialized).toContain("GET /api/v1/memorial");
    expect(serialized).toContain("POST /api/v1/memorial/upload");
    expect(serialized).toContain("POST /api/v1/memorial/generate");
    expect(serialized).toContain("GET /api/v1/memorial/memories/{cycle}");
    expect(serialized).toContain("POST /api/v1/memorial/reset");
    expect(template.toJSON().Parameters.RecordsBundleCodeSha256.Default).toBeUndefined();
    for (const alias of Object.values(template.findResources("AWS::Lambda::Alias"))) {
      expect(alias.Properties.Name).toBe("live");
    }
  });

  test("rebuilds rankings every 15 minutes without asynchronous retries", () => {
    const { template } = fixture;

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-ranking",
      Handler: "shittim_records.lambda_handlers.ranking_handler",
      MemorySize: 512,
      ReservedConcurrentExecutions: 1,
      Runtime: "python3.14",
      Timeout: 60,
      Environment: {
        Variables: {
          ARCHIVE_TABLE_NAME: "shittim-chest-production-records",
          STATISTICS_TABLE_NAME: "shittim-chest-production-records-statistics",
        },
      },
    });
    template.resourceCountIs("AWS::Events::Rule", 4);
    template.hasResourceProperties("AWS::Events::Rule", {
      ScheduleExpression: "rate(15 minutes)",
      State: "ENABLED",
      Targets: [
        {
          Arn: {
            "Fn::GetAtt": [Match.stringLikeRegexp("^RankingFunction"), "Arn"],
          },
          Id: Match.anyValue(),
          RetryPolicy: { MaximumRetryAttempts: 0 },
        },
      ],
    });
    template.resourceCountIs("AWS::Lambda::EventInvokeConfig", 3);
    template.hasResourceProperties("AWS::Lambda::EventInvokeConfig", {
      FunctionName: {
        Ref: Match.stringLikeRegexp("^RankingFunction"),
      },
      MaximumRetryAttempts: 0,
      Qualifier: "$LATEST",
    });
  });

  test("collects AWS, OpenAI, and Frankfurter cost inputs on bounded schedules", () => {
    const { template } = fixture;

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-cost",
      Handler: "shittim_records.lambda_handlers.cost_handler",
      MemorySize: 512,
      ReservedConcurrentExecutions: 1,
      Runtime: "python3.14",
      Timeout: 300,
      Environment: {
        Variables: {
          OPENAI_ADMIN_KEY_PARAMETER_NAME:
            "/shittim-chest/production/records/openai/admin-key",
          OPENAI_PROJECT_ID_PARAMETER_NAME:
            "/shittim-chest/production/records/openai/project-id",
          STATISTICS_TABLE_NAME: "shittim-chest-production-records-statistics",
        },
      },
    });
    template.hasResourceProperties("AWS::Events::Rule", {
      ScheduleExpression: "cron(17 3 * * ? *)",
      State: "ENABLED",
      Targets: [
        {
          Arn: {
            "Fn::GetAtt": [Match.stringLikeRegexp("^CostFunction"), "Arn"],
          },
          Id: Match.anyValue(),
          Input: '{"mode":"aws_fx"}',
          RetryPolicy: { MaximumRetryAttempts: 0 },
        },
      ],
    });
    template.hasResourceProperties("AWS::Events::Rule", {
      ScheduleExpression: "cron(37 * * * ? *)",
      State: "ENABLED",
      Targets: [
        {
          Arn: {
            "Fn::GetAtt": [Match.stringLikeRegexp("^CostFunction"), "Arn"],
          },
          Id: Match.anyValue(),
          Input: '{"mode":"openai"}',
          RetryPolicy: { MaximumRetryAttempts: 0 },
        },
      ],
    });
  });

  test("translates Inspector descriptions hourly with isolated least privilege", () => {
    const { template } = fixture;

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-inspector-translation",
      Handler: "shittim_records.lambda_handlers.inspector_translation_handler",
      MemorySize: 512,
      ReservedConcurrentExecutions: 1,
      Runtime: "python3.14",
      Timeout: 300,
      Environment: {
        Variables: {
          ECR_REPOSITORY_NAME: "shittim-chest",
          INSPECTOR_TRANSLATION_API_KEY_PARAMETER_NAME:
            "/shittim-chest/production/records/openai/inspector-translation-api-key",
          STATISTICS_TABLE_NAME: "shittim-chest-production-records-statistics",
        },
      },
    });
    template.hasResourceProperties("AWS::Events::Rule", {
      Description: "Translate unseen active Inspector descriptions hourly at minute 7",
      ScheduleExpression: "cron(7 * * * ? *)",
      State: "ENABLED",
      Targets: [
        {
          Arn: {
            "Fn::GetAtt": [
              Match.stringLikeRegexp("^InspectorTranslationFunction"),
              "Arn",
            ],
          },
          Id: Match.anyValue(),
          RetryPolicy: { MaximumRetryAttempts: 0 },
        },
      ],
    });
  });

  test("isolates memorial API and generation worker resources and permissions", () => {
    const { template } = fixture;

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-memorial-api",
      Handler: "shittim_records.lambda_handlers.memorial_api_handler",
      MemorySize: 512,
      ReservedConcurrentExecutions: 2,
      Runtime: "python3.14",
      Timeout: 15,
      Environment: {
        Variables: {
          SESSION_TABLE_NAME: "shittim-chest-production-records-sessions",
          SOURCE_TABLE_NAME: { Ref: "SourceDebateTableName" },
          STATISTICS_TABLE_NAME: "shittim-chest-production-records-statistics",
          MEMORIAL_UPLOAD_BUCKET_NAME:
            "shittim-chest-production-records-memorial-upload-000000000000",
          MEDIA_BUCKET_NAME: "shittim-chest-production-records-media-000000000000",
          MEMORIAL_GENERATION_QUEUE_URL: Match.anyValue(),
          OAUTH_CONFIG_PARAMETER_NAME:
            "/shittim-chest/production/records/discord/oauth/v0001",
          SESSION_KEY_PARAMETER_NAME:
            "/shittim-chest/production/records/session-key",
        },
      },
    });
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-memorial-worker",
      Handler: "shittim_records.lambda_handlers.memorial_worker_handler",
      MemorySize: 1024,
      ReservedConcurrentExecutions: 1,
      Runtime: "python3.14",
      Timeout: 300,
      Environment: {
        Variables: {
          ARCHIVE_TABLE_NAME: "shittim-chest-production-records",
          STATISTICS_TABLE_NAME: "shittim-chest-production-records-statistics",
          MEMORIAL_UPLOAD_BUCKET_NAME:
            "shittim-chest-production-records-memorial-upload-000000000000",
          MEDIA_BUCKET_NAME: "shittim-chest-production-records-media-000000000000",
          MEMORIAL_OPENAI_API_KEY_PARAMETER_NAME:
            "/shittim-chest/production/records/openai/memorial-api-key",
          RUNTIME_PROMPTS_PARAMETER_ROOT:
            "/shittim-chest/production/runtime-prompts",
          RUNTIME_PROMPTS_ACTIVE_PARAMETER_NAME:
            "/shittim-chest/production/runtime-prompts/active",
          LEGACY_PERSONA_PARTICIPANT_A_PARAMETER_NAME: Match.anyValue(),
          LEGACY_PERSONA_PARTICIPANT_B_PARAMETER_NAME: Match.anyValue(),
          LEGACY_PERSONA_PARTICIPANT_C_PARAMETER_NAME: Match.anyValue(),
        },
      },
    });
    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 1,
      EventSourceArn: Match.objectLike({
        "Fn::Join": Match.anyValue(),
      }),
      FunctionName: {
        Ref: Match.stringLikeRegexp("^MemorialWorkerFunction"),
      },
      FunctionResponseTypes: ["ReportBatchItemFailures"],
      ScalingConfig: Match.absent(),
    });

    const aliases = Object.values(template.findResources("AWS::Lambda::Alias"));
    const memorialAlias = aliases.find((alias) =>
      JSON.stringify(alias.Properties.FunctionName).includes("MemorialApiFunction"),
    );
    expect(memorialAlias?.Properties.Name).toBe("live");
    expect(JSON.stringify(memorialAlias?.Properties.FunctionVersion)).toContain(
      "MemorialApiVersion",
    );

    const policies = template.findResources("AWS::IAM::Policy");
    const apiPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("MemorialApiFunctionRole"),
    );
    const workerPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("MemorialWorkerFunctionRole"),
    );
    expect(apiPolicy).toBeDefined();
    expect(workerPolicy).toBeDefined();

    const apiText = JSON.stringify(apiPolicy);
    expect(apiText).toContain("SESSION#*");
    expect(apiText).toContain("AFFECTION#REQUESTER#*");
    expect(apiText).toContain("MEMORIAL#REQUESTER#*");
    expect(apiText).toContain("dynamodb:ConditionCheckItem");
    expect(apiText).toContain("dynamodb:EnclosingOperation");
    expect(apiText).not.toContain("dynamodb:TransactWriteItems");
    expect(apiText).toContain("dynamodb:Query");
    expect(apiText).toContain("s3:GetObject");
    expect(apiText).toContain("s3:PutObject");
    expect(apiText).toContain("s3:DeleteObject");
    expect(apiText).toContain("s3:ListBucket");
    expect(apiText).toContain("sqs:SendMessage");
    expect(apiText).toContain("ssm:GetParameters");
    expect(apiText).toContain("/records/discord/oauth/v0001");
    expect(apiText).toContain("/memorials/*");
    expect(apiText).not.toContain("sqs:ReceiveMessage");
    expect(apiText).not.toContain("/records/openai/memorial-api-key");
    expect(apiText).not.toContain("/participants/*");

    const workerText = JSON.stringify(workerPolicy);
    expect(workerText).toContain("MEMORIAL#REQUESTER#*");
    expect(workerText).toContain("dynamodb:GetItem");
    expect(workerText).toContain("dynamodb:UpdateItem");
    expect(workerText).toContain("dynamodb:Query");
    expect(workerText).toContain("/index/gsi3");
    expect(workerText).toContain("/participants/*");
    expect(workerText).toContain("/memorials/*");
    expect(workerText).toContain("s3:GetObject");
    expect(workerText).toContain("s3:PutObject");
    expect(workerText).toContain("s3:DeleteObject");
    expect(workerText).toContain("s3:ListBucket");
    expect(workerText).toContain("/records/openai/memorial-api-key");
    expect(workerText).toContain("/runtime-prompts/active");
    expect(workerText).toContain("/runtime-prompts/r??????????????????????????/*");
    expect(workerText).toContain("/participant-a");
    expect(workerText).toContain("/participant-b");
    expect(workerText).toContain("/participant-c");
    expect(workerText).toContain("sqs:ReceiveMessage");
    expect(workerText).toContain("sqs:DeleteMessage");
    expect(workerText).not.toContain("AFFECTION#REQUESTER#*");
    expect(workerText).not.toContain("SESSION#*");
    expect(workerText).not.toContain("dynamodb:PutItem");
    expect(workerText).not.toContain("dynamodb:TransactWriteItems");

    const apiStatements = apiPolicy?.Properties.PolicyDocument.Statement as PolicyStatement[];
    const uploadDelete = apiStatements.find((statement) =>
      actionsOf(statement).includes("s3:DeleteObject"),
    );
    expect(JSON.stringify(uploadDelete?.Resource)).toContain(
      "shittim-chest-production-records-memorial-upload",
    );
    expect(JSON.stringify(uploadDelete?.Resource)).not.toContain("/memorials/*");
    const apiListStatements = apiStatements.filter((statement) =>
      actionsOf(statement).includes("s3:ListBucket"),
    );
    expect(apiListStatements).toHaveLength(2);
    expect(apiListStatements.map((statement) => statement.Condition)).toEqual(
      expect.arrayContaining([
        { StringLike: { "s3:prefix": ["uploads/*"] } },
        { StringLike: { "s3:prefix": ["memorials/*"] } },
      ]),
    );
    expect(
      apiListStatements.every(
        (statement) => !JSON.stringify(statement.Resource).includes("/*"),
      ),
    ).toBe(true);
    const workerStatements = workerPolicy?.Properties.PolicyDocument.Statement as PolicyStatement[];
    const workerListStatements = workerStatements.filter((statement) =>
      actionsOf(statement).includes("s3:ListBucket"),
    );
    expect(workerListStatements).toHaveLength(1);
    expect(workerListStatements[0]?.Condition).toEqual({
      StringLike: { "s3:prefix": ["memorials/*"] },
    });
    expect(
      workerListStatements.every(
        (statement) => !JSON.stringify(statement.Resource).includes("/*"),
      ),
    ).toBe(true);
    const sourceWrite = apiStatements.find(
      (statement) =>
        actionsOf(statement).includes("dynamodb:UpdateItem") &&
        JSON.stringify(statement.Resource).includes("SourceDebateTableName"),
    );
    expect(sourceWrite?.Condition).toEqual({
      StringEquals: {
        "dynamodb:EnclosingOperation": "TransactWriteItems",
      },
      "ForAllValues:StringLike": {
        "dynamodb:LeadingKeys": ["AFFECTION#REQUESTER#*"],
      },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    const memorialWrite = apiStatements.find(
      (statement) =>
        actionsOf(statement).includes("dynamodb:PutItem") &&
        JSON.stringify(statement.Resource).includes(
          "table/shittim-chest-production-records-statistics",
        ),
    );
    expect(memorialWrite?.Condition).toEqual({
      StringEquals: {
        "dynamodb:EnclosingOperation": "TransactWriteItems",
      },
      "ForAllValues:StringLike": {
        "dynamodb:LeadingKeys": ["MEMORIAL#REQUESTER#*"],
      },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    for (const statement of apiStatements.filter((candidate) =>
      actionsOf(candidate).some((action) =>
        [
          "dynamodb:ConditionCheckItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ].includes(action),
      ),
    )) {
      expect(statement.Condition).toMatchObject({
        StringEquals: {
          "dynamodb:EnclosingOperation": "TransactWriteItems",
        },
      });
    }
  });

  test("keeps Auth, Read, Ranking, Cost, and translation IAM exact and disjoint", () => {
    const { template } = fixture;
    const policies = template.findResources("AWS::IAM::Policy");
    const auth = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("AuthFunctionRole"),
    );
    const read = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("ReadFunctionRole"),
    );
    const ranking = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("RankingFunctionRole"),
    );
    const cost = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("CostFunctionRole"),
    );
    const translation = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("InspectorTranslationFunctionRole"),
    );

    expect(auth).toBeDefined();
    expect(read).toBeDefined();
    expect(ranking).toBeDefined();
    expect(cost).toBeDefined();
    expect(translation).toBeDefined();
    const authText = JSON.stringify(auth);
    const readText = JSON.stringify(read);
    const rankingText = JSON.stringify(ranking);
    const costText = JSON.stringify(cost);
    const translationText = JSON.stringify(translation);
    expect(authText).toContain("dynamodb:TransactWriteItems");
    expect(authText).toContain("/requesters/*");
    expect(authText).not.toContain("/participants/*");
    expect(authText).not.toContain("/index/gsi");
    expect(readText).toContain("dynamodb:BatchGetItem");
    expect(readText).toContain("/index/gsi1");
    expect(readText).toContain("/index/gsi2");
    expect(readText).not.toContain("/index/gsi3");
    expect(readText).toContain("/participants/*");
    expect(readText).toContain("/requesters/*");
    expect(readText).not.toContain("dynamodb:PutItem");
    expect(readText).not.toContain("dynamodb:DeleteItem");
    expect(readText).not.toContain("s3:PutObject");
    expect(readText).toContain("shittim-chest-production-records-statistics");
    expect(readText).toContain("RANKING#AFFECTION");
    expect(readText).toContain("RANKING#AFFECTION#GEN#*");
    expect(readText).not.toContain("RANKING#AFFECTION#CATALOG");
    expect(readText).not.toContain("AFFECTION#SEED");
    expect(readText).not.toContain("/records/openai/admin-key");
    expect(readText).not.toContain("/records/openai/project-id");
    expect(rankingText).toContain("/index/gsi1");
    expect(rankingText).toContain("dynamodb:Query");
    expect(rankingText).toContain("dynamodb:PutItem");
    expect(rankingText).toContain("dynamodb:EnclosingOperation");
    expect(rankingText).toContain("TransactWriteItems");
    expect(rankingText).toContain("AFFECTION#PROFILE");
    expect(rankingText).toContain("RANKING#AFFECTION");
    expect(rankingText).toContain("RANKING#AFFECTION#GEN#*");
    expect(rankingText).toContain("RANKING#AFFECTION#CATALOG");
    expect(rankingText).toContain("dynamodb:BatchGetItem");
    expect(rankingText).toContain("dynamodb:BatchWriteItem");
    expect(rankingText).toContain("dynamodb:DeleteItem");
    expect(rankingText).not.toContain("dynamodb:Scan");
    expect(rankingText).not.toContain("dynamodb:UpdateItem");
    expect(rankingText).not.toContain("/records/openai/");
    expect(authText).not.toContain("/records/openai/");
    expect(costText).toContain("ce:GetCostAndUsage");
    expect(costText).toContain("ssm:GetParameters");
    expect(costText).toContain("/records/openai/admin-key");
    expect(costText).toContain("/records/openai/project-id");
    expect(costText).toContain("dynamodb:GetItem");
    expect(costText).toContain("dynamodb:PutItem");
    expect(costText).toContain("COLLECTOR#COST");
    expect(costText).toContain("COST#DAILY");
    expect(costText).toContain("FX#DAILY");
    expect(costText).not.toContain("dynamodb:DeleteItem");
    expect(costText).not.toContain("dynamodb:Scan");
    expect(translationText).toContain("ecr:DescribeImages");
    expect(translationText).toContain("inspector2:ListFindings");
    expect(translationText).toContain("ssm:GetParameters");
    expect(translationText).toContain(
      "/records/openai/inspector-translation-api-key",
    );
    expect(translationText).toContain("dynamodb:BatchGetItem");
    expect(translationText).toContain("dynamodb:PutItem");
    expect(translationText).toContain("ADMIN#INSPECTOR_TRANSLATION");
    expect(translationText).not.toContain("/records/openai/admin-key");
    expect(translationText).not.toContain("/records/openai/project-id");
    expect(translationText).not.toContain("dynamodb:DeleteItem");
    expect(translationText).not.toContain("dynamodb:Scan");
  });

  test("isolates ADMIN prompt writes from sanitized read-only status access", () => {
    const { template } = fixture;
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-admin-config",
      Handler: "shittim_records.lambda_handlers.admin_config_handler",
      MemorySize: 512,
      ReservedConcurrentExecutions: 2,
      Runtime: "python3.14",
      Timeout: 15,
      Environment: {
        Variables: {
          RUNTIME_PROMPTS_PARAMETER_ROOT: "/shittim-chest/production/runtime-prompts",
          LEGACY_PERSONA_MODERATOR_PARAMETER_NAME: {
            "Fn::Join": [
              "",
              [
                "/shittim-chest/production/personas/",
                { Ref: "LegacyRuntimeConfigVersion" },
                "/moderator",
              ],
            ],
          },
        },
      },
    });
    const policies = template.findResources("AWS::IAM::Policy");
    const configPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("AdminConfigFunctionRole"),
    );
    const statusPolicies = Object.values(policies).filter((policy) =>
      JSON.stringify(policy).includes("AdminStatusFunctionRole"),
    );

    expect(configPolicy).toBeDefined();
    expect(statusPolicies).toHaveLength(5);
    const configText = JSON.stringify(configPolicy);
    const statusText = JSON.stringify(statusPolicies);
    expect(configText).toContain("/records/admin/discord-user-id");
    expect(configText).toContain("/runtime-prompts/r??????????????????????????/*");
    expect(configText).toContain("/personas/");
    expect(configText).toContain("ssm:GetParameter");
    expect(configText).toContain("ssm:GetParameters");
    expect(configText).toContain("ssm:PutParameter");
    expect(configText).toContain("ssm:DeleteParameters");
    expect(configText).toContain("ADMIN#PROMPT");
    expect(configText).toContain("dynamodb:EnclosingOperation");
    expect(configText).toContain('"ssm:Overwrite":"false"');
    expect(configText).toContain('"ssm:Overwrite":"true"');
    expect(configText).toContain('"Effect":"Deny"');
    expect(configText).toContain("dynamodb:DeleteItem");
    expect(configText).not.toContain("cloudwatch:");
    expect(configText).not.toContain("ecs:");
    expect(statusText).not.toContain("ssm:PutParameter");
    expect(statusText).not.toContain("ADMIN#PROMPT");

    const configStatements = configPolicy?.Properties.PolicyDocument.Statement as PolicyStatement[];
    const promptPutStatements = configStatements.filter((statement) =>
      actionsOf(statement).includes("ssm:PutParameter"),
    );
    const promptDeleteStatements = configStatements.filter((statement) =>
      actionsOf(statement).includes("ssm:DeleteParameters"),
    );
    expect(promptPutStatements).toHaveLength(3);
    expect(
      promptPutStatements.find(
        (statement) =>
          statement.Effect === "Allow" &&
          statement.Condition === undefined &&
          JSON.stringify(statement.Resource).includes("/runtime-prompts/active"),
      ),
    ).toBeDefined();
    expect(promptDeleteStatements).toHaveLength(1);
    expect(promptDeleteStatements[0]?.Effect).toBe("Allow");
    expect(JSON.stringify(promptDeleteStatements[0]?.Resource)).toContain(
      "/runtime-prompts/r??????????????????????????/*",
    );
    expect(JSON.stringify(promptDeleteStatements[0]?.Resource)).not.toContain(
      "/runtime-prompts/active",
    );
    expect(
      configStatements.some((statement) => actionsOf(statement).includes("ssm:DeleteParameter")),
    ).toBe(false);
    expect(
      promptPutStatements.find(
        (statement) =>
          statement.Effect === "Allow" &&
          JSON.stringify(statement.Condition) ===
            JSON.stringify({ StringEquals: { "ssm:Overwrite": "false" } }),
      ),
    ).toBeDefined();
    expect(
      promptPutStatements.find(
        (statement) =>
          statement.Effect === "Deny" &&
          JSON.stringify(statement.Condition) ===
            JSON.stringify({ StringEquals: { "ssm:Overwrite": "true" } }),
      ),
    ).toBeDefined();

    const adminWrite = configStatements.find((statement) =>
      actionsOf(statement).includes("dynamodb:DeleteItem"),
    );
    expect(adminWrite?.Condition).toEqual({
      StringEquals: { "dynamodb:EnclosingOperation": "TransactWriteItems" },
      "ForAllValues:StringEquals": { "dynamodb:LeadingKeys": ["ADMIN#PROMPT"] },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
  });

  test("keeps ADMIN status access read-only and least privilege", () => {
    const { template } = fixture;
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-admin-status",
      ReservedConcurrentExecutions: 2,
      Environment: {
        Variables: {
          ADMIN_ALARM_PREFIX: "shittim-chest-production-",
          ADMIN_AWS_ACCOUNT_ID: "000000000000",
          ECS_CONTAINER_NAME: "application",
          RECORDS_PUBLIC_HOSTNAME: { Ref: "RecordsPublicHostname" },
          RUNTIME_STACK_NAME: "ShittimChest-Prod-Runtime",
          RUNTIME_SCHEDULER_NAME: "shittim-chest-production-runtime-reconciler",
          SIGNING_PROFILE_NAME: "shittim_chest_ecr",
          COST_ANOMALY_SUBSCRIPTION_NAME: "shittim-chest-production-cost-anomalies",
          MEMORIAL_UPLOAD_BUCKET_NAME:
            "shittim-chest-production-records-memorial-upload-000000000000",
          MEMORIAL_GENERATION_QUEUE_URL: Match.anyValue(),
          MEMORIAL_GENERATION_DLQ_URL: Match.anyValue(),
        },
      },
    });
    const policies = template.findResources("AWS::IAM::Policy");
    const statusPolicies = Object.values(policies).filter((policy) =>
      JSON.stringify(policy).includes("AdminStatusFunctionRole"),
    );

    expect(statusPolicies).toHaveLength(5);
    const statusText = JSON.stringify(statusPolicies);

    for (const action of [
      "acm:DescribeCertificate",
      "apigateway:GET",
      "budgets:ViewBudget",
      "ce:GetAnomalySubscriptions",
      "cloudfront:GetDistribution",
      "cloudfront:ListDistributions",
      "cloudformation:DescribeStacks",
      "cloudformation:ListStackResources",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "dynamodb:DescribeTable",
      "ecr:DescribeImages",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "events:DescribeRule",
      "inspector2:ListCoverage",
      "inspector2:ListFindingAggregations",
      "inspector2:ListFindings",
      "lambda:GetFunctionConcurrency",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "scheduler:GetSchedule",
      "signer:GetSigningProfile",
      "sns:GetTopicAttributes",
      "sqs:GetQueueAttributes",
      "ssm:DescribeParameters",
    ]) {
      expect(statusText).toContain(action);
    }
    expect(statusText).toContain("/records/admin/discord-user-id");
    expect(statusText).toContain(
      "shittim-chest-production-records-memorial-generation",
    );
    expect(statusText).toContain(
      "shittim-chest-production-records-memorial-generation-dlq",
    );
    expect(statusText).not.toContain("ssm:PutParameter");
    expect(statusText).not.toContain("ssm:DeleteParameters");
    expect(statusText).not.toContain("dynamodb:PutItem");
    expect(statusText).not.toContain("dynamodb:UpdateItem");
    expect(statusText).not.toContain("dynamodb:DeleteItem");
    expect(statusText).not.toContain("sqs:ReceiveMessage");
    expect(statusText).not.toContain("lambda:InvokeFunction");
    expect(statusText).not.toContain("ecs:UpdateService");
    expect(statusText).not.toContain("cloudformation:UpdateStack");
    expect(statusText).not.toContain("cloudformation:DetectStackDrift");
    expect(statusText).not.toContain("sns:ListSubscriptions");
    for (const stackName of [
      "Stateful",
      "ReleaseIdentity",
      "Runtime",
      "Operations",
      "CostGovernance",
      "RecordsStateful",
      "RecordsApplication",
      "RecordsEdge",
    ]) {
      expect(statusText).toContain(`stack/ShittimChest-Prod-${stackName}/*`);
    }

    const statusStatements = statusPolicies.flatMap(
      (policy) => policy.Properties.PolicyDocument.Statement,
    ) as PolicyStatement[];
    const statusTaskDefinitionRead = statusStatements.find((statement) =>
      actionsOf(statement).includes("ecs:DescribeTaskDefinition"),
    );
    expect(statusTaskDefinitionRead?.Resource).toBe("*");
    expect(statusTaskDefinitionRead?.Condition).toEqual({
      StringEquals: { "aws:RequestedRegion": "ap-northeast-1" },
    });
    const statusEventBridgeRead = statusStatements.find((statement) =>
      actionsOf(statement).includes("events:DescribeRule"),
    );
    expect(Array.isArray(statusEventBridgeRead?.Resource)).toBe(true);
    const statusEventBridgeArns = statusEventBridgeRead?.Resource as unknown[];
    const eventRules = template.findResources("AWS::Events::Rule");
    for (const description of [
      "Rebuild the Records ranking snapshots every 15 minutes",
      "Collect Project-tagged AWS costs and USD/JPY rates daily at 12:17 JST",
      "Collect project-scoped OpenAI organization costs hourly at minute 37",
      "Translate unseen active Inspector descriptions hourly at minute 7",
    ]) {
      const [logicalId] = Object.entries(eventRules).find(
        ([, resource]) => resource.Properties.Description === description,
      ) ?? [undefined];
      expect(logicalId).toBeDefined();
      expect(statusEventBridgeArns).toContainEqual({
        "Fn::GetAtt": [logicalId, "Arn"],
      });
    }
    expect(statusEventBridgeArns).toHaveLength(5);
    expect(JSON.stringify(statusEventBridgeArns)).not.toContain(
      "ShittimChest-Prod-RecordsApplication-*",
    );
    const statusLambdaReads = statusStatements.filter((statement) =>
      actionsOf(statement).includes("lambda:GetFunctionConfiguration"),
    );
    expect(statusLambdaReads).toHaveLength(2);
    const statusLambdaArns = (
      statusLambdaReads.flatMap((statement) => statement.Resource) as Array<{
        readonly "Fn::Join": readonly [
          string,
          ReadonlyArray<string | { readonly Ref: string }>,
        ];
      }>
    ).map((resource) => {
      const [separator, parts] = resource["Fn::Join"];
      expect(separator).toBe("");
      return parts
        .map((part) => {
          if (typeof part === "string") {
            return part;
          }
          expect(part).toEqual({ Ref: "AWS::Partition" });
          return "aws";
        })
        .join("");
    });
    expect(statusLambdaArns.sort()).toEqual(
      [
        "arn:aws:lambda:ap-northeast-1:000000000000:function:shittim-chest-production-discord-ingress",
        "arn:aws:lambda:ap-northeast-1:000000000000:function:shittim-chest-production-discord-status-publisher",
        "arn:aws:lambda:ap-northeast-1:000000000000:function:shittim-chest-production-image-admission",
        ...recordsFunctionNames.map(
          (name) => `arn:aws:lambda:ap-northeast-1:000000000000:function:${name}`,
        ),
        "arn:aws:lambda:ap-northeast-1:000000000000:function:shittim-chest-production-runtime-reconciler",
      ].sort(),
    );
    expect(JSON.stringify(statusLambdaArns)).not.toContain(":function/");
    const statusSessionRead = statusStatements.find(
      (statement) =>
        actionsOf(statement).includes("dynamodb:GetItem") &&
        JSON.stringify(statement.Resource).includes(
          "table/shittim-chest-production-records-sessions",
        ),
    );
    expect(statusSessionRead?.Condition).toEqual({
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["SESSION#*"] },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    const statusControlRead = statusStatements.find(
      (statement) =>
        actionsOf(statement).includes("dynamodb:GetItem") &&
        statement.Condition !== undefined &&
        Object.hasOwn(statement.Condition, "ForAllValues:StringEquals"),
    );
    expect(statusControlRead?.Condition).toEqual({
      "ForAllValues:StringEquals": {
        "dynamodb:LeadingKeys": [
          "CONTROL#RUNTIME",
          "CONTROL#DEBATE",
          "CONTROL#OUTBOX",
        ],
      },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    const statusCollectorRead = statusStatements.find(
      (statement) =>
        actionsOf(statement).includes("dynamodb:GetItem") &&
        JSON.stringify(statement.Resource).includes(
          "table/shittim-chest-production-records-statistics",
        ) &&
        JSON.stringify(statement.Condition).includes("COLLECTOR#COST"),
    );
    expect(statusCollectorRead?.Condition).toEqual({
      "ForAllValues:StringEquals": {
        "dynamodb:LeadingKeys": [
          "AFFECTION#SEED",
          "COLLECTOR#COST",
          "RANKING#AFFECTION",
        ],
      },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    const statusTranslationRead = statusStatements.find(
      (statement) =>
        actionsOf(statement).includes("dynamodb:BatchGetItem") &&
        JSON.stringify(statement.Resource).includes(
          "table/shittim-chest-production-records-statistics",
        ) &&
        JSON.stringify(statement.Condition).includes("ADMIN#INSPECTOR_TRANSLATION"),
    );
    expect(statusTranslationRead?.Condition).toEqual({
      "ForAllValues:StringEquals": {
        "dynamodb:LeadingKeys": ["ADMIN#INSPECTOR_TRANSLATION"],
      },
      Null: { "dynamodb:LeadingKeys": "false" },
    });

    const parameters = template.toJSON().Parameters;
    for (const name of ["RecordsPublicHostname", "LegacyRuntimeConfigVersion"]) {
      expect(parameters[name].Default).toBeUndefined();
    }
    expect(parameters.RuntimeImageDigest).toBeUndefined();
    expect(parameters.BreakGlassImageDigest).toBeUndefined();
    expect(parameters.RecordsCertificateArn).toBeUndefined();
  });

  test("filters completed metadata and bounds every stream retry dimension", () => {
    const { template } = fixture;

    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 1,
      BisectBatchOnFunctionError: true,
      EventSourceArn: { Ref: "SourceDebateTableStreamArn" },
      FilterCriteria: {
        Filters: [
          {
            Pattern:
              '{"eventName":["MODIFY"],"dynamodb":{"NewImage":{"record_type":{"S":["debate_meta"]},"current_phase":{"S":["completed"]}}}}',
          },
          {
            Pattern:
              '{"eventName":["INSERT","MODIFY"],"dynamodb":{"NewImage":{"record_type":{"S":["affection_profile"]},"schema_version":{"N":["8","9"]}}}}',
          },
          {
            Pattern:
              '{"eventName":["REMOVE"],"dynamodb":{"Keys":{"PK":{"S":[{"prefix":"AFFECTION#REQUESTER#"}]},"SK":{"S":["PROFILE"]}}}}',
          },
        ],
      },
      FunctionResponseTypes: ["ReportBatchItemFailures"],
      MaximumRecordAgeInSeconds: 3600,
      MaximumRetryAttempts: 3,
      StartingPosition: "TRIM_HORIZON",
    });
  });

  test("keeps source writes and projector scans out of both execution roles", () => {
    const { template } = fixture;
    const policies = template.findResources("AWS::IAM::Policy");
    const serialized = JSON.stringify(policies);
    const projectionPolicies = Object.values(policies).filter((policy) => {
      const value = JSON.stringify(policy);
      return value.includes("ProjectorFunctionRole") || value.includes("BackfillFunctionRole");
    });
    const projectionText = JSON.stringify(projectionPolicies);

    expect(projectionText).not.toContain("dynamodb:DeleteItem");
    expect(projectionText).not.toContain("dynamodb:BatchWriteItem");
    const projectorPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("ProjectorFunctionRole"),
    );
    expect(projectorPolicy).toBeDefined();
    const projectorText = JSON.stringify(projectorPolicy);
    const backfillPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("BackfillFunctionRole"),
    );
    const backfillText = JSON.stringify(backfillPolicy);
    expect(projectorText).not.toContain("dynamodb:Scan");
    expect(projectorText).toContain("AFFECTION#REQUESTER#*");
    expect(projectorText).toContain("AFFECTION#PROFILE");
    expect(projectorText).toContain("dynamodb:UpdateItem");
    expect(projectorText).toContain("RECORD_LINK_NOTIFICATION");
    expect(projectorText).toContain("ssm:GetParameter");
    expect(projectorText).toContain("/shittim-chest/production/discord/moderator/token");
    expect(backfillText).not.toContain("dynamodb:UpdateItem");
    expect(backfillText).not.toContain("RECORD_LINK_NOTIFICATION");
    expect(backfillText).not.toContain("discord/moderator/token");
    expect(serialized).toContain("dynamodb:Query");
    expect(serialized).toContain("dynamodb:PutItem");
    expect(serialized).toContain("ssm:GetParameters");
    expect(serialized).not.toContain("ssm:GetParameterHistory");
    for (const role of ["ProjectorFunctionRole", "BackfillFunctionRole"]) {
      const archivePolicy = Object.values(policies).find((policy) => {
        const value = JSON.stringify(policy);
        return (
          value.includes(role) &&
          value.includes("dynamodb:PutItem") &&
          value.includes("dynamodb:EnclosingOperation") &&
          value.includes("TransactWriteItems")
        );
      });
      expect(archivePolicy).toBeDefined();
    }

    expect(projectionText).not.toContain('"Action":"dynamodb:TransactWriteItems"');

    for (const role of ["ProjectorFunctionRole", "BackfillFunctionRole"]) {
      const policy = Object.values(policies).find((candidate) =>
        JSON.stringify(candidate).includes(role),
      );
      expect(policy).toBeDefined();
      const statements = policy?.Properties.PolicyDocument.Statement as Array<{
        readonly Action: string | string[];
        readonly Resource: unknown;
        readonly Condition?: unknown;
      }>;
      const archivePutStatements = statements.filter((statement) => {
        const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
        return (
          actions.length === 1 &&
          actions[0] === "dynamodb:PutItem" &&
          JSON.stringify(statement.Resource).includes("table/shittim-chest-production-records") &&
          JSON.stringify(statement.Condition) ===
            JSON.stringify({
              StringEquals: {
                "dynamodb:EnclosingOperation": "TransactWriteItems",
              },
            })
        );
      });
      expect(archivePutStatements).toHaveLength(1);
      expect(archivePutStatements[0]?.Condition).toEqual({
        StringEquals: { "dynamodb:EnclosingOperation": "TransactWriteItems" },
      });
    }

    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "shittim-chest-production-records-projector",
      Environment: {
        Variables: Match.objectLike({
          RECORDS_PUBLIC_HOSTNAME: { Ref: "RecordsPublicHostname" },
          SHITTIM_MODERATOR_TOKEN_PARAMETER:
            "/shittim-chest/production/discord/moderator/token",
          SHITTIM_RUNTIME_CONFIG_PARAMETER: Match.anyValue(),
        }),
      },
    });
    const functions = template.findResources("AWS::Lambda::Function");
    const backfill = Object.values(functions).find(
      (resource) =>
        resource.Properties.FunctionName === "shittim-chest-production-records-backfill",
    );
    expect(backfill?.Properties.Environment.Variables).not.toHaveProperty(
      "SHITTIM_MODERATOR_TOKEN_PARAMETER",
    );
  });

  test("does not recreate the source debate table", () => {
    const { template } = fixture;

    template.resourceCountIs("AWS::DynamoDB::Table", 0);
    expect(template.toJSON().Parameters).toHaveProperty("SourceDebateTableName");
    expect(template.toJSON().Parameters).toHaveProperty("SourceDebateTableStreamArn");
  });

  test("has no unacknowledged AWS Solutions findings", () => {
    const { checks, stack } = fixture;

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
