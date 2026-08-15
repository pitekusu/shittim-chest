import {
  Aws,
  CfnOutput,
  CfnParameter,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  StackProps,
  Validations,
  aws_apigatewayv2 as apigatewayv2,
  aws_apigatewayv2_integrations as apigatewayv2Integrations,
  aws_dynamodb as dynamodb,
  aws_ec2 as ec2,
  aws_ecr as ecr,
  aws_ecs as ecs,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_scheduler as scheduler,
} from "aws-cdk-lib";
import { Construct } from "constructs";

import containerPolicy from "../../container-policy.json";

const IMAGE_DIGEST_PATTERN = "^sha256:[0-9a-f]{64}$";
const CONFIG_VERSION_PATTERN = "^v[0-9]{4}$";
const LAMBDA_BUNDLE_BUCKET_PATTERN = "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$";
const LAMBDA_BUNDLE_KEY_PATTERN =
  "^lambda/shittim-chest/[0-9a-f]{64}/shittim-chest-lambda-arm64\\.zip$";
const LAMBDA_BUNDLE_CODE_SHA256_PATTERN = "^[A-Za-z0-9+/]{43}=$";
const PARAMETER_ROOT = "/shittim-chest/production";
const DISCORD_PUBLIC_KEY_PARAMETER = `${PARAMETER_ROOT}/discord/moderator/public-key`;
const MODERATOR_TOKEN_PARAMETER = `${PARAMETER_ROOT}/discord/moderator/token`;
const RUNTIME_CLUSTER_NAME = "shittim-chest-production";
const RUNTIME_SERVICE_NAME = "shittim-chest-production";
const NORMAL_TASK_DEFINITION_FAMILY = "shittim-chest-production-normal";
const BREAK_GLASS_TASK_DEFINITION_FAMILY = "shittim-chest-production-break-glass";
const RECONCILER_SCHEDULE_NAME = "shittim-chest-production-runtime-reconciler";
const DISCORD_STATUS_PUBLISHER_FUNCTION_NAME =
  "shittim-chest-production-discord-status-publisher";
const DISCORD_INGRESS_ALIAS_NAME = "live";
const DISCORD_INGRESS_HANDLER =
  "shittim_chest.lambda_handlers.discord_ingress.lambda_handler";
const DISCORD_STATUS_HANDLER =
  "shittim_chest.lambda_handlers.discord_status_publisher.lambda_handler";
const RUNTIME_RECONCILER_HANDLER =
  "shittim_chest.lambda_handlers.runtime_reconciler.lambda_handler";
const IMAGE_ADMISSION_HANDLER =
  "shittim_chest.lambda_handlers.image_admission.lambda_handler";
const RUNTIME_UID = containerPolicy.runtime_identity.uid;
const RUNTIME_GID = containerPolicy.runtime_identity.gid;
const RUNTIME_USER = `${RUNTIME_UID}:${RUNTIME_GID}`;
const HEARTBEAT_TMPFS = containerPolicy.heartbeat_tmpfs;
const DEPLOYMENT_LOCK_PARTITION = "CONTROL#DEPLOYMENT";
const INGRESS_READABLE_PARTITION_PATTERNS = [
  "CONTROL#INGRESS",
  "DEBATE#*",
  "INGRESS_OPERATION#*",
  "INGRESS_SEMANTIC_OPERATION#*",
];
const INGRESS_WRITABLE_PARTITION_PATTERNS = [
  "CONTROL#INGRESS",
  "CONTROL#INGRESS#ACTIVE",
  "INGRESS_OPERATION#*",
  "INGRESS_SEMANTIC_OPERATION#*",
];
const STATUS_PUBLISHER_READABLE_PARTITION_PATTERNS = [
  "CONTROL#INGRESS",
  "INGRESS_OPERATION#*",
];
const STATUS_PUBLISHER_WRITABLE_PARTITION_PATTERNS = [
  "CONTROL#INGRESS",
  "INGRESS_OPERATION#*",
];
const RECONCILER_READABLE_PARTITION_PATTERNS = [
  "CONTROL#DEBATE",
  "CONTROL#GLOBAL",
  "CONTROL#INGRESS",
  "CONTROL#INGRESS#ACTIVE",
  "CONTROL#OUTBOX",
  "CONTROL#PANEL_REFRESH",
  "CONTROL#RUNTIME",
  "INGRESS_OPERATION#*",
];
const RECONCILER_WRITABLE_PARTITION_PATTERNS = [
  "CONTROL#INGRESS",
  "CONTROL#INGRESS#ACTIVE",
  "CONTROL#RUNTIME",
  "INGRESS_OPERATION#*",
];
const APPLICATION_WRITABLE_PARTITION_PATTERNS = [
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
];
const APPLICATION_READABLE_PARTITION_PATTERNS = [
  ...APPLICATION_WRITABLE_PARTITION_PATTERNS,
];
const INGRESS_CONDITION_CHECK_PARTITION_PATTERNS = [
  DEPLOYMENT_LOCK_PARTITION,
  "CONTROL#INGRESS",
];
const STATUS_PUBLISHER_CONDITION_CHECK_PARTITION_PATTERNS = [
  DEPLOYMENT_LOCK_PARTITION,
  "CONTROL#INGRESS",
];
const RECONCILER_CONDITION_CHECK_PARTITION_PATTERNS = [
  DEPLOYMENT_LOCK_PARTITION,
  "CONTROL#DEBATE",
  "CONTROL#GLOBAL",
  "CONTROL#INGRESS",
  "CONTROL#INGRESS#ACTIVE",
  "CONTROL#OUTBOX",
  "CONTROL#PANEL_REFRESH",
  "CONTROL#RUNTIME",
  "INGRESS_OPERATION#*",
];
const APPLICATION_CONDITION_CHECK_PARTITION_PATTERNS = [
  DEPLOYMENT_LOCK_PARTITION,
  ...APPLICATION_WRITABLE_PARTITION_PATTERNS,
];

export interface RuntimeStackProps extends StackProps {
  readonly debateTable: dynamodb.ITable;
  readonly imageRepository: ecr.IRepository;
  readonly signingProfileArn: string;
}

interface RuntimeParameters {
  readonly secrets: Record<string, ecs.Secret>;
}

interface ApplicationLambdaBundle {
  readonly code: lambda.CfnParametersCode;
  readonly codeSha256: string;
  readonly objectKey: string;
}

export class RuntimeStack extends Stack {
  public readonly applicationLogGroup: logs.LogGroup;
  public readonly breakGlassLogGroup: logs.LogGroup;
  public readonly breakGlassTaskDefinition: ecs.FargateTaskDefinition;
  public readonly cluster: ecs.Cluster;
  public readonly discordIngressAlias: lambda.Alias;
  public readonly discordIngressFunction: lambda.Function;
  public readonly discordStatusPublisherFunction: lambda.Function;
  public readonly interactionsApi: apigatewayv2.HttpApi;
  public readonly imageAdmissionFunction: lambda.Function;
  public readonly normalTaskDefinition: ecs.FargateTaskDefinition;
  public readonly runtimeReconcilerFunction: lambda.Function;
  public readonly runtimeReconcilerSchedule: scheduler.CfnSchedule;
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
      default: "v0004",
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
      removalPolicy: RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
      retention: logs.RetentionDays.THREE_MONTHS,
    });
    this.breakGlassLogGroup = new logs.LogGroup(this, "BreakGlassExecLogGroup", {
      dataProtectionPolicy,
      logGroupName: "/ecs/shittim-chest/production/break-glass-exec",
      removalPolicy: RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
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
      clusterName: RUNTIME_CLUSTER_NAME,
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
    const statusPublisherArn = this.formatArn({
      resource: "function",
      resourceName: DISCORD_STATUS_PUBLISHER_FUNCTION_NAME,
      service: "lambda",
    });
    for (const role of [normalTaskRole, breakGlassTaskRole]) {
      role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ["lambda:InvokeFunction"],
          resources: [statusPublisherArn],
        }),
      );
    }
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
      circuitBreaker: { rollback: true },
      cluster: this.cluster,
      desiredCount: 0,
      enableECSManagedTags: true,
      enableExecuteCommand: false,
      maxHealthyPercent: 100,
      minHealthyPercent: 0,
      platformVersion: ecs.FargatePlatformVersion.LATEST,
      propagateTags: ecs.PropagatedTagSource.SERVICE,
      securityGroups: [this.taskSecurityGroup],
      serviceName: RUNTIME_SERVICE_NAME,
      taskDefinition: this.normalTaskDefinition,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    const sharedLambdaBundle = this.applicationLambdaCode();
    const runtimeServiceArn = this.formatArn({
      resource: "service",
      resourceName: `${RUNTIME_CLUSTER_NAME}/${RUNTIME_SERVICE_NAME}`,
      service: "ecs",
    });
    this.imageAdmissionFunction = this.createApplicationFunction({
      code: sharedLambdaBundle.code,
      environment: {
        SHITTIM_ECR_REPOSITORY_NAME: props.imageRepository.repositoryName,
        SHITTIM_ECR_REPOSITORY_URI: props.imageRepository.repositoryUri,
        SHITTIM_ECS_SERVICE_ARN: runtimeServiceArn,
        SHITTIM_EXPECTED_CONTAINER_NAME: "application",
        SHITTIM_SIGNING_PROFILE_ARN: props.signingProfileArn,
      },
      functionName: "shittim-chest-production-image-admission",
      handler: IMAGE_ADMISSION_HANDLER,
      id: "ImageAdmissionFunction",
      memorySize: 256,
      reservedConcurrency: 1,
      timeout: Duration.seconds(30),
    });
    this.configureImageAdmission(
      props.imageRepository,
      runtimeServiceArn,
    );
    const runtimeConfigParameter = `${PARAMETER_ROOT}/runtime/${configVersion.valueAsString}`;
    this.discordStatusPublisherFunction = this.createApplicationFunction({
      code: sharedLambdaBundle.code,
      environment: {
        SHITTIM_DYNAMODB_TABLE: props.debateTable.tableName,
        SHITTIM_MODERATOR_TOKEN_PARAMETER: MODERATOR_TOKEN_PARAMETER,
        SHITTIM_RUNTIME_CONFIG_PARAMETER: runtimeConfigParameter,
      },
      functionName: DISCORD_STATUS_PUBLISHER_FUNCTION_NAME,
      handler: DISCORD_STATUS_HANDLER,
      id: "DiscordStatusPublisherFunction",
      memorySize: 256,
      reservedConcurrency: 2,
      timeout: Duration.seconds(120),
    });
    this.runtimeReconcilerFunction = this.createApplicationFunction({
      code: sharedLambdaBundle.code,
      environment: {
        SHITTIM_DYNAMODB_TABLE: props.debateTable.tableName,
        SHITTIM_ECS_CLUSTER: this.cluster.clusterName,
        SHITTIM_ECS_SERVICE: this.service.serviceName,
        SHITTIM_STATUS_PUBLISHER_FUNCTION:
          this.discordStatusPublisherFunction.functionName,
      },
      functionName: "shittim-chest-production-runtime-reconciler",
      handler: RUNTIME_RECONCILER_HANDLER,
      id: "RuntimeReconcilerFunction",
      memorySize: 256,
      reservedConcurrency: 1,
      timeout: Duration.seconds(55),
    });
    this.discordIngressFunction = this.createApplicationFunction({
      code: sharedLambdaBundle.code,
      environment: {
        SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER: DISCORD_PUBLIC_KEY_PARAMETER,
        SHITTIM_DYNAMODB_TABLE: props.debateTable.tableName,
        SHITTIM_RUNTIME_CONFIG_PARAMETER: runtimeConfigParameter,
      },
      functionName: "shittim-chest-production-discord-ingress",
      handler: DISCORD_INGRESS_HANDLER,
      id: "DiscordIngressFunction",
      memorySize: 512,
      reservedConcurrency: 5,
      snapStart: lambda.SnapStartConf.ON_PUBLISHED_VERSIONS,
      // The application stops at 2.0s; 5s is only a final safety net for an
      // SDK call unwinding after cancellation and must not define Discord UX.
      timeout: Duration.seconds(5),
    });
    const discordIngressVersion = new lambda.Version(
      this,
      "DiscordIngressVersion",
      {
        codeSha256: sharedLambdaBundle.codeSha256,
        description: Fn.join("|", [
          sharedLambdaBundle.objectKey,
          runtimeConfigParameter,
          DISCORD_PUBLIC_KEY_PARAMETER,
        ]),
        lambda: this.discordIngressFunction,
        removalPolicy: RemovalPolicy.DESTROY,
      },
    );
    Validations.of(this.discordIngressFunction).acknowledge({
      id: "Construct-Annotations::@aws-cdk/aws-lambda:snapStartRequirePublish",
      reason:
        "DiscordIngressVersion publishes the checksum-bound version consumed by the live alias.",
    });
    this.discordIngressAlias = new lambda.Alias(this, "DiscordIngressAlias", {
      aliasName: DISCORD_INGRESS_ALIAS_NAME,
      version: discordIngressVersion,
    });

    this.grantApplicationLambdaAccess(props.debateTable, runtimeConfigParameter);
    this.discordStatusPublisherFunction.configureAsyncInvoke({
      maxEventAge: Duration.minutes(15),
      retryAttempts: 2,
    });
    this.runtimeReconcilerFunction.configureAsyncInvoke({
      maxEventAge: Duration.minutes(2),
      retryAttempts: 1,
    });

    this.interactionsApi = this.createDiscordInteractionsApi(
      this.discordIngressAlias,
    );
    this.runtimeReconcilerSchedule = this.createRuntimeReconcilerSchedule(
      this.runtimeReconcilerFunction,
    );
  }

  private applicationLambdaCode(): ApplicationLambdaBundle {
    const bundleBucket = new CfnParameter(this, "LambdaBundleBucketName", {
      allowedPattern: LAMBDA_BUNDLE_BUCKET_PATTERN,
      description: "S3 bucket containing the verified shared Python Lambda bundle",
      type: "String",
    });
    const bundleKey = new CfnParameter(this, "LambdaBundleObjectKey", {
      allowedPattern: LAMBDA_BUNDLE_KEY_PATTERN,
      description: "Content-addressed key for the verified shared Python Lambda bundle",
      type: "String",
    });
    const bundleCodeSha256 = new CfnParameter(this, "LambdaBundleCodeSha256", {
      allowedPattern: LAMBDA_BUNDLE_CODE_SHA256_PATTERN,
      description: "Base64 SHA-256 checksum of the verified shared Python Lambda bundle",
      type: "String",
    });
    return {
      code: lambda.Code.fromCfnParameters({
        bucketNameParam: bundleBucket,
        objectKeyParam: bundleKey,
      }),
      codeSha256: bundleCodeSha256.valueAsString,
      objectKey: bundleKey.valueAsString,
    };
  }

  private createApplicationFunction(options: {
    readonly code: lambda.Code;
    readonly environment: Record<string, string>;
    readonly functionName: string;
    readonly handler: string;
    readonly id: string;
    readonly memorySize: number;
    readonly reservedConcurrency: number;
    readonly snapStart?: lambda.SnapStartConf;
    readonly timeout: Duration;
  }): lambda.Function {
    const logGroup = new logs.LogGroup(this, `${options.id}LogGroup`, {
      dataProtectionPolicy: this.lambdaDataProtectionPolicy(
        `${options.functionName}-log-protection`,
      ),
      logGroupName: `/aws/lambda/${options.functionName}`,
      removalPolicy: RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
      retention: logs.RetentionDays.THREE_MONTHS,
    });
    const role = this.lambdaRole(
      `${options.id}Role`,
      `${options.functionName}-role`,
      logGroup,
    );
    const function_ = new lambda.Function(this, options.id, {
      architecture: lambda.Architecture.ARM_64,
      code: options.code,
      environment: options.environment,
      functionName: options.functionName,
      handler: options.handler,
      logGroup,
      loggingFormat: lambda.LoggingFormat.JSON,
      memorySize: options.memorySize,
      reservedConcurrentExecutions: options.reservedConcurrency,
      role,
      runtime: lambda.Runtime.PYTHON_3_14,
      snapStart: options.snapStart,
      timeout: options.timeout,
      tracing: lambda.Tracing.DISABLED,
    });
    Validations.of(function_).acknowledge({
      id: "AwsSolutions-L1",
      reason:
        "Python 3.14 is the current project runtime and is explicitly selected instead of an unpinned latest runtime.",
    });
    return function_;
  }

  private grantApplicationLambdaAccess(
    table: dynamodb.ITable,
    runtimeConfigParameter: string,
  ): void {
    this.addTableActions(this.discordIngressFunction, table, {
      conditionCheckPartitionPatterns: INGRESS_CONDITION_CHECK_PARTITION_PATTERNS,
      readActions: ["dynamodb:GetItem"],
      readablePartitionPatterns: INGRESS_READABLE_PARTITION_PATTERNS,
      writablePartitionPatterns: INGRESS_WRITABLE_PARTITION_PATTERNS,
      writeActions: ["dynamodb:PutItem", "dynamodb:UpdateItem"],
    });
    this.addTableActions(this.discordStatusPublisherFunction, table, {
      conditionCheckPartitionPatterns:
        STATUS_PUBLISHER_CONDITION_CHECK_PARTITION_PATTERNS,
      readActions: ["dynamodb:GetItem"],
      readablePartitionPatterns: STATUS_PUBLISHER_READABLE_PARTITION_PATTERNS,
      writablePartitionPatterns: STATUS_PUBLISHER_WRITABLE_PARTITION_PATTERNS,
      writeActions: ["dynamodb:PutItem", "dynamodb:UpdateItem"],
    });
    this.addTableActions(this.runtimeReconcilerFunction, table, {
      conditionCheckPartitionPatterns: RECONCILER_CONDITION_CHECK_PARTITION_PATTERNS,
      readActions: ["dynamodb:GetItem"],
      readablePartitionPatterns: RECONCILER_READABLE_PARTITION_PATTERNS,
      writablePartitionPatterns: RECONCILER_WRITABLE_PARTITION_PATTERNS,
      writeActions: [
        "dynamodb:DeleteItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
      ],
    });
    this.addTableQueryActions(
      this.runtimeReconcilerFunction,
      table,
      RECONCILER_READABLE_PARTITION_PATTERNS,
    );

    this.grantParameterRead(this.discordIngressFunction, runtimeConfigParameter);
    this.grantParameterRead(
      this.discordIngressFunction,
      DISCORD_PUBLIC_KEY_PARAMETER,
    );

    this.grantParameterRead(
      this.discordStatusPublisherFunction,
      runtimeConfigParameter,
    );
    this.grantParameterRead(
      this.discordStatusPublisherFunction,
      MODERATOR_TOKEN_PARAMETER,
    );

    this.runtimeReconcilerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [this.discordStatusPublisherFunction.functionArn],
      }),
    );

    this.runtimeReconcilerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeServices", "ecs:UpdateService"],
        resources: [this.service.serviceArn],
      }),
    );
  }

  private addTableActions(
    function_: lambda.Function,
    table: dynamodb.ITable,
    options: {
      conditionCheckPartitionPatterns: string[];
      readActions: string[];
      readablePartitionPatterns: string[];
      writablePartitionPatterns: string[];
      writeActions: string[];
    },
  ): void {
    function_.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:ConditionCheckItem"],
        conditions: this.leadingKeyConditions(
          options.conditionCheckPartitionPatterns,
        ),
        resources: [table.tableArn],
      }),
    );
    if (options.readActions.length > 0) {
      function_.addToRolePolicy(
        new iam.PolicyStatement({
          actions: options.readActions,
          conditions: this.leadingKeyConditions(options.readablePartitionPatterns),
          resources: [table.tableArn],
        }),
      );
    }
    if (options.writeActions.length > 0) {
      function_.addToRolePolicy(
        new iam.PolicyStatement({
          actions: options.writeActions,
          conditions: this.leadingKeyConditions(options.writablePartitionPatterns),
          resources: [table.tableArn],
        }),
      );
    }
  }

  private grantParameterRead(function_: lambda.Function, parameterName: string): void {
    function_.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [this.parameterArn(parameterName)],
      }),
    );
  }

  private parameterArn(parameterName: string): string {
    return this.formatArn({
      resource: "parameter",
      resourceName: parameterName.slice(1),
      service: "ssm",
    });
  }

  private createDiscordInteractionsApi(
    ingress: lambda.IFunction,
  ): apigatewayv2.HttpApi {
    const accessLogs = new logs.LogGroup(this, "DiscordInteractionsAccessLogGroup", {
      dataProtectionPolicy: this.lambdaDataProtectionPolicy(
        "shittim-chest-discord-interactions-access-log-protection",
      ),
      logGroupName: "/aws/apigateway/shittim-chest/production/discord-interactions",
      removalPolicy: RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
      retention: logs.RetentionDays.THREE_MONTHS,
    });
    const api = new apigatewayv2.HttpApi(this, "DiscordInteractionsApi", {
      apiName: "shittim-chest-production-discord-interactions",
      createDefaultStage: false,
      description: "Signed Discord Interaction endpoint",
      disableExecuteApiEndpoint: false,
    });
    api.addRoutes({
      integration: new apigatewayv2Integrations.HttpLambdaIntegration(
        "DiscordIngressIntegration",
        ingress,
        { payloadFormatVersion: apigatewayv2.PayloadFormatVersion.VERSION_2_0 },
      ),
      methods: [apigatewayv2.HttpMethod.POST],
      path: "/interactions",
    });
    const stage = new apigatewayv2.HttpStage(this, "DiscordInteractionsStage", {
      autoDeploy: true,
      httpApi: api,
      stageName: "$default",
      throttle: {
        // Keep API admission within the ingress function's reserved concurrency
        // instead of accepting a burst that Lambda must synchronously throttle.
        burstLimit: 5,
        rateLimit: 2,
      },
    });
    const cfnStage = stage.node.defaultChild as apigatewayv2.CfnStage;
    cfnStage.accessLogSettings = {
      destinationArn: accessLogs.logGroupArn,
      format: JSON.stringify({
        integrationStatus: "$context.integration.status",
        requestId: "$context.requestId",
        responseLength: "$context.responseLength",
        routeKey: "$context.routeKey",
        status: "$context.status",
      }),
    };
    new CfnOutput(this, "DiscordInteractionsEndpointUrl", {
      description: "Register this URL as the Discord Interactions Endpoint after rollout gates",
      value: `${api.apiEndpoint}/interactions`,
    });
    Validations.of(api).acknowledge({
      id: "AwsSolutions-APIG4",
      reason:
        "Discord cannot use an API Gateway authorizer; the only POST route validates Ed25519 over the untouched raw body before parsing.",
    });
    return api;
  }

  private createRuntimeReconcilerSchedule(
    reconciler: lambda.IFunction,
  ): scheduler.CfnSchedule {
    const scheduleGroupArn = this.formatArn({
      resource: "schedule-group",
      resourceName: "default",
      service: "scheduler",
    });
    const role = new iam.Role(this, "RuntimeReconcilerSchedulerRole", {
      assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com", {
        conditions: {
          ArnEquals: { "aws:SourceArn": scheduleGroupArn },
          StringEquals: { "aws:SourceAccount": Aws.ACCOUNT_ID },
        },
      }),
      description: "Invoke only the runtime reconciler on its one-minute schedule",
      roleName: "ShittimChest-Prod-RuntimeReconcilerScheduler",
    });
    const invokePolicy = new iam.Policy(this, "RuntimeReconcilerSchedulerPolicy", {
      statements: [
        new iam.PolicyStatement({
          actions: ["lambda:InvokeFunction"],
          resources: [reconciler.functionArn],
        }),
      ],
    });
    invokePolicy.attachToRole(role);
    const schedule = new scheduler.CfnSchedule(this, "RuntimeReconcilerSchedule", {
      flexibleTimeWindow: { mode: "OFF" },
      name: RECONCILER_SCHEDULE_NAME,
      scheduleExpression: "rate(1 minute)",
      state: "ENABLED",
      target: {
        arn: reconciler.functionArn,
        input: JSON.stringify({ schema_version: 1, trigger: "scheduled" }),
        retryPolicy: {
          maximumEventAgeInSeconds: 120,
          maximumRetryAttempts: 2,
        },
        roleArn: role.roleArn,
      },
    });
    schedule.node.addDependency(invokePolicy);
    return schedule;
  }

  private lambdaDataProtectionPolicy(name: string): logs.DataProtectionPolicy {
    return new logs.DataProtectionPolicy({
      description: "Mask common credentials and identifiers in serverless logs",
      identifiers: [
        logs.DataIdentifier.AWSSECRETKEY,
        logs.DataIdentifier.EMAILADDRESS,
        logs.DataIdentifier.IPADDRESS,
        logs.DataIdentifier.OPENSSHPRIVATEKEY,
        logs.DataIdentifier.PGPPRIVATEKEY,
        logs.DataIdentifier.PKCSPRIVATEKEY,
      ],
      name,
    });
  }

  private lambdaRole(id: string, roleName: string, logGroup: logs.LogGroup): iam.Role {
    const role = new iam.Role(this, id, {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: `Write only to ${logGroup.logGroupName} and use explicitly added service actions`,
      roleName,
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [`${logGroup.logGroupArn}:*`],
      }),
    );
    const logResource = logGroup.node.defaultChild as logs.CfnLogGroup;
    Validations.of(role).acknowledge({
      id: `AwsSolutions-IAM5[Resource::<${this.getLogicalId(logResource)}.Arn>:*]`,
      reason:
        "CloudWatch Logs creates unpredictable stream names; access remains confined to this function's dedicated retained log group.",
    });
    return role;
  }

  private configureImageAdmission(
    repository: ecr.IRepository,
    serviceArn: string,
  ): void {
    const serviceRevisionArn =
      `arn:aws:ecs:${this.region}:*:service-revision/` +
      `${RUNTIME_CLUSTER_NAME}/${RUNTIME_SERVICE_NAME}/*`;
    this.imageAdmissionFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeServiceRevisions"],
        conditions: {
          StringEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID },
        },
        resources: [serviceArn, serviceRevisionArn],
      }),
    );
    this.imageAdmissionFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeTaskDefinition"],
        resources: ["*"],
      }),
    );
    this.imageAdmissionFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecr:BatchGetImage", "ecr:DescribeImageSigningStatus"],
        resources: [repository.repositoryArn],
      }),
    );

    const hookRole = new iam.Role(this, "ImageAdmissionHookRole", {
      assumedBy: new iam.ServicePrincipal("ecs.amazonaws.com"),
      description: "Allow ECS deployment lifecycle hooks to invoke image admission only",
      roleName: "ShittimChest-Prod-ImageAdmissionHook",
    });
    const invokePolicy = new iam.Policy(this, "ImageAdmissionHookInvokePolicy", {
      statements: [
        new iam.PolicyStatement({
          actions: ["lambda:InvokeFunction"],
          resources: [this.imageAdmissionFunction.functionArn],
        }),
      ],
    });
    invokePolicy.attachToRole(hookRole);
    Validations.of(this.imageAdmissionFunction.role!).acknowledge({
      id: "AwsSolutions-IAM5[Resource::*]",
      reason:
        "ECS DescribeTaskDefinition does not support resource-level permissions; the handler validates the exact revision ARN, task definition ARN, container name, repository, and image digest.",
    });
    Validations.of(this.imageAdmissionFunction.role!).acknowledge({
      id:
        "AwsSolutions-IAM5[Resource::arn:aws:ecs:" +
        `${this.region}:*:service-revision/` +
        `${RUNTIME_CLUSTER_NAME}/${RUNTIME_SERVICE_NAME}/*]`,
      reason:
        "DescribeServiceRevisions requires deployment-generated revision and account segments; aws:ResourceAccount and the named production service confine both wildcards.",
    });
    // FargateService has no L2 lifecycle-hook API in the current aws-cdk-lib.
    // Keep the escape hatch confined to the generated lifecycleHooks field.
    // CloudFormation declares HookDetails as Json but the ECS resource provider
    // requires a JSON object serialized as a string.
    const cfnService = this.service.node.defaultChild as ecs.CfnService;
    cfnService.addPropertyOverride("DeploymentConfiguration.LifecycleHooks", [
      {
        HookDetails: JSON.stringify({ schemaVersion: 1 }),
        HookTargetArn: this.imageAdmissionFunction.functionArn,
        LifecycleStages: ["PRE_SCALE_UP"],
        RoleArn: hookRole.roleArn,
        TargetType: "AWS_LAMBDA",
        TimeoutConfiguration: {
          Action: "ROLLBACK",
          TimeoutInMinutes: 5,
        },
      },
    ]);
    cfnService.node.addDependency(invokePolicy);
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
        actions: ["dynamodb:GetItem"],
        conditions: this.leadingKeyConditions(
          APPLICATION_READABLE_PARTITION_PATTERNS,
        ),
        resources: [table.tableArn],
      }),
    );
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:ConditionCheckItem"],
        conditions: this.leadingKeyConditions(
          APPLICATION_CONDITION_CHECK_PARTITION_PATTERNS,
        ),
        resources: [table.tableArn],
      }),
    );
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:DeleteItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ],
        conditions: this.leadingKeyConditions(
          APPLICATION_WRITABLE_PARTITION_PATTERNS,
        ),
        resources: [table.tableArn],
      }),
    );
    this.addTableQueryActions(
      role,
      table,
      APPLICATION_READABLE_PARTITION_PATTERNS,
    );
  }

  private addTableQueryActions(
    grantee: iam.IGrantable,
    table: dynamodb.ITable,
    readablePartitionPatterns: string[],
  ): void {
    grantee.grantPrincipal.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:Query"],
        conditions: this.leadingKeyConditions(readablePartitionPatterns),
        resources: [table.tableArn],
      }),
    );
    grantee.grantPrincipal.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:Query"],
        resources: [
          `${table.tableArn}/index/gsi1`,
          `${table.tableArn}/index/gsi2`,
        ],
      }),
    );
  }

  private leadingKeyConditions(
    partitionPatterns: string[],
  ): Record<string, Record<string, string | string[]>> {
    return {
      "ForAllValues:StringLike": {
        "dynamodb:LeadingKeys": partitionPatterns,
      },
      Null: { "dynamodb:LeadingKeys": "false" },
    };
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
      family: options.readonlyRootFilesystem
        ? NORMAL_TASK_DEFINITION_FAMILY
        : BREAK_GLASS_TASK_DEFINITION_FAMILY,
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
        SHITTIM_STATUS_PUBLISHER_FUNCTION: DISCORD_STATUS_PUBLISHER_FUNCTION_NAME,
      },
      healthCheck: {
        command: ["CMD", "python", "-m", "shittim_chest.healthcheck"],
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
      user: RUNTIME_USER,
      versionConsistency: ecs.VersionConsistency.ENABLED,
      workingDirectory: "/app",
    });
    // CDK LinuxParameters cannot express parameterized tmpfs mount options
    // (uid=/gid=/mode=), so declare the 1 MiB heartbeat tmpfs through the L1
    // task definition. Fargate supports tmpfs since the 2026-01 announcement.
    const cfnTaskDefinition = definition.node.defaultChild as ecs.CfnTaskDefinition;
    cfnTaskDefinition.addPropertyOverride("ContainerDefinitions.0.LinuxParameters.Tmpfs", [
      {
        ContainerPath: HEARTBEAT_TMPFS.path,
        MountOptions: [
          ...HEARTBEAT_TMPFS.mount_options,
          `uid=${RUNTIME_UID}`,
          `gid=${RUNTIME_GID}`,
          `mode=${HEARTBEAT_TMPFS.mode}`,
        ],
        Size: HEARTBEAT_TMPFS.size_mib,
      },
    ]);
    options.imageRepository.grantPull(options.executionRole);
    return definition;
  }
}
