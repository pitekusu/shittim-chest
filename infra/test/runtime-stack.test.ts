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
    signingProfileArn: stateful.signingProfile.attrArn,
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
      Default: "v0002",
    });
    expect(parameters.LambdaBundleBucketName).toMatchObject({
      AllowedPattern: "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    });
    expect(parameters.LambdaBundleObjectKey).toMatchObject({
      AllowedPattern:
        "^lambda/shittim-chest/[0-9a-f]{64}/shittim-chest-lambda-arm64\\.zip$",
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
    template.resourceCountIs("AWS::DynamoDB::Table", 0);
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

  test("starts at zero on Fargate On-Demand with stop-before-start deployment", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterSettings: [{ Name: "containerInsights", Value: "disabled" }],
    });
    template.hasResourceProperties("AWS::ECS::Service", {
      AvailabilityZoneRebalancing: "DISABLED",
      CapacityProviderStrategy: Match.absent(),
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
        MaximumPercent: 100,
        MinimumHealthyPercent: 0,
      }),
      DesiredCount: 0,
      EnableExecuteCommand: false,
      LaunchType: "FARGATE",
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "ENABLED" }),
      },
      PlatformVersion: "LATEST",
    });
    template.resourceCountIs("AWS::ApplicationAutoScaling::ScalableTarget", 0);
    expect(JSON.stringify(template.toJSON())).not.toContain("FARGATE_SPOT");
  });

  test("creates three control-plane Lambdas plus one image admission Lambda", () => {
    const { template } = synthesize();
    const functions = Object.values(template.findResources("AWS::Lambda::Function"));
    const applicationFunctions = functions.filter((resource) =>
      String(resource.Properties.Handler).startsWith("shittim_chest.lambda_handlers."),
    );
    const deploymentProviders = functions.filter(
      (resource) => resource.Properties.Handler === "__entrypoint__.handler",
    );

    expect(applicationFunctions).toHaveLength(4);
    // CDK's existing VPC default-security-group restriction remains as one
    // deploy-only provider. It is not an application Lambda and removing it
    // would weaken the VPC baseline solely to change a resource count.
    expect(deploymentProviders).toHaveLength(1);
    expect(functions).toHaveLength(5);
    const expected = new Map([
      [
        "shittim_chest.lambda_handlers.discord_ingress.lambda_handler",
        { memory: 512, concurrency: 5, timeout: 5 },
      ],
      [
        "shittim_chest.lambda_handlers.discord_status_publisher.lambda_handler",
        { memory: 256, concurrency: 2, timeout: 120 },
      ],
      [
        "shittim_chest.lambda_handlers.runtime_reconciler.lambda_handler",
        { memory: 256, concurrency: 1, timeout: 55 },
      ],
      [
        "shittim_chest.lambda_handlers.image_admission.lambda_handler",
        { memory: 256, concurrency: 1, timeout: 30 },
      ],
    ]);
    for (const resource of applicationFunctions) {
      const properties = resource.Properties;
      const limits = expected.get(properties.Handler);
      expect(limits).toBeDefined();
      expect(properties).toMatchObject({
        Architectures: ["arm64"],
        Code: {
          S3Bucket: { Ref: "LambdaBundleBucketName" },
          S3Key: { Ref: "LambdaBundleObjectKey" },
        },
        LoggingConfig: expect.objectContaining({ LogFormat: "JSON" }),
        MemorySize: limits?.memory,
        ReservedConcurrentExecutions: limits?.concurrency,
        Runtime: "python3.14",
        Timeout: limits?.timeout,
      });
      expect(properties.VpcConfig).toBeUndefined();
    }
  });

  test("fails closed before scale-up unless the release image is admitted", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: Match.objectLike({
        LifecycleHooks: [
          Match.objectLike({
            HookDetails: JSON.stringify({ schemaVersion: 1 }),
            LifecycleStages: ["PRE_SCALE_UP"],
            TargetType: "AWS_LAMBDA",
            TimeoutConfiguration: {
              Action: "ROLLBACK",
              TimeoutInMinutes: 5,
            },
          }),
        ],
      }),
    });
    const functions = Object.values(template.findResources("AWS::Lambda::Function"));
    const admission = functions.find(
      (resource) =>
        resource.Properties.Handler ===
        "shittim_chest.lambda_handlers.image_admission.lambda_handler",
    );
    expect(admission?.Properties.Environment.Variables).toMatchObject({
      SHITTIM_ECR_REPOSITORY_NAME: expect.anything(),
      SHITTIM_ECR_REPOSITORY_URI: expect.anything(),
      SHITTIM_ECS_SERVICE_ARN: expect.anything(),
      SHITTIM_EXPECTED_CONTAINER_NAME: "application",
      SHITTIM_SIGNING_PROFILE_ARN: expect.anything(),
    });
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const imagePolicy = policies.find((policy) =>
      JSON.stringify(policy.Properties.Roles).includes("ImageAdmissionFunctionRole"),
    );
    const imagePolicyText = JSON.stringify(imagePolicy);
    const describeTaskDefinitionStatements =
      imagePolicy?.Properties.PolicyDocument.Statement.filter(
        (statement: { Action?: string | string[] }) =>
          [statement.Action].flat().includes("ecs:DescribeTaskDefinition"),
      );
    expect(describeTaskDefinitionStatements).toEqual([
      {
        Action: "ecs:DescribeTaskDefinition",
        Effect: "Allow",
        Resource: "*",
      },
    ]);
    expect(imagePolicyText).toContain("ecs:DescribeServiceRevisions");
    expect(imagePolicyText).toContain("ecr:BatchGetImage");
    expect(imagePolicyText).toContain("ecr:DescribeImageSigningStatus");
    expect(imagePolicyText).not.toContain("ecr:ListImageReferrers");
    expect(imagePolicyText).not.toContain("ecr:PutImage");
    expect(JSON.stringify(policies)).toContain("lambda:InvokeFunction");
  });

  test("wires only versioned parameter names and content-free function references", () => {
    const { template } = synthesize();
    const functions = Object.values(template.findResources("AWS::Lambda::Function"));
    const byHandler = (handler: string) =>
      functions.find((resource) => resource.Properties.Handler === handler)?.Properties;
    const ingress = byHandler(
      "shittim_chest.lambda_handlers.discord_ingress.lambda_handler",
    );
    const status = byHandler(
      "shittim_chest.lambda_handlers.discord_status_publisher.lambda_handler",
    );
    const reconciler = byHandler(
      "shittim_chest.lambda_handlers.runtime_reconciler.lambda_handler",
    );

    expect(ingress?.Environment.Variables).toMatchObject({
      SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER:
        "/shittim-chest/production/discord/moderator/public-key",
      SHITTIM_RUNTIME_CONFIG_PARAMETER: expect.anything(),
      SHITTIM_RUNTIME_RECONCILER_FUNCTION: expect.anything(),
      SHITTIM_STATUS_PUBLISHER_FUNCTION: expect.anything(),
    });
    expect(status?.Environment.Variables).toMatchObject({
      SHITTIM_MODERATOR_TOKEN_PARAMETER:
        "/shittim-chest/production/discord/moderator/token",
      SHITTIM_RUNTIME_CONFIG_PARAMETER: expect.anything(),
    });
    expect(reconciler?.Environment.Variables).toMatchObject({
      SHITTIM_ECS_CLUSTER: expect.anything(),
      SHITTIM_ECS_SERVICE: expect.anything(),
      SHITTIM_STATUS_PUBLISHER_FUNCTION: expect.anything(),
    });
    expect(JSON.stringify([ingress, status, reconciler])).not.toContain("participant-");
    expect(JSON.stringify([ingress, status, reconciler])).not.toContain("OPENAI_API_KEY");
  });

  test("exposes only the signed Discord POST route through HTTP API v2", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::ApiGatewayV2::Api", 1);
    template.resourceCountIs("AWS::ApiGatewayV2::Integration", 1);
    template.resourceCountIs("AWS::ApiGatewayV2::Route", 1);
    template.resourceCountIs("AWS::ApiGatewayV2::Stage", 1);
    template.resourceCountIs("AWS::ApiGateway::RestApi", 0);
    template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
      CorsConfiguration: Match.absent(),
      ProtocolType: "HTTP",
    });
    template.hasResourceProperties("AWS::ApiGatewayV2::Integration", {
      IntegrationType: "AWS_PROXY",
      PayloadFormatVersion: "2.0",
    });
    template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
      AuthorizationType: "NONE",
      RouteKey: "POST /interactions",
    });
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      AccessLogSettings: Match.objectLike({
        DestinationArn: Match.anyValue(),
        Format: Match.stringLikeRegexp("requestId"),
      }),
      AutoDeploy: true,
      DefaultRouteSettings: {
        ThrottlingBurstLimit: 5,
        ThrottlingRateLimit: 2,
      },
      StageName: "$default",
    });
    const stage = Object.values(
      template.findResources("AWS::ApiGatewayV2::Stage"),
    )[0];
    const accessLogFormat = stage?.Properties.AccessLogSettings.Format;
    expect(accessLogFormat).not.toContain("body");
    expect(accessLogFormat).not.toContain("header");
    expect(accessLogFormat).not.toContain("identity");
    expect(
      template.toJSON().Outputs.DiscordInteractionsEndpointUrl.Value["Fn::Join"],
    ).toBeDefined();
    expect(JSON.stringify(template.toJSON().Outputs)).toContain("/interactions");
  });

  test("schedules a bounded one-minute reconciliation and async retries", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::Scheduler::Schedule", 1);
    template.hasResourceProperties("AWS::Scheduler::Schedule", {
      FlexibleTimeWindow: { Mode: "OFF" },
      Name: "shittim-chest-production-runtime-reconciler",
      ScheduleExpression: "rate(1 minute)",
      State: "ENABLED",
      Target: Match.objectLike({
        Input: '{"schema_version":1,"trigger":"scheduled"}',
        RetryPolicy: {
          MaximumEventAgeInSeconds: 120,
          MaximumRetryAttempts: 2,
        },
      }),
    });
    template.resourceCountIs("AWS::Lambda::EventInvokeConfig", 2);
    template.hasResourceProperties("AWS::Lambda::EventInvokeConfig", {
      MaximumEventAgeInSeconds: 900,
      MaximumRetryAttempts: 2,
    });
    template.hasResourceProperties("AWS::Lambda::EventInvokeConfig", {
      MaximumEventAgeInSeconds: 120,
      MaximumRetryAttempts: 1,
    });
    const schedulerRole = Object.values(template.findResources("AWS::IAM::Role")).find(
      (resource) => resource.Properties.RoleName === "ShittimChest-Prod-RuntimeReconcilerScheduler",
    );
    const schedulerTrust = JSON.stringify(
      schedulerRole?.Properties.AssumeRolePolicyDocument,
    );
    expect(schedulerTrust).toContain("scheduler.amazonaws.com");
    expect(schedulerTrust).toContain("schedule-group/default");
    expect(schedulerTrust).toContain("aws:SourceAccount");
    const schedulerPolicyEntry = Object.entries(
      template.findResources("AWS::IAM::Policy"),
    ).find(([, resource]) =>
      JSON.stringify(resource.Properties.Roles).includes(
        "RuntimeReconcilerSchedulerRole",
      ),
    );
    const schedulerPolicy = schedulerPolicyEntry?.[1];
    expect(JSON.stringify(schedulerPolicy)).toContain("lambda:InvokeFunction");
    expect(JSON.stringify(schedulerPolicy)).toContain("RuntimeReconcilerFunction");
    expect(JSON.stringify(schedulerPolicy)).not.toContain("Resource\":\"*");
    const schedule = Object.values(
      template.findResources("AWS::Scheduler::Schedule"),
    )[0];
    const invokePolicyLogicalId = schedulerPolicyEntry?.[0];
    expect(invokePolicyLogicalId).toBeDefined();
    expect(schedule?.DependsOn).toContain(invokePolicyLogicalId);
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
        HealthCheck: {
          Command: ["CMD", "python", "-m", "shittim_chest.healthcheck"],
          Interval: 10,
          Retries: 3,
          StartPeriod: 30,
          Timeout: 3,
        },
        LinuxParameters: {
          Capabilities: { Drop: ["ALL"] },
          InitProcessEnabled: true,
          Tmpfs: [
            {
              ContainerPath: "/tmp/shittim-chest",
              MountOptions: [
                "nosuid",
                "nodev",
                "noexec",
                "uid=65532",
                "gid=65532",
                "mode=0700",
              ],
              Size: 1,
            },
          ],
        },
        Privileged: false,
        StopTimeout: 120,
        User: "65532:65532",
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
    expect(JSON.stringify(normal)).toContain("dynamodb:DeleteItem");
    expect(JSON.stringify(normal)).toContain("dynamodb:Query");
    expect(JSON.stringify(normal)).toContain("dynamodb:UpdateItem");
    expect(JSON.stringify(normal)).not.toContain("dynamodb:TransactGetItems");
    expect(JSON.stringify(normal)).not.toContain("dynamodb:TransactWriteItems");
    expect(JSON.stringify(normal)).not.toContain("ssm:");
    expect(JSON.stringify(normal)).not.toContain("ssmmessages:");
    expect(JSON.stringify(breakGlass)).toContain("ssmmessages:OpenControlChannel");
    expect(JSON.stringify(breakGlass)).toContain("logs:PutLogEvents");
  });

  test("grants each application Lambda only its underlying DynamoDB operations", () => {
    const { template } = synthesize();
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const policyFor = (roleId: string) => {
      const policy = policies.find((resource) =>
        JSON.stringify(resource.Properties.Roles).includes(roleId),
      );
      expect(policy).toBeDefined();
      return JSON.stringify(policy);
    };
    const ingress = policyFor("DiscordIngressFunctionRole");
    const status = policyFor("DiscordStatusPublisherFunctionRole");
    const reconciler = policyFor("RuntimeReconcilerFunctionRole");

    for (const action of [
      "dynamodb:ConditionCheckItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]) {
      expect(ingress).toContain(action);
    }
    for (const action of ["dynamodb:DeleteItem", "dynamodb:Query"]) {
      expect(ingress).not.toContain(action);
    }
    for (const action of [
      "dynamodb:ConditionCheckItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]) {
      expect(status).toContain(action);
    }
    for (const action of ["dynamodb:DeleteItem", "dynamodb:Query"]) {
      expect(status).not.toContain(action);
    }
    for (const action of [
      "dynamodb:ConditionCheckItem",
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
    ]) {
      expect(reconciler).toContain(action);
    }
    expect(reconciler).toContain("/index/gsi1");
    expect(reconciler).toContain("/index/gsi2");
    expect(ingress).toContain("discord/moderator/public-key");
    expect(ingress).not.toContain("discord/moderator/token");
    expect(status).toContain("discord/moderator/token");
    expect(status).not.toContain("discord/moderator/public-key");
    expect(reconciler).not.toContain("ssm:GetParameter");
    for (const policy of [ingress, status, reconciler]) {
      expect(policy).not.toContain("dynamodb:TransactGetItems");
      expect(policy).not.toContain("dynamodb:TransactWriteItems");
    }
  });

  test("reserves deployment lock and audit writes for deployment tooling", () => {
    const { template } = synthesize();
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const expectedPartitionsByRole = new Map([
      [
        "DiscordIngressFunctionRole",
        [
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
        ],
      ],
      [
        "DiscordStatusPublisherFunctionRole",
        ["CONTROL#INGRESS", "INGRESS_OPERATION#*"],
      ],
      [
        "RuntimeReconcilerFunctionRole",
        [
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#RUNTIME",
          "INGRESS_OPERATION#*",
        ],
      ],
      [
        "NormalTaskRole",
        [
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
          "OPERATION#*",
          "QUOTA#GUILD#*",
        ],
      ],
      [
        "BreakGlassTaskRole",
        [
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
          "OPERATION#*",
          "QUOTA#GUILD#*",
        ],
      ],
    ]);
    const expectedReadsByRole = new Map([
      [
        "DiscordIngressFunctionRole",
        [
          "CONTROL#INGRESS",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
        ],
      ],
      [
        "DiscordStatusPublisherFunctionRole",
        ["CONTROL#INGRESS", "INGRESS_OPERATION#*"],
      ],
      [
        "RuntimeReconcilerFunctionRole",
        [
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "INGRESS_OPERATION#*",
        ],
      ],
      [
        "NormalTaskRole",
        [
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
          "OPERATION#*",
          "QUOTA#GUILD#*",
        ],
      ],
      [
        "BreakGlassTaskRole",
        [
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
          "OPERATION#*",
          "QUOTA#GUILD#*",
        ],
      ],
    ]);
    const expectedConditionChecksByRole = new Map([
      [
        "DiscordIngressFunctionRole",
        ["CONTROL#DEPLOYMENT", "CONTROL#INGRESS"],
      ],
      [
        "DiscordStatusPublisherFunctionRole",
        ["CONTROL#DEPLOYMENT", "CONTROL#INGRESS"],
      ],
      [
        "RuntimeReconcilerFunctionRole",
        [
          "CONTROL#DEPLOYMENT",
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "INGRESS_OPERATION#*",
        ],
      ],
      [
        "NormalTaskRole",
        [
          "CONTROL#DEPLOYMENT",
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
          "OPERATION#*",
          "QUOTA#GUILD#*",
        ],
      ],
      [
        "BreakGlassTaskRole",
        [
          "CONTROL#DEPLOYMENT",
          "CONTROL#DEBATE",
          "CONTROL#GLOBAL",
          "CONTROL#INGRESS",
          "CONTROL#INGRESS#ACTIVE",
          "CONTROL#OUTBOX",
          "CONTROL#PANEL_REFRESH",
          "CONTROL#RUNTIME",
          "DEBATE#*",
          "INGRESS_OPERATION#*",
          "INGRESS_SEMANTIC_OPERATION#*",
          "OPERATION#*",
          "QUOTA#GUILD#*",
        ],
      ],
    ]);
    const writeActions = new Set([
      "dynamodb:DeleteItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]);
    const queryRoleIds = new Set([
      "RuntimeReconcilerFunctionRole",
      "NormalTaskRole",
      "BreakGlassTaskRole",
    ]);
    for (const [roleId, expectedPartitionPatterns] of expectedPartitionsByRole) {
      const policy = policies.find((resource) =>
        JSON.stringify(resource.Properties.Roles).includes(roleId),
      );
      expect(policy).toBeDefined();
      const statements = policy?.Properties.PolicyDocument.Statement as Array<{
        Action: string | string[];
        Condition?: Record<string, unknown>;
        Resource: unknown;
      }>;
      const actionList = (statement: { Action: string | string[] }) =>
        Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      const writeStatements = statements.filter((statement) =>
        actionList(statement).some((action) => writeActions.has(action)),
      );
      expect(writeStatements).toHaveLength(1);
      expect(writeStatements[0]?.Condition).toEqual({
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": expectedPartitionPatterns,
        },
        Null: { "dynamodb:LeadingKeys": "false" },
      });
      expect(actionList(writeStatements[0]!)).not.toContain("dynamodb:ConditionCheckItem");
      const getItem = statements.find((statement) =>
        actionList(statement).includes("dynamodb:GetItem"),
      );
      expect(getItem?.Condition).toEqual({
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": expectedReadsByRole.get(roleId),
        },
        Null: { "dynamodb:LeadingKeys": "false" },
      });
      const conditionCheck = statements.find((statement) =>
        actionList(statement).includes("dynamodb:ConditionCheckItem"),
      );
      expect(conditionCheck?.Condition).toEqual({
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": expectedConditionChecksByRole.get(roleId),
        },
        Null: { "dynamodb:LeadingKeys": "false" },
      });
      const queryStatements = statements.filter((statement) =>
        actionList(statement).includes("dynamodb:Query"),
      );
      if (queryRoleIds.has(roleId)) {
        expect(queryStatements).toHaveLength(2);
        const baseTableQuery = queryStatements.find(
          (statement) => !JSON.stringify(statement.Resource).includes("/index/"),
        );
        const indexQuery = queryStatements.find((statement) =>
          JSON.stringify(statement.Resource).includes("/index/"),
        );
        expect(baseTableQuery?.Condition).toEqual({
          "ForAllValues:StringLike": {
            "dynamodb:LeadingKeys": expectedReadsByRole.get(roleId),
          },
          Null: { "dynamodb:LeadingKeys": "false" },
        });
        expect(JSON.stringify(baseTableQuery)).not.toContain("CONTROL#DEPLOYMENT");
        expect(indexQuery?.Condition).toBeUndefined();
        expect(JSON.stringify(indexQuery?.Resource)).toContain("/index/gsi1");
        expect(JSON.stringify(indexQuery?.Resource)).toContain("/index/gsi2");
      } else {
        expect(queryStatements).toHaveLength(0);
      }
      expect(JSON.stringify(writeStatements)).not.toContain("CONTROL#DEPLOYMENT");
    }
  });

  test("retains logs after normal deletion but removes them after a failed first create", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::Logs::LogGroup", 7);
    for (const suffix of ["application", "break-glass-exec"]) {
      template.hasResource("AWS::Logs::LogGroup", {
        DeletionPolicy: "RetainExceptOnCreate",
        UpdateReplacePolicy: "Retain",
        Properties: {
          DataProtectionPolicy: Match.anyValue(),
          LogGroupName: `/ecs/shittim-chest/production/${suffix}`,
          RetentionInDays: 90,
        },
      });
    }
    for (const resource of Object.values(
      template.findResources("AWS::Logs::LogGroup"),
    )) {
      expect(resource.DeletionPolicy).toBe("RetainExceptOnCreate");
      expect(resource.UpdateReplacePolicy).toBe("Retain");
      expect(resource.Properties.DataProtectionPolicy).toBeDefined();
      expect(resource.Properties.RetentionInDays).toBe(90);
    }
  });

  test("has no unsuppressed AWS Solutions findings", () => {
    const { checks, runtime } = synthesize();

    expect(checks.validateScope(runtime).success).toBe(true);
  });
});
