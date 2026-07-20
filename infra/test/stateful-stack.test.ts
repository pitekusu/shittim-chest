import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { StatefulStack } from "../lib/stateful-stack";

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly stack: StatefulStack;
  readonly template: Template;
} {
  const app = new App();
  const stack = new StatefulStack(app, "Stateful", {
    env: { account: "000000000000", region: "ap-northeast-1" },
    stackName: "ShittimChest-Prod-Stateful",
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

describe("StatefulStack", () => {
  test("creates the retained and protected DynamoDB table", () => {
    const { stack, template } = synthesize();

    expect(stack.terminationProtection).toBe(true);
    template.resourceCountIs("AWS::DynamoDB::Table", 1);
    template.hasResource("AWS::DynamoDB::Table", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        BillingMode: "PAY_PER_REQUEST",
        DeletionProtectionEnabled: true,
        KeySchema: [
          { AttributeName: "PK", KeyType: "HASH" },
          { AttributeName: "SK", KeyType: "RANGE" },
        ],
        PointInTimeRecoverySpecification: {
          PointInTimeRecoveryEnabled: true,
          RecoveryPeriodInDays: 35,
        },
        SSESpecification: { SSEEnabled: true },
        TableName: "shittim-chest-production",
      },
    });
  });

  test("creates the two all-projection lookup indexes", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::DynamoDB::Table", {
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "gsi1",
          KeySchema: [
            { AttributeName: "gsi1pk", KeyType: "HASH" },
            { AttributeName: "gsi1sk", KeyType: "RANGE" },
          ],
          Projection: { ProjectionType: "ALL" },
        }),
        Match.objectLike({
          IndexName: "gsi2",
          KeySchema: [
            { AttributeName: "gsi2pk", KeyType: "HASH" },
            { AttributeName: "gsi2sk", KeyType: "RANGE" },
          ],
          Projection: { ProjectionType: "ALL" },
        }),
      ]),
    });
  });

  test("creates a retained immutable repository that keeps only the newest 5 images", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::ECR::Repository", 1);
    template.hasResource("AWS::ECR::Repository", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        EncryptionConfiguration: Match.absent(),
        ImageScanningConfiguration: { ScanOnPush: false },
        ImageTagMutability: "IMMUTABLE",
        LifecyclePolicy: {
          LifecyclePolicyText: Match.serializedJson(
            Match.objectLike({
              rules: Match.arrayWith([
                Match.objectLike({
                  action: { type: "expire" },
                  selection: Match.objectLike({
                    countNumber: 5,
                    countType: "imageCountMoreThan",
                    tagStatus: "untagged",
                  }),
                }),
                Match.objectLike({
                  action: { type: "expire" },
                  selection: Match.objectLike({
                    countNumber: 5,
                    countType: "imageCountMoreThan",
                    tagPatternList: ["*"],
                    tagStatus: "tagged",
                  }),
                }),
              ]),
            }),
          ),
        },
        RepositoryName: "shittim-chest",
      },
    });
  });

  test("enables registry enhanced scanning only for the application repository", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::ECR::RegistryScanningConfiguration", 1);
    template.hasResourceProperties("AWS::ECR::RegistryScanningConfiguration", {
      ScanType: "ENHANCED",
      Rules: [
        {
          ScanFrequency: "CONTINUOUS_SCAN",
          RepositoryFilters: [
            {
              Filter: "shittim-chest",
              FilterType: "WILDCARD_MATCH",
            },
          ],
        },
      ],
    });
  });

  test("enables managed signing only for the application repository", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::Signer::SigningProfile", 1);
    template.hasResource("AWS::Signer::SigningProfile", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
      Properties: {
        PlatformId: "Notation-OCI-SHA384-ECDSA",
        ProfileName: "shittim_chest_ecr",
        SignatureValidityPeriod: {
          Type: "MONTHS",
          Value: 135,
        },
      },
    });
    template.resourceCountIs("AWS::ECR::SigningConfiguration", 1);
    template.hasResourceProperties("AWS::ECR::SigningConfiguration", {
      Rules: [
        {
          RepositoryFilters: [
            {
              Filter: "shittim-chest",
              FilterType: "WILDCARD_MATCH",
            },
          ],
          SigningProfileArn: Match.anyValue(),
        },
      ],
    });
  });

  test("applies mandatory cost-allocation tags", () => {
    const { template } = synthesize();

    for (const resourceType of ["AWS::DynamoDB::Table", "AWS::ECR::Repository"]) {
      template.hasResourceProperties(resourceType, {
        Tags: Match.arrayWith([
          { Key: "Environment", Value: "production" },
          { Key: "ManagedBy", Value: "cdk" },
          { Key: "Project", Value: "shittim-chest" },
        ]),
      });
    }
  });

  test("has no unsuppressed AWS Solutions findings", () => {
    const { checks, stack } = synthesize();

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
