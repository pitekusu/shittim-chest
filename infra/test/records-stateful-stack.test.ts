import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { RecordsStatefulStack } from "../lib/records-stateful-stack";

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly stack: RecordsStatefulStack;
  readonly template: Template;
} {
  const app = new App();
  const stack = new RecordsStatefulStack(app, "RecordsStateful", {
    env: { account: "000000000000", region: "ap-northeast-1" },
    stackName: "ShittimChest-Prod-RecordsStateful",
    terminationProtection: true,
  });
  Tags.of(stack).add("Project", "shittim-chest");
  Tags.of(stack).add("Environment", "production");
  Tags.of(stack).add("ManagedBy", "cdk");
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { checks, stack, template: Template.fromStack(stack) };
}

describe("RecordsStatefulStack", () => {
  test("retains every table while expiring only ephemeral session records", () => {
    const { stack, template } = synthesize();

    expect(stack.terminationProtection).toBe(true);
    template.resourceCountIs("AWS::DynamoDB::Table", 3);
    for (const tableName of [
      "shittim-chest-production-records",
      "shittim-chest-production-records-statistics",
    ]) {
      template.hasResource("AWS::DynamoDB::Table", {
        DeletionPolicy: "Retain",
        UpdateReplacePolicy: "Retain",
        Properties: Match.objectLike({
          BillingMode: "PAY_PER_REQUEST",
          DeletionProtectionEnabled: true,
          PointInTimeRecoverySpecification: {
            PointInTimeRecoveryEnabled: true,
            RecoveryPeriodInDays: 35,
          },
          TableName: tableName,
        }),
      });
    }
    template.hasResource("AWS::DynamoDB::Table", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: Match.objectLike({
        DeletionProtectionEnabled: true,
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
          RecoveryPeriodInDays: 35,
        },
        TableName: "shittim-chest-production-records-sessions",
        TimeToLiveSpecification: { AttributeName: "expiresAt", Enabled: true },
      }),
    });
  });

  test("creates all three immutable archive lookup indexes", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "shittim-chest-production-records",
      GlobalSecondaryIndexes: [
        Match.objectLike({ IndexName: "gsi1" }),
        Match.objectLike({ IndexName: "gsi2" }),
        Match.objectLike({ IndexName: "gsi3" }),
      ],
    });
  });

  test("keeps media private and gives the projector a bounded failure destination", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "shittim-chest-production-records-media-000000000000",
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: "Enabled" },
    });
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "shittim-chest-production-records-projector-dlq",
      MessageRetentionPeriod: 1209600,
      SqsManagedSseEnabled: true,
    });
  });

  test("has no unacknowledged AWS Solutions findings", () => {
    const { checks, stack } = synthesize();

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
