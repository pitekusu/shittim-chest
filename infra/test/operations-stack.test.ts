import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { OperationsStack } from "../lib/operations-stack";
import { RuntimeStack } from "../lib/runtime-stack";
import { StatefulStack } from "../lib/stateful-stack";

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly operations: OperationsStack;
  readonly template: Template;
} {
  const app = new App();
  const env = { account: "000000000000", region: "ap-northeast-1" };
  const stateful = new StatefulStack(app, "Stateful", {
    env,
    stackName: "ShittimChest-Prod-Stateful",
    terminationProtection: true,
  });
  const runtime = new RuntimeStack(app, "Runtime", {
    debateTable: stateful.debateTable,
    env,
    imageRepository: stateful.imageRepository,
    stackName: "ShittimChest-Prod-Runtime",
  });
  runtime.addDependency(stateful);
  const operations = new OperationsStack(app, "Operations", {
    cluster: runtime.cluster,
    debateTable: stateful.debateTable,
    env,
    service: runtime.service,
    stackName: "ShittimChest-Prod-Operations",
  });
  operations.addDependency(runtime);
  for (const stack of [stateful, runtime, operations]) {
    Tags.of(stack).add("Project", "shittim-chest");
    Tags.of(stack).add("Environment", "production");
    Tags.of(stack).add("ManagedBy", "cdk");
  }
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { checks, operations, template: Template.fromStack(operations) };
}

describe("OperationsStack", () => {
  test("requires a private validated operator email without committing an address", () => {
    const { template } = synthesize();
    const parameter = template.toJSON().Parameters.OperatorNotificationEmail;

    expect(parameter).toMatchObject({
      AllowedPattern:
        "^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\\.[A-Za-z0-9-]+)+$",
      NoEcho: true,
      Type: "String",
    });
    expect(parameter.Default).toBeUndefined();
    expect(JSON.stringify(template.toJSON())).not.toContain("@example.com");
  });

  test("uses one TLS-only topic and one confirmable email subscription", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::SNS::Topic", 1);
    template.resourceCountIs("AWS::SNS::Subscription", 1);
    template.hasResourceProperties("AWS::SNS::Subscription", {
      Endpoint: { Ref: "OperatorNotificationEmail" },
      Protocol: "email",
    });
    template.hasResourceProperties("AWS::SNS::TopicPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "sns:Publish",
            Condition: { Bool: { "aws:SecureTransport": "false" } },
            Effect: "Deny",
          }),
        ]),
      }),
    });
  });

  test("creates bounded M-of-N alarms with explicit missing-data behavior", () => {
    const { template } = synthesize();
    const alarms = Object.values(template.findResources("AWS::CloudWatch::Alarm"));

    expect(alarms).toHaveLength(9);
    for (const alarm of alarms) {
      expect(alarm.Properties.ActionsEnabled).toBe(false);
      expect(alarm.Properties.Period ?? alarm.Properties.Metrics?.[1]?.MetricStat.Period).toBe(
        60,
      );
      expect(alarm.Properties.TreatMissingData).toMatch(/^(notBreaching|breaching|ignore)$/);
    }
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "shittim-chest-production-bot-not-ready",
      ComparisonOperator: "LessThanThreshold",
      DatapointsToAlarm: 2,
      EvaluationPeriods: 3,
      MetricName: "BotReady",
      Namespace: "ShittimChest/Prod",
      Threshold: 1,
      TreatMissingData: "breaching",
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "shittim-chest-production-idle-still-running",
      DatapointsToAlarm: 35,
      EvaluationPeriods: 35,
      TreatMissingData: "notBreaching",
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "shittim-chest-production-outbox-backlog",
      DatapointsToAlarm: 10,
      EvaluationPeriods: 10,
      TreatMissingData: "breaching",
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "shittim-chest-production-dynamo-db-throttle",
      TreatMissingData: "ignore",
    });
  });

  test("notifies only through critical and warning composite alarms", () => {
    const { template } = synthesize();
    const composites = Object.values(
      template.findResources("AWS::CloudWatch::CompositeAlarm"),
    );

    expect(composites).toHaveLength(2);
    const actionable = composites.filter(
      (alarm) => alarm.Properties.AlarmActions !== undefined,
    );
    expect(actionable).toHaveLength(2);
    expect(actionable.map((alarm) => alarm.Properties.AlarmName).sort()).toEqual([
      "shittim-chest-production-critical",
      "shittim-chest-production-warning",
    ]);
  });

  test("filters abnormal ECS task stops and sends only bounded lifecycle fields", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::Events::Rule", 1);
    template.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: {
        detail: Match.objectLike({
          lastStatus: ["STOPPED"],
          stopCode: [
            "TaskFailedToStart",
            "EssentialContainerExited",
            "SpotInterruption",
            "TerminationNotice",
          ],
        }),
        "detail-type": ["ECS Task State Change"],
        source: ["aws.ecs"],
      },
      State: "ENABLED",
      Targets: [
        Match.objectLike({
          InputTransformer: Match.objectLike({
            InputTemplate: Match.stringLikeRegexp("ecs_task_stopped_abnormally"),
          }),
        }),
      ],
    });
    const body = JSON.stringify(template.toJSON());
    expect(body).not.toContain('"UserInitiated"');
    expect(body).not.toContain('"ServiceSchedulerInitiated"');
  });

  test("builds one low-cost dashboard without Container Insights or helper compute", () => {
    const { template } = synthesize();
    const body = JSON.stringify(template.toJSON().Resources);

    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
    template.resourceCountIs("AWS::Lambda::Function", 0);
    template.resourceCountIs("AWS::Logs::LogGroup", 0);
    template.resourceCountIs("AWS::KMS::Key", 0);
    expect(body).toContain("ShittimChest/Prod");
    expect(body).toContain("AWS/ECS");
    expect(body).toContain("CPUUtilization");
    expect(body).toContain("MemoryUtilization");
    expect(body).toContain("AWS/DynamoDB");
    expect(body).not.toContain("ContainerInsights");
  });

  test("passes cdk-nag validations with only the documented SNS exception", () => {
    const { checks, operations } = synthesize();

    expect(checks.validateScope(operations).success).toBe(true);
  });
});
