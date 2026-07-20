import {
  Aws,
  CfnParameter,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Validations,
  aws_dynamodb as dynamodb,
  aws_ec2 as ec2,
  aws_ecr as ecr,
  aws_ecs as ecs,
  aws_iam as iam,
  aws_logs as logs,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const IMAGE_DIGEST_PATTERN = "^sha256:[0-9a-f]{64}$";
const CONFIG_VERSION_PATTERN = "^v[0-9]{4}$";
const PARAMETER_ROOT = "/shittim-chest/production";

export interface RuntimeStackProps extends StackProps {
  readonly debateTable: dynamodb.ITable;
  readonly imageRepository: ecr.IRepository;
}

interface RuntimeParameters {
  readonly secrets: Record<string, ecs.Secret>;
}

export class RuntimeStack extends Stack {
  public readonly applicationLogGroup: logs.LogGroup;
  public readonly breakGlassLogGroup: logs.LogGroup;
  public readonly breakGlassTaskDefinition: ecs.FargateTaskDefinition;
  public readonly cluster: ecs.Cluster;
  public readonly normalTaskDefinition: ecs.FargateTaskDefinition;
  public readonly service: ecs.FargateService;
  public readonly taskSecurityGroup: ec2.SecurityGroup;
  public readonly vpc: ec2.Vpc;

  public constructor(scope: Construct, id: string, props: RuntimeStackProps) {
    super(scope, id, props);

    const runtimeImageDigest = this.imageDigestParameter(
      "RuntimeImageDigest",
      "Approved production image manifest digest",
    );
    const breakGlassImageDigest = this.imageDigestParameter(
      "BreakGlassImageDigest",
      "Approved break-glass image manifest digest",
    );
    const configVersion = new CfnParameter(this, "RuntimeConfigVersion", {
      allowedPattern: CONFIG_VERSION_PATTERN,
      default: "v0001",
      description: "Versioned private runtime and persona configuration path",
      type: "String",
    });

    const dataProtectionPolicy = new logs.DataProtectionPolicy({
      name: "shittim-chest-production-log-protection",
      description: "Mask common credentials and identifiers in production logs",
      identifiers: [
        logs.DataIdentifier.AWSSECRETKEY,
        logs.DataIdentifier.EMAILADDRESS,
        logs.DataIdentifier.IPADDRESS,
        logs.DataIdentifier.OPENSSHPRIVATEKEY,
        logs.DataIdentifier.PGPPRIVATEKEY,
        logs.DataIdentifier.PKCSPRIVATEKEY,
      ],
    });
    this.applicationLogGroup = new logs.LogGroup(this, "ApplicationLogGroup", {
      dataProtectionPolicy,
      logGroupName: "/ecs/shittim-chest/production/application",
      removalPolicy: RemovalPolicy.RETAIN,
      retention: logs.RetentionDays.THREE_MONTHS,
    });
    this.breakGlassLogGroup = new logs.LogGroup(this, "BreakGlassExecLogGroup", {
      dataProtectionPolicy,
      logGroupName: "/ecs/shittim-chest/production/break-glass-exec",
      removalPolicy: RemovalPolicy.RETAIN,
      retention: logs.RetentionDays.THREE_MONTHS,
    });

    this.vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr("10.42.0.0/24"),
      maxAzs: 2,
      natGateways: 0,
      restrictDefaultSecurityGroup: true,
      subnetConfiguration: [
        {
          cidrMask: 26,
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
        },
      ],
      vpcName: "shittim-chest-production",
    });
    Validations.of(this.vpc).acknowledge({
      id: "AwsSolutions-VPC7",
      reason:
        "The cost-minimized singleton MVP has no ingress and HTTPS-only egress; VPC Flow Logs are intentionally deferred unless incident evidence shows they are necessary.",
    });
    this.taskSecurityGroup = new ec2.SecurityGroup(this, "TaskSecurityGroup", {
      allowAllIpv6Outbound: false,
      allowAllOutbound: false,
      description: "No ingress; HTTPS-only egress for the Discord debate task",
      securityGroupName: "shittim-chest-production-task",
      vpc: this.vpc,
    });
    this.taskSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "Discord, OpenAI, and AWS HTTPS endpoints",
    );

    this.cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: "shittim-chest-production",
      containerInsightsV2: ecs.ContainerInsights.DISABLED,
      executeCommandConfiguration: {
        logConfiguration: {
          cloudWatchEncryptionEnabled: false,
          cloudWatchLogGroup: this.breakGlassLogGroup,
        },
        logging: ecs.ExecuteCommandLogging.OVERRIDE,
      },
      vpc: this.vpc,
    });
    this.cluster.enableFargateCapacityProviders();
    Validations.of(this.cluster).acknowledge({
      id: "AwsSolutions-ECS4",
      reason:
        "Container Insights is intentionally disabled to avoid its per-observation cost; STEP-09C uses standard ECS metrics and a bounded application metric set.",
    });

    const executionRole = this.executionRole();
    const normalTaskRole = this.taskRole("NormalTaskRole", "ShittimChest-Prod-Task");
    const breakGlassTaskRole = this.taskRole(
      "BreakGlassTaskRole",
      "ShittimChest-Prod-BreakGlassTask",
    );
    this.grantApplicationData(normalTaskRole, props.debateTable);
    this.grantApplicationData(breakGlassTaskRole, props.debateTable);
    this.grantBreakGlassAccess(breakGlassTaskRole);

    const parameters = this.runtimeParameters(configVersion.valueAsString);
    const logging = ecs.LogDrivers.awsLogs({
      logGroup: this.applicationLogGroup,
      mode: ecs.AwsLogDriverMode.BLOCKING,
      streamPrefix: "application",
    });

    this.normalTaskDefinition = this.taskDefinition({
      containerName: "application",
      digest: runtimeImageDigest.valueAsString,
      executionRole,
      imageRepository: props.imageRepository,
      logging,
      parameters,
      readonlyRootFilesystem: true,
      taskId: "NormalTaskDefinition",
      taskRole: normalTaskRole,
    });
    this.acknowledgeStaticEnvironment(this.normalTaskDefinition);
    this.breakGlassTaskDefinition = this.taskDefinition({
      containerName: "break-glass-application",
      digest: breakGlassImageDigest.valueAsString,
      executionRole,
      imageRepository: props.imageRepository,
      logging,
      parameters,
      readonlyRootFilesystem: false,
      taskId: "BreakGlassTaskDefinition",
      taskRole: breakGlassTaskRole,
    });
    this.acknowledgeStaticEnvironment(this.breakGlassTaskDefinition);

    this.service = new ecs.FargateService(this, "Service", {
      assignPublicIp: true,
      availabilityZoneRebalancing: ecs.AvailabilityZoneRebalancing.DISABLED,
      capacityProviderStrategies: [{ capacityProvider: "FARGATE_SPOT", weight: 1 }],
      circuitBreaker: { rollback: true },
      cluster: this.cluster,
      desiredCount: 1,
      enableECSManagedTags: true,
      enableExecuteCommand: false,
      maxHealthyPercent: 100,
      minHealthyPercent: 0,
      platformVersion: ecs.FargatePlatformVersion.LATEST,
      propagateTags: ecs.PropagatedTagSource.SERVICE,
      securityGroups: [this.taskSecurityGroup],
      serviceName: "shittim-chest-production",
      taskDefinition: this.normalTaskDefinition,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });
  }

  private imageDigestParameter(
    id: string,
    description: string,
  ): CfnParameter {
    return new CfnParameter(this, id, {
      allowedPattern: IMAGE_DIGEST_PATTERN,
      description,
      type: "String",
    });
  }

  private executionRole(): iam.Role {
    const role = new iam.Role(this, "ExecutionRole", {
      assumedBy: this.ecsTaskPrincipal(),
      description: "ECS agent access to approved image, logs, and injected SSM parameters",
      roleName: "ShittimChest-Prod-Execution",
    });
    Validations.of(role).acknowledge({
      id: "AwsSolutions-IAM5[Resource::*]",
      reason:
        "ecr:GetAuthorizationToken has no resource-level ARN and is the only unscoped normal execution action.",
    });
    return role;
  }

  private taskRole(id: string, roleName: string): iam.Role {
    return new iam.Role(this, id, {
      assumedBy: this.ecsTaskPrincipal(),
      description: "Least-privilege application access for one Shittim Chest task mode",
      roleName,
    });
  }

  private ecsTaskPrincipal(): iam.ServicePrincipal {
    return new iam.ServicePrincipal("ecs-tasks.amazonaws.com", {
      conditions: {
        ArnLike: {
          "aws:SourceArn": `arn:${Aws.PARTITION}:ecs:${Aws.REGION}:${Aws.ACCOUNT_ID}:*`,
        },
        StringEquals: { "aws:SourceAccount": Aws.ACCOUNT_ID },
      },
    });
  }

  private grantApplicationData(role: iam.Role, table: dynamodb.ITable): void {
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:ConditionCheckItem",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ],
        resources: [table.tableArn],
      }),
    );
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:Query"],
        resources: [
          table.tableArn,
          `${table.tableArn}/index/gsi1`,
          `${table.tableArn}/index/gsi2`,
        ],
      }),
    );
  }

  private grantBreakGlassAccess(role: iam.Role): void {
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ],
        resources: ["*"],
      }),
    );
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"],
        resources: [`${this.breakGlassLogGroup.logGroupArn}:*`],
      }),
    );
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["logs:DescribeLogGroups"],
        resources: ["*"],
      }),
    );
    Validations.of(role).acknowledge({
      id: "AwsSolutions-IAM5[Resource::*]",
      reason:
        "ECS Exec channel and DescribeLogGroups APIs do not support resource-level permissions; this inactive break-glass role is never attached to the normal service.",
    });
    const breakGlassLogResource = this.breakGlassLogGroup.node.defaultChild as logs.CfnLogGroup;
    Validations.of(role).acknowledge({
      id: `AwsSolutions-IAM5[Resource::<${this.getLogicalId(breakGlassLogResource)}.Arn>:*]`,
      reason:
        "The break-glass log stream suffix is runtime-generated by ECS Exec and is scoped to the dedicated retained log group.",
    });
  }

  private acknowledgeStaticEnvironment(definition: ecs.FargateTaskDefinition): void {
    Validations.of(definition).acknowledge({
      id: "AwsSolutions-ECS2",
      reason:
        "Only non-secret immutable deployment metadata is set directly; all credentials and private runtime/persona values use SSM SecureString task secrets.",
    });
  }

  private runtimeParameters(configVersion: string): RuntimeParameters {
    const secureParameter = (parameterName: string): ecs.Secret => {
      const arn = this.formatArn({
        resource: "parameter",
        resourceName: parameterName.slice(1),
        service: "ssm",
      });
      return {
        arn,
        grantRead: (grantee: iam.IGrantable): iam.Grant =>
          iam.Grant.addToPrincipal({
            actions: ["ssm:GetParameters"],
            grantee,
            resourceArns: [arn],
          }),
      };
    };
    const versionedRoot = `${PARAMETER_ROOT}/personas/${configVersion}`;

    return {
      secrets: {
        DISCORD_TOKEN_MODERATOR: secureParameter(
          `${PARAMETER_ROOT}/discord/moderator/token`,
        ),
        DISCORD_TOKEN_PARTICIPANT_A: secureParameter(
          `${PARAMETER_ROOT}/discord/participant-a/token`,
        ),
        DISCORD_TOKEN_PARTICIPANT_B: secureParameter(
          `${PARAMETER_ROOT}/discord/participant-b/token`,
        ),
        DISCORD_TOKEN_PARTICIPANT_C: secureParameter(
          `${PARAMETER_ROOT}/discord/participant-c/token`,
        ),
        OPENAI_API_KEY: secureParameter(`${PARAMETER_ROOT}/openai/api-key`),
        SHITTIM_PERSONA_MODERATOR_JSON: secureParameter(`${versionedRoot}/moderator`),
        SHITTIM_PERSONA_PARTICIPANT_A_JSON: secureParameter(
          `${versionedRoot}/participant-a`,
        ),
        SHITTIM_PERSONA_PARTICIPANT_B_JSON: secureParameter(
          `${versionedRoot}/participant-b`,
        ),
        SHITTIM_PERSONA_PARTICIPANT_C_JSON: secureParameter(
          `${versionedRoot}/participant-c`,
        ),
        SHITTIM_RUNTIME_CONFIG_JSON: secureParameter(
          `${PARAMETER_ROOT}/runtime/${configVersion}`,
        ),
      },
    };
  }

  private taskDefinition(options: {
    readonly containerName: string;
    readonly digest: string;
    readonly executionRole: iam.IRole;
    readonly imageRepository: ecr.IRepository;
    readonly logging: ecs.LogDriver;
    readonly parameters: RuntimeParameters;
    readonly readonlyRootFilesystem: boolean;
    readonly taskId: string;
    readonly taskRole: iam.IRole;
  }): ecs.FargateTaskDefinition {
    const definition = new ecs.FargateTaskDefinition(this, options.taskId, {
      cpu: 512,
      executionRole: options.executionRole,
      family: `shittim-chest-production-${options.readonlyRootFilesystem ? "normal" : "break-glass"}`,
      memoryLimitMiB: 1_024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole: options.taskRole,
    });
    const linuxParameters = new ecs.LinuxParameters(this, `${options.taskId}LinuxParameters`, {
      initProcessEnabled: true,
    });
    linuxParameters.dropCapabilities(ecs.Capability.ALL);
    definition.addContainer("ApplicationContainer", {
      containerName: options.containerName,
      environment: {
        AWS_REGION: "ap-northeast-1",
        SHITTIM_DYNAMODB_TABLE: "shittim-chest-production",
        SHITTIM_ENVIRONMENT: "production",
        SHITTIM_LOG_LEVEL: "INFO",
      },
      healthCheck: {
        command: ["CMD", "python", "-m", "shittim_chest.runtime.health"],
        interval: Duration.seconds(10),
        retries: 3,
        startPeriod: Duration.seconds(30),
        timeout: Duration.seconds(3),
      },
      image: ecs.ContainerImage.fromRegistry(
        options.imageRepository.repositoryUriForDigest(options.digest),
      ),
      linuxParameters,
      logging: options.logging,
      privileged: false,
      readonlyRootFilesystem: options.readonlyRootFilesystem,
      secrets: options.parameters.secrets,
      stopTimeout: Duration.seconds(120),
      user: "10001:10001",
      versionConsistency: ecs.VersionConsistency.ENABLED,
      workingDirectory: "/app",
    });
    // CDK LinuxParameters cannot express parameterized tmpfs mount options
    // (uid=/gid=/mode=), so declare the 1 MiB heartbeat tmpfs through the L1
    // task definition. Fargate supports tmpfs since the 2026-01 announcement.
    const cfnTaskDefinition = definition.node.defaultChild as ecs.CfnTaskDefinition;
    cfnTaskDefinition.addPropertyOverride("ContainerDefinitions.0.LinuxParameters.Tmpfs", [
      {
        ContainerPath: "/tmp/shittim-chest",
        MountOptions: ["nosuid", "nodev", "noexec", "uid=10001", "gid=10001", "mode=0700"],
        Size: 1,
      },
    ]);
    options.imageRepository.grantPull(options.executionRole);
    return definition;
  }
}
