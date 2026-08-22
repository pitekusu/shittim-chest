import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { RecordsApplicationStack } from "../lib/records-application-stack";
import { RecordsStatefulStack } from "../lib/records-stateful-stack";

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

  test("creates five Python 3.14 ARM64 functions from one immutable S3 version", () => {
    const { stack, template } = synthesize();

    expect(stack.terminationProtection).toBe(true);
    template.resourceCountIs("AWS::Lambda::Function", 5);
    for (const functionName of [
      "shittim-chest-production-records-projector",
      "shittim-chest-production-records-backfill",
      "shittim-chest-production-records-auth",
      "shittim-chest-production-records-ranking",
      "shittim-chest-production-records-read",
    ]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Architectures: ["arm64"],
        Code: {
          S3Bucket: { Ref: "RecordsBundleBucketName" },
          S3Key: { Ref: "RecordsBundleObjectKey" },
          S3ObjectVersion: { Ref: "RecordsBundleObjectVersion" },
        },
        FunctionName: functionName,
        MemorySize: 512,
        Runtime: "python3.14",
      });
    }
  });

  test("binds each function to one explicit 90-day log group", () => {
    const { template } = synthesize();
    const logGroups = template.findResources("AWS::Logs::LogGroup");

    template.resourceCountIs("AWS::Logs::LogGroup", 6);
    for (const functionName of [
      "shittim-chest-production-records-projector",
      "shittim-chest-production-records-backfill",
      "shittim-chest-production-records-auth",
      "shittim-chest-production-records-ranking",
      "shittim-chest-production-records-read",
    ]) {
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

  test("publishes Auth and Read aliases behind exactly seven HTTP API routes", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::Lambda::Version", 2);
    template.resourceCountIs("AWS::Lambda::Alias", 2);
    template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
    template.resourceCountIs("AWS::ApiGatewayV2::Route", 7);
    template.resourceCountIs("AWS::ApiGatewayV2::Stage", 1);
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      AutoDeploy: true,
      DefaultRouteSettings: {
        ThrottlingBurstLimit: 20,
        ThrottlingRateLimit: 10,
      },
      StageName: "$default",
    });
    const serialized = JSON.stringify(template.toJSON());
    expect(serialized).toContain("GET /api/v1/insights/rankings");
    expect(serialized).not.toContain("/api/v1/insights/costs");
    expect(template.toJSON().Parameters.RecordsBundleCodeSha256.Default).toBeUndefined();
    for (const alias of Object.values(template.findResources("AWS::Lambda::Alias"))) {
      expect(alias.Properties.Name).toBe("live");
    }
  });

  test("rebuilds rankings every 15 minutes without asynchronous retries", () => {
    const { template } = synthesize();

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
    template.resourceCountIs("AWS::Events::Rule", 1);
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
  });

  test("keeps Auth, Read, and Ranking IAM resources exact and disjoint", () => {
    const { template } = synthesize();
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

    expect(auth).toBeDefined();
    expect(read).toBeDefined();
    expect(ranking).toBeDefined();
    const authText = JSON.stringify(auth);
    const readText = JSON.stringify(read);
    const rankingText = JSON.stringify(ranking);
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
    expect(rankingText).toContain("/index/gsi1");
    expect(rankingText).toContain("dynamodb:Query");
    expect(rankingText).toContain("dynamodb:PutItem");
    expect(rankingText).toContain("dynamodb:EnclosingOperation");
    expect(rankingText).toContain("TransactWriteItems");
    expect(rankingText).not.toContain("dynamodb:Scan");
    expect(rankingText).not.toContain("dynamodb:GetItem");
    expect(rankingText).not.toContain("dynamodb:UpdateItem");
    expect(rankingText).not.toContain("dynamodb:DeleteItem");
  });

  test("filters completed metadata and bounds every stream retry dimension", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 10,
      BisectBatchOnFunctionError: true,
      EventSourceArn: { Ref: "SourceDebateTableStreamArn" },
      FilterCriteria: {
        Filters: [
          {
            Pattern:
              '{"eventName":["MODIFY"],"dynamodb":{"NewImage":{"record_type":{"S":["debate_meta"]},"current_phase":{"S":["completed"]}}}}',
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
    const { template } = synthesize();
    const policies = template.findResources("AWS::IAM::Policy");
    const serialized = JSON.stringify(policies);
    const projectionPolicies = Object.values(policies).filter((policy) => {
      const value = JSON.stringify(policy);
      return value.includes("ProjectorFunctionRole") || value.includes("BackfillFunctionRole");
    });
    const projectionText = JSON.stringify(projectionPolicies);

    expect(projectionText).not.toContain("dynamodb:DeleteItem");
    expect(projectionText).not.toContain("dynamodb:UpdateItem");
    expect(projectionText).not.toContain("dynamodb:BatchWriteItem");
    const projectorPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("ProjectorFunctionRole"),
    );
    expect(projectorPolicy).toBeDefined();
    expect(JSON.stringify(projectorPolicy)).not.toContain("dynamodb:Scan");
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
          JSON.stringify(statement.Resource).includes("table/shittim-chest-production-records")
        );
      });
      expect(archivePutStatements).toHaveLength(1);
      expect(archivePutStatements[0]?.Condition).toEqual({
        StringEquals: { "dynamodb:EnclosingOperation": "TransactWriteItems" },
      });
    }
  });

  test("does not recreate the source debate table", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::DynamoDB::Table", 0);
    expect(template.toJSON().Parameters).toHaveProperty("SourceDebateTableName");
    expect(template.toJSON().Parameters).toHaveProperty("SourceDebateTableStreamArn");
  });

  test("has no unacknowledged AWS Solutions findings", () => {
    const { checks, stack } = synthesize();

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
