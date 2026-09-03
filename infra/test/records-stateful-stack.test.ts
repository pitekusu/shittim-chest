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

  test("retains a private, short-lived memorial upload bucket", () => {
    const { template } = synthesize();

    template.hasResource("AWS::S3::Bucket", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            {
              ServerSideEncryptionByDefault: {
                SSEAlgorithm: "AES256",
              },
            },
          ],
        },
        BucketName: "shittim-chest-production-records-memorial-upload-000000000000",
        CorsConfiguration: {
          CorsRules: [
            {
              AllowedHeaders: ["content-type"],
              AllowedMethods: ["POST"],
              AllowedOrigins: ["https://shittim.pitekusu.dev"],
              MaxAge: 300,
            },
          ],
        },
        LifecycleConfiguration: {
          Rules: [
            {
              AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 },
              ExpirationInDays: 1,
              Id: "ExpireMemorialUploads",
              Status: "Enabled",
            },
          ],
        },
        LoggingConfiguration: {
          DestinationBucketName: { Ref: Match.stringLikeRegexp("^MediaAccessLogs") },
          LogFilePrefix: "memorial-upload/",
        },
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
        VersioningConfiguration: Match.absent(),
      },
    });
    template.hasResourceProperties("AWS::S3::BucketPolicy", {
      Bucket: { Ref: Match.stringLikeRegexp("^MemorialUploadBucket") },
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "s3:*",
            Condition: { Bool: { "aws:SecureTransport": "false" } },
            Effect: "Deny",
          }),
        ]),
      },
    });
  });

  test("retains a bounded memorial generation queue and dead-letter queue", () => {
    const { template } = synthesize();

    template.hasResource("AWS::SQS::Queue", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        MessageRetentionPeriod: 1209600,
        QueueName: "shittim-chest-production-records-memorial-generation-dlq",
        SqsManagedSseEnabled: true,
      },
    });
    template.hasResource("AWS::SQS::Queue", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        MessageRetentionPeriod: 86400,
        QueueName: "shittim-chest-production-records-memorial-generation",
        RedrivePolicy: {
          deadLetterTargetArn: {
            "Fn::GetAtt": [
              Match.stringLikeRegexp("^MemorialGenerationDlq"),
              "Arn",
            ],
          },
          maxReceiveCount: 3,
        },
        SqsManagedSseEnabled: true,
        VisibilityTimeout: 1800,
      },
    });
    for (const queueLogicalId of [
      "MemorialGenerationDlq",
      "MemorialGenerationQueue",
    ]) {
      template.hasResourceProperties("AWS::SQS::QueuePolicy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "sqs:*",
              Condition: { Bool: { "aws:SecureTransport": "false" } },
              Effect: "Deny",
              Resource: {
                "Fn::GetAtt": [Match.stringLikeRegexp(`^${queueLogicalId}`), "Arn"],
              },
            }),
          ]),
        },
      });
    }
  });

  test("has no unacknowledged AWS Solutions findings", () => {
    const { checks, stack } = synthesize();

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
