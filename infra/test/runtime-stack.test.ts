import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { RuntimeStack } from "../lib/runtime-stack";
import { StatefulStack } from "../lib/stateful-stack";

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly runtime: RuntimeStack;
  readonly template: Template;
} {
  const app = new App();
  const stateful = new StatefulStack(app, "Stateful", {
    env: { account: "000000000000", region: "ap-northeast-1" },
    stackName: "ShittimChest-Prod-Stateful",
    terminationProtection: true,
  });
  const runtime = new RuntimeStack(app, "Runtime", {
    debateTable: stateful.debateTable,
    env: { account: "000000000000", region: "ap-northeast-1" },
    imageRepository: stateful.imageRepository,
    stackName: "ShittimChest-Prod-Runtime",
  });
  runtime.addDependency(stateful);
  for (const stack of [stateful, runtime]) {
    Tags.of(stack).add("Project", "shittim-chest");
    Tags.of(stack).add("Environment", "production");
    Tags.of(stack).add("ManagedBy", "cdk");
  }
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { checks, runtime, template: Template.fromStack(runtime) };
}

describe("RuntimeStack", () => {
  test("requires validated image digests and accepts only versioned runtime config", () => {
    const { template } = synthesize();
    const parameters = template.toJSON().Parameters;

    expect(parameters.RuntimeImageDigest).toEqual({
      AllowedPattern: "^sha256:[0-9a-f]{64}$",
      Description: "Approved production image manifest digest",
      Type: "String",
    });
    expect(parameters.BreakGlassImageDigest).toEqual({
      AllowedPattern: "^sha256:[0-9a-f]{64}$",
      Description: "Approved break-glass image manifest digest",
      Type: "String",
    });
    expect(parameters.RuntimeConfigVersion).toMatchObject({
      AllowedPattern: "^v[0-9]{4}$",
      Default: "v0001",
    });
  });

  test("creates a two-AZ public-only VPC without paid network appliances", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::EC2::VPC", 1);
    template.hasResourceProperties("AWS::EC2::VPC", { CidrBlock: "10.42.0.0/24" });
    template.resourceCountIs("AWS::EC2::Subnet", 2);
    template.resourceCountIs("AWS::EC2::InternetGateway", 1);
    template.resourceCountIs("AWS::EC2::NatGateway", 0);
    template.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 0);
    template.resourceCountIs("AWS::EC2::VPCEndpoint", 0);
  });

  test("allows no ingress and only IPv4 HTTPS egress", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::EC2::SecurityGroupIngress", 0);
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      GroupDescription: "No ingress; HTTPS-only egress for the Discord debate task",
      SecurityGroupEgress: [
        {
          CidrIp: "0.0.0.0/0",
          FromPort: 443,
          IpProtocol: "tcp",
          ToPort: 443,
        },
      ],
    });
  });

  test("runs one ARM64 task only on Fargate Spot with stop-before-start deployment", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterSettings: [{ Name: "containerInsights", Value: "disabled" }],
    });
    template.hasResourceProperties("AWS::ECS::Service", {
      AvailabilityZoneRebalancing: "DISABLED",
      CapacityProviderStrategy: [{ CapacityProvider: "FARGATE_SPOT", Weight: 1 }],
      DeploymentConfiguration: {
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
        MaximumPercent: 100,
        MinimumHealthyPercent: 0,
      },
      DesiredCount: 1,
      EnableExecuteCommand: false,
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "ENABLED" }),
      },
      PlatformVersion: "LATEST",
    });
  });

  test("uses digest-only images and hardened normal and break-glass task definitions", () => {
    const { template } = synthesize();

    const taskDefinitions = Object.values(template.findResources("AWS::ECS::TaskDefinition"));
    expect(taskDefinitions).toHaveLength(2);
    for (const task of taskDefinitions) {
      const properties = task.Properties as Record<string, unknown>;
      expect(properties).toMatchObject({
        Cpu: "512",
        Memory: "1024",
        NetworkMode: "awsvpc",
        RequiresCompatibilities: ["FARGATE"],
        RuntimePlatform: { CpuArchitecture: "ARM64", OperatingSystemFamily: "LINUX" },
      });
      expect(properties.Volumes ?? []).toEqual([]);
      const container = (properties.ContainerDefinitions as Array<Record<string, unknown>>)[0]!;
      expect(JSON.stringify(container.Image)).toContain("@");
      expect(container).toMatchObject({
        LinuxParameters: {
          Capabilities: { Drop: ["ALL"] },
          InitProcessEnabled: true,
          Tmpfs: [
            {
              ContainerPath: "/tmp/shittim-chest",
              MountOptions: ["nosuid", "nodev", "noexec", "uid=10001", "gid=10001", "mode=0700"],
              Size: 1,
            },
          ],
        },
        Privileged: false,
        StopTimeout: 120,
        User: "10001:10001",
        VersionConsistency: "enabled",
        WorkingDirectory: "/app",
      });
    }

    const normal = taskDefinitions.find((task) => task.Properties.Family.endsWith("-normal"));
    const breakGlass = taskDefinitions.find((task) =>
      task.Properties.Family.endsWith("-break-glass"),
    );
    expect(normal?.Properties.ContainerDefinitions[0].ReadonlyRootFilesystem).toBe(true);
    expect(breakGlass?.Properties.ContainerDefinitions[0].ReadonlyRootFilesystem).toBe(false);
  });

  test("injects private runtime values from versioned SSM paths", () => {
    const { template } = synthesize();

    const normal = Object.values(template.findResources("AWS::ECS::TaskDefinition")).find(
      (task) => task.Properties.Family.endsWith("-normal"),
    );
    expect(normal).toBeDefined();
    const secrets = normal?.Properties.ContainerDefinitions[0].Secrets as Array<{
      Name: string;
      ValueFrom: unknown;
    }>;
    expect(secrets.map((secret) => secret.Name).sort()).toEqual([
      "DISCORD_TOKEN_MODERATOR",
      "DISCORD_TOKEN_PARTICIPANT_A",
      "DISCORD_TOKEN_PARTICIPANT_B",
      "DISCORD_TOKEN_PARTICIPANT_C",
      "OPENAI_API_KEY",
      "SHITTIM_PERSONA_MODERATOR_JSON",
      "SHITTIM_PERSONA_PARTICIPANT_A_JSON",
      "SHITTIM_PERSONA_PARTICIPANT_B_JSON",
      "SHITTIM_PERSONA_PARTICIPANT_C_JSON",
      "SHITTIM_RUNTIME_CONFIG_JSON",
    ]);
    expect(JSON.stringify(secrets)).toContain("/shittim-chest/production/runtime/");
    expect(JSON.stringify(secrets)).toContain("RuntimeConfigVersion");
  });

  test("keeps normal task permissions bounded and break-glass access isolated", () => {
    const { template } = synthesize();
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const execution = policies.find((policy) =>
      JSON.stringify(policy.Properties.Roles).includes("ExecutionRole"),
    );
    const normal = policies.find((policy) =>
      JSON.stringify(policy.Properties.Roles).includes("NormalTaskRole"),
    );
    const breakGlass = policies.find((policy) =>
      JSON.stringify(policy.Properties.Roles).includes("BreakGlassTaskRole"),
    );

    expect(JSON.stringify(execution)).toContain("ssm:GetParameters");
    expect(JSON.stringify(execution)).not.toContain("ssm:GetParameterHistory");
    expect(JSON.stringify(execution)).not.toContain("ssm:DescribeParameters");
    expect(JSON.stringify(normal)).toContain("dynamodb:ConditionCheckItem");
    expect(JSON.stringify(normal)).toContain("dynamodb:Query");
    expect(JSON.stringify(normal)).not.toContain("ssm:");
    expect(JSON.stringify(normal)).not.toContain("ssmmessages:");
    expect(JSON.stringify(breakGlass)).toContain("ssmmessages:OpenControlChannel");
    expect(JSON.stringify(breakGlass)).toContain("logs:PutLogEvents");
  });

  test("retains protected application and break-glass log groups for 90 days", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::Logs::LogGroup", 2);
    for (const suffix of ["application", "break-glass-exec"]) {
      template.hasResource("AWS::Logs::LogGroup", {
        DeletionPolicy: "Retain",
        UpdateReplacePolicy: "Retain",
        Properties: {
          DataProtectionPolicy: Match.anyValue(),
          LogGroupName: `/ecs/shittim-chest/production/${suffix}`,
          RetentionInDays: 90,
        },
      });
    }
  });

  test("has no unsuppressed AWS Solutions findings", () => {
    const { checks, runtime } = synthesize();

    expect(checks.validateScope(runtime).success).toBe(true);
  });
});
