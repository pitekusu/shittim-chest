import { App, Tags, Validations } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
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
  test("creates two Python 3.14 ARM64 functions from an immutable S3 version", () => {
    const { stack, template } = synthesize();

    expect(stack.terminationProtection).toBe(true);
    template.resourceCountIs("AWS::Lambda::Function", 2);
    for (const functionName of [
      "shittim-chest-production-records-projector",
      "shittim-chest-production-records-backfill",
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

    template.resourceCountIs("AWS::Logs::LogGroup", 2);
    for (const functionName of [
      "shittim-chest-production-records-projector",
      "shittim-chest-production-records-backfill",
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

    expect(serialized).not.toContain("dynamodb:DeleteItem");
    expect(serialized).not.toContain("dynamodb:UpdateItem");
    expect(serialized).not.toContain("dynamodb:BatchWriteItem");
    const projectorPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("ProjectorFunctionRole"),
    );
    expect(projectorPolicy).toBeDefined();
    expect(JSON.stringify(projectorPolicy)).not.toContain("dynamodb:Scan");
    expect(serialized).toContain("dynamodb:Query");
    expect(serialized).toContain("dynamodb:TransactWriteItems");
    expect(serialized).toContain("dynamodb:PutItem");
    expect(serialized).toContain("ssm:GetParameters");
    expect(serialized).not.toContain("ssm:GetParameterHistory");
    for (const role of ["ProjectorFunctionRole", "BackfillFunctionRole"]) {
      const archivePolicy = Object.values(policies).find((policy) => {
        const value = JSON.stringify(policy);
        return value.includes(role) && value.includes("dynamodb:TransactWriteItems");
      });
      expect(archivePolicy).toBeDefined();
    }

    const backfillPolicy = Object.values(policies).find((policy) =>
      JSON.stringify(policy).includes("BackfillFunctionRole"),
    );
    expect(backfillPolicy).toBeDefined();
    const backfillStatements = backfillPolicy?.Properties.PolicyDocument.Statement as Array<{
      readonly Action: string | string[];
      readonly Resource: unknown;
    }>;
    const archivePutStatements = backfillStatements.filter((statement) => {
      const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      return (
        actions.length === 1 &&
        actions[0] === "dynamodb:PutItem" &&
        JSON.stringify(statement.Resource).includes("table/shittim-chest-production-records")
      );
    });
    expect(archivePutStatements).toHaveLength(1);
    expect(JSON.stringify(archivePutStatements[0]?.Resource)).not.toContain(
      "records-statistics",
    );
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
