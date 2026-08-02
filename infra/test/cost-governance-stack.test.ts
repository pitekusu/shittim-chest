import { App, Tags, Validations } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { CostGovernanceStack } from "../lib/cost-governance-stack";

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly stack: CostGovernanceStack;
  readonly template: Template;
} {
  const app = new App();
  const stack = new CostGovernanceStack(app, "CostGovernance", {
    env: { account: "000000000000", region: "us-east-1" },
    stackName: "ShittimChest-Prod-CostGovernance",
  });
  Tags.of(stack).add("Project", "shittim-chest");
  Tags.of(stack).add("Environment", "production");
  Tags.of(stack).add("ManagedBy", "cdk");
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { checks, stack, template: Template.fromStack(stack) };
}

describe("CostGovernanceStack", () => {
  test("requires private email and an existing service monitor ARN", () => {
    const { template } = synthesize();
    const parameters = template.toJSON().Parameters;

    expect(parameters.OperatorNotificationEmail).toMatchObject({
      NoEcho: true,
      Type: "String",
    });
    expect(parameters.OperatorNotificationEmail.Default).toBeUndefined();
    expect(parameters.ExistingServiceAnomalyMonitorArn).toMatchObject({
      AllowedPattern:
        "^arn:(aws|aws-us-gov|aws-cn|aws-iso|aws-iso-b):ce::[0-9]{12}:anomalymonitor/[A-Za-z0-9-]+$",
      Type: "String",
    });
    expect(parameters.ExistingServiceAnomalyMonitorArn.Default).toBeUndefined();
    expect(JSON.stringify(template.toJSON())).not.toContain("@example.com");
  });

  test("creates a 20 USD project budget using the modern tag filter", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::Budgets::Budget", {
      Budget: {
        BudgetLimit: { Amount: 20, Unit: "USD" },
        BudgetName: "shittim-chest-production-project",
        BudgetType: "COST",
        FilterExpression: {
          Tags: {
            Key: "user:Project",
            MatchOptions: ["EQUALS"],
            Values: ["shittim-chest"],
          },
        },
        Metrics: ["NetUnblendedCost"],
        TimeUnit: "MONTHLY",
      },
    });
  });

  test("creates an unfiltered 30 USD account budget", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::Budgets::Budget", {
      Budget: {
        BudgetLimit: { Amount: 30, Unit: "USD" },
        BudgetName: "shittim-chest-production-account",
        BudgetType: "COST",
        Metrics: ["NetUnblendedCost"],
        TimeUnit: "MONTHLY",
      },
    });
    const accountBudget = Object.values(
      template.findResources("AWS::Budgets::Budget"),
    ).find(
      (resource) =>
        resource.Properties.Budget.BudgetName ===
        "shittim-chest-production-account",
    );
    expect(accountBudget?.Properties.Budget.FilterExpression).toBeUndefined();
  });

  test("sends three percentage notifications for each budget to one parameter", () => {
    const { template } = synthesize();
    const resources = Object.values(
      template.findResources("AWS::Budgets::Budget"),
    );

    expect(resources).toHaveLength(2);
    for (const resource of resources) {
      const configured = resource.Properties.NotificationsWithSubscribers;
      expect(
        configured.map((item: Record<string, unknown>) => item.Notification),
      ).toEqual([
        expect.objectContaining({ NotificationType: "ACTUAL", Threshold: 80 }),
        expect.objectContaining({ NotificationType: "ACTUAL", Threshold: 100 }),
        expect.objectContaining({
          NotificationType: "FORECASTED",
          Threshold: 100,
        }),
      ]);
      for (const item of configured) {
        expect(item.Subscribers).toEqual([
          {
            Address: { Ref: "OperatorNotificationEmail" },
            SubscriptionType: "EMAIL",
          },
        ]);
      }
    }
    template.resourceCountIs("AWS::Budgets::BudgetsAction", 0);
  });

  test("reuses the existing service monitor for a daily 10 USD anomaly threshold", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::CE::AnomalyMonitor", 0);
    template.resourceCountIs("AWS::CE::AnomalySubscription", 1);
    template.hasResourceProperties("AWS::CE::AnomalySubscription", {
      Frequency: "DAILY",
      MonitorArnList: [{ Ref: "ExistingServiceAnomalyMonitorArn" }],
      Subscribers: [
        {
          Address: { Ref: "OperatorNotificationEmail" },
          Type: "EMAIL",
        },
      ],
      SubscriptionName: "shittim-chest-production-cost-anomalies",
      ThresholdExpression: JSON.stringify({
        Dimensions: {
          Key: "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
          MatchOptions: ["GREATER_THAN_OR_EQUAL"],
          Values: ["10"],
        },
      }),
    });
  });

  test("contains no secret, automation action, or unrelated helper resource", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::SecretsManager::Secret", 0);
    template.resourceCountIs("AWS::SNS::Topic", 0);
    template.resourceCountIs("AWS::Lambda::Function", 0);
    template.resourceCountIs("AWS::IAM::Role", 0);
  });

  test("tags every cost-governance resource for ownership", () => {
    const { template } = synthesize();
    const expectedTags = [
      { Key: "Environment", Value: "production" },
      { Key: "ManagedBy", Value: "cdk" },
      { Key: "Project", Value: "shittim-chest" },
    ];

    for (const resourceType of [
      "AWS::Budgets::Budget",
      "AWS::CE::AnomalySubscription",
    ]) {
      for (const resource of Object.values(template.findResources(resourceType))) {
        const tags = resource.Properties.ResourceTags;
        expect(tags).toEqual(expectedTags);
      }
    }
  });

  test("passes cdk-nag validations", () => {
    const { checks, stack } = synthesize();

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
