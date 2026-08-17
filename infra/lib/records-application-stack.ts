import {
  CfnParameter,
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Validations,
  aws_apigateway as apigateway,
  aws_apigatewayv2 as apigatewayv2,
  aws_apigatewayv2_integrations as integrations,
  aws_dynamodb as dynamodb,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_lambda_event_sources as eventSources,
  aws_logs as logs,
  aws_s3 as s3,
  aws_ssm as ssm,
  aws_sqs as sqs,
} from "aws-cdk-lib";
import { Construct } from "constructs";

export interface RecordsApplicationStackProps extends StackProps {
}

const BACKFILL_FUNCTION_NAME = "shittim-chest-production-records-backfill";
const AUTH_FUNCTION_NAME = "shittim-chest-production-records-auth";
const READ_FUNCTION_NAME = "shittim-chest-production-records-read";

export class RecordsApplicationStack extends Stack {
  public readonly projectorFunction: lambda.Function;
  public readonly backfillFunction: lambda.Function;
  public readonly authFunction: lambda.Function;
  public readonly readFunction: lambda.Function;

  public constructor(
    scope: Construct,
    id: string,
    props: RecordsApplicationStackProps,
  ) {
    super(scope, id, props);

    const sourceTableName = new CfnParameter(this, "SourceDebateTableName", {
      type: "String",
      default: "shittim-chest-production",
      allowedPattern: "^[A-Za-z0-9_.-]{3,255}$",
    });
    const sourceTableStreamArn = new CfnParameter(this, "SourceDebateTableStreamArn", {
      type: "String",
      allowedPattern: "^arn:[^:]+:dynamodb:[^:]+:[0-9]{12}:table/[^/]+/stream/.+$",
    });
    const bundleBucketName = new CfnParameter(this, "RecordsBundleBucketName", {
      type: "String",
      allowedPattern: "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    });
    const bundleObjectKey = new CfnParameter(this, "RecordsBundleObjectKey", {
      type: "String",
      minLength: 1,
    });
    const bundleObjectVersion = new CfnParameter(this, "RecordsBundleObjectVersion", {
      type: "String",
      minLength: 1,
    });
    const bundleCodeSha256 = new CfnParameter(this, "RecordsBundleCodeSha256", {
      type: "String",
      allowedPattern: "^[A-Za-z0-9+/]{43}=$",
      description: "Base64-encoded SHA-256 of the immutable Records Lambda bundle",
    });

    const sourceTable = dynamodb.Table.fromTableAttributes(this, "SourceDebateTable", {
      tableName: sourceTableName.valueAsString,
      tableStreamArn: sourceTableStreamArn.valueAsString,
    });
    const archiveTable = dynamodb.Table.fromTableName(
      this,
      "ArchiveTable",
      "shittim-chest-production-records",
    );
    const statisticsTable = dynamodb.Table.fromTableName(
      this,
      "StatisticsTable",
      "shittim-chest-production-records-statistics",
    );
    const sessionTable = dynamodb.Table.fromTableName(
      this,
      "SessionTable",
      "shittim-chest-production-records-sessions",
    );
    const mediaBucket = s3.Bucket.fromBucketName(
      this,
      "MediaBucket",
      `shittim-chest-production-records-media-${this.account}`,
    );
    const projectorDlq = sqs.Queue.fromQueueArn(
      this,
      "ProjectorDlq",
      this.formatArn({
        service: "sqs",
        resource: "shittim-chest-production-records-projector-dlq",
      }),
    );
    const bundleBucket = s3.Bucket.fromBucketName(
      this,
      "RecordsBundleBucket",
      bundleBucketName.valueAsString,
    );
    const code = lambda.Code.fromBucket(
      bundleBucket,
      bundleObjectKey.valueAsString,
      bundleObjectVersion.valueAsString,
    );
    const identityParameter = ssm.StringParameter.fromSecureStringParameterAttributes(
      this,
      "IdentityHmacParameter",
      {
        parameterName: "/shittim-chest/production/records/identity-hmac-key",
      },
    );
    const presentationParameter = ssm.StringParameter.fromSecureStringParameterAttributes(
      this,
      "PresentationParameter",
      {
        parameterName: "/shittim-chest/production/records/presentation/v0001",
      },
    );
    const oauthConfigParameter = ssm.StringParameter.fromSecureStringParameterAttributes(
      this,
      "OauthConfigParameter",
      {
        parameterName: "/shittim-chest/production/records/discord/oauth/v0001",
      },
    );
    const oauthClientSecretParameter =
      ssm.StringParameter.fromSecureStringParameterAttributes(
        this,
        "OauthClientSecretParameter",
        {
          parameterName: "/shittim-chest/production/records/discord/client-secret",
        },
      );
    const sessionKeyParameter = ssm.StringParameter.fromSecureStringParameterAttributes(
      this,
      "SessionKeyParameter",
      {
        parameterName: "/shittim-chest/production/records/session-key",
      },
    );

    this.projectorFunction = this.functionWithRole({
      id: "ProjectorFunction",
      functionName: "shittim-chest-production-records-projector",
      handler: "shittim_records.lambda_handlers.projector_handler",
      code,
      timeout: Duration.minutes(1),
      reservedConcurrentExecutions: 1,
      sourceTable,
      archiveTable,
      statisticsTable,
      identityParameter,
      presentationParameter,
      allowScan: false,
    });
    this.projectorFunction.addEventSource(
      new eventSources.DynamoEventSource(sourceTable, {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        batchSize: 10,
        bisectBatchOnError: true,
        maxRecordAge: Duration.hours(1),
        onFailure: new eventSources.SqsDlq(projectorDlq),
        reportBatchItemFailures: true,
        retryAttempts: 3,
        filters: [
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.isEqual("MODIFY"),
            dynamodb: {
              NewImage: {
                record_type: { S: lambda.FilterRule.isEqual("debate_meta") },
                current_phase: { S: lambda.FilterRule.isEqual("completed") },
              },
            },
          }),
        ],
      }),
    );
    Validations.of(this.projectorFunction.role!).acknowledge({
      id: "AwsSolutions-IAM5[Resource::*]",
      reason:
        "dynamodb:ListStreams does not support resource-level permissions; the event source mapping is separately bound to one exact stream ARN.",
    });

    this.backfillFunction = this.functionWithRole({
      id: "BackfillFunction",
      functionName: BACKFILL_FUNCTION_NAME,
      handler: "shittim_records.lambda_handlers.backfill_handler",
      code,
      timeout: Duration.minutes(15),
      sourceTable,
      archiveTable,
      statisticsTable,
      identityParameter,
      presentationParameter,
      allowScan: true,
    });

    this.authFunction = this.httpFunctionWithRole({
      id: "AuthFunction",
      functionName: AUTH_FUNCTION_NAME,
      handler: "shittim_records.lambda_handlers.auth_handler",
      code,
      timeout: Duration.seconds(15),
      reservedConcurrentExecutions: 2,
      environment: {
        SESSION_TABLE_NAME: sessionTable.tableName,
        MEDIA_BUCKET_NAME: mediaBucket.bucketName,
        IDENTITY_HMAC_PARAMETER_NAME: identityParameter.parameterName,
        OAUTH_CONFIG_PARAMETER_NAME: oauthConfigParameter.parameterName,
        OAUTH_CLIENT_SECRET_PARAMETER_NAME: oauthClientSecretParameter.parameterName,
        SESSION_KEY_PARAMETER_NAME: sessionKeyParameter.parameterName,
      },
      policyStatements: [
        new iam.PolicyStatement({
          actions: [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:TransactWriteItems",
          ],
          resources: [sessionTable.tableArn],
        }),
        new iam.PolicyStatement({
          actions: ["ssm:GetParameters"],
          resources: [
            identityParameter.parameterArn,
            oauthConfigParameter.parameterArn,
            oauthClientSecretParameter.parameterArn,
            sessionKeyParameter.parameterArn,
          ],
        }),
        new iam.PolicyStatement({
          actions: ["s3:GetObject", "s3:PutObject"],
          resources: [`${mediaBucket.bucketArn}/requesters/*`],
        }),
      ],
    });
    this.authFunction.role!.node.addMetadata(
      Validations.ACKNOWLEDGED_RULES_METADATA_KEY,
      Object.fromEntries(
        ["arn:aws", "arn:<AWS::Partition>"].map((partition) => [
          `AwsSolutions-IAM5[Resource::${partition}:s3:::` +
            `shittim-chest-production-records-media-${this.account}/requesters/*]`,
          "OAuth avatar caching is restricted to opaque object keys below the requester media prefix.",
        ]),
      ),
    );
    this.readFunction = this.httpFunctionWithRole({
      id: "ReadFunction",
      functionName: READ_FUNCTION_NAME,
      handler: "shittim_records.lambda_handlers.read_handler",
      code,
      timeout: Duration.seconds(10),
      reservedConcurrentExecutions: 4,
      environment: {
        ARCHIVE_TABLE_NAME: archiveTable.tableName,
        SESSION_TABLE_NAME: sessionTable.tableName,
        MEDIA_BUCKET_NAME: mediaBucket.bucketName,
        SESSION_KEY_PARAMETER_NAME: sessionKeyParameter.parameterName,
      },
      policyStatements: [
        new iam.PolicyStatement({
          actions: ["dynamodb:GetItem", "dynamodb:BatchGetItem"],
          resources: [sessionTable.tableArn],
        }),
        new iam.PolicyStatement({
          actions: ["dynamodb:Query"],
          resources: [
            archiveTable.tableArn,
            `${archiveTable.tableArn}/index/gsi1`,
            `${archiveTable.tableArn}/index/gsi2`,
          ],
        }),
        new iam.PolicyStatement({
          actions: ["ssm:GetParameters"],
          resources: [sessionKeyParameter.parameterArn],
        }),
        new iam.PolicyStatement({
          actions: ["s3:GetObject"],
          resources: [
            `${mediaBucket.bucketArn}/participants/*`,
            `${mediaBucket.bucketArn}/requesters/*`,
          ],
        }),
      ],
    });
    this.readFunction.role!.node.addMetadata(
      Validations.ACKNOWLEDGED_RULES_METADATA_KEY,
      Object.fromEntries(
        ["arn:aws", "arn:<AWS::Partition>"].flatMap((partition) =>
          ["participants", "requesters"].map((prefix) => [
            `AwsSolutions-IAM5[Resource::${partition}:s3:::` +
              `shittim-chest-production-records-media-${this.account}/${prefix}/*]`,
            "Authenticated avatar reads are restricted to the two approved private media prefixes.",
          ]),
        ),
      ),
    );

    const authVersion = new lambda.Version(this, "AuthVersion", {
      lambda: this.authFunction,
      codeSha256: bundleCodeSha256.valueAsString,
    });
    const readVersion = new lambda.Version(this, "ReadVersion", {
      lambda: this.readFunction,
      codeSha256: bundleCodeSha256.valueAsString,
    });
    const authAlias = new lambda.Alias(this, "AuthLiveAlias", {
      aliasName: "live",
      version: authVersion,
    });
    const readAlias = new lambda.Alias(this, "ReadLiveAlias", {
      aliasName: "live",
      version: readVersion,
    });

    const accessLogs = new logs.LogGroup(this, "RecordsApiAccessLogs", {
      logGroupName: "/aws/apigateway/shittim-chest-production-records",
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const api = new apigatewayv2.HttpApi(this, "RecordsApi", {
      apiName: "shittim-chest-production-records",
      createDefaultStage: false,
      disableExecuteApiEndpoint: false,
    });
    Validations.of(api).acknowledge({
      id: "AwsSolutions-APIG4",
      reason:
        "OAuth bootstrap routes are public by design; protected routes validate the server-side hashed Session record inside the two isolated Lambda handlers.",
    });
    const authIntegration = new integrations.HttpLambdaIntegration(
      "AuthIntegration",
      authAlias,
      { payloadFormatVersion: apigatewayv2.PayloadFormatVersion.VERSION_2_0 },
    );
    const readIntegration = new integrations.HttpLambdaIntegration(
      "ReadIntegration",
      readAlias,
      { payloadFormatVersion: apigatewayv2.PayloadFormatVersion.VERSION_2_0 },
    );
    for (const path of [
      "/api/v1/auth/discord/start",
      "/api/v1/auth/discord/callback",
      "/api/v1/session",
    ]) {
      api.addRoutes({ path, methods: [apigatewayv2.HttpMethod.GET], integration: authIntegration });
    }
    api.addRoutes({
      path: "/api/v1/logout",
      methods: [apigatewayv2.HttpMethod.POST],
      integration: authIntegration,
    });
    api.addRoutes({
      path: "/api/v1/records",
      methods: [apigatewayv2.HttpMethod.GET],
      integration: readIntegration,
    });
    api.addRoutes({
      path: "/api/v1/records/{recordId}",
      methods: [apigatewayv2.HttpMethod.GET],
      integration: readIntegration,
    });
    const stage = api.addStage("DefaultStage", {
      stageName: "$default",
      autoDeploy: true,
      throttle: { rateLimit: 10, burstLimit: 20 },
      accessLogSettings: {
        destination: new apigatewayv2.LogGroupLogDestination(accessLogs),
        format: apigateway.AccessLogFormat.custom(
          JSON.stringify({
            requestId: "$context.requestId",
            routeKey: "$context.routeKey",
            status: "$context.status",
            latency: "$context.responseLatency",
            responseSize: "$context.responseLength",
          }),
        ),
      },
    });
    new CfnOutput(this, "RecordsApiEndpoint", { value: stage.url });
  }

  private httpFunctionWithRole(options: {
    readonly id: string;
    readonly functionName: string;
    readonly handler: string;
    readonly code: lambda.Code;
    readonly timeout: Duration;
    readonly reservedConcurrentExecutions: number;
    readonly environment: Readonly<Record<string, string>>;
    readonly policyStatements: readonly iam.PolicyStatement[];
  }): lambda.Function {
    const logGroup = new logs.LogGroup(this, `${options.id}Logs`, {
      logGroupName: `/aws/lambda/${options.functionName}`,
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(this, `${options.id}Role`, {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: `Least-privilege execution role for ${options.functionName}`,
    });
    logGroup.grantWrite(role);
    for (const statement of options.policyStatements) {
      role.addToPrincipalPolicy(statement);
    }
    return new lambda.Function(this, options.id, {
      functionName: options.functionName,
      architecture: lambda.Architecture.ARM_64,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: options.handler,
      code: options.code,
      memorySize: 512,
      timeout: options.timeout,
      reservedConcurrentExecutions: options.reservedConcurrentExecutions,
      role,
      logGroup,
      environment: options.environment,
      loggingFormat: lambda.LoggingFormat.JSON,
    });
  }

  private functionWithRole(options: {
    readonly id: string;
    readonly functionName: string;
    readonly handler: string;
    readonly code: lambda.Code;
    readonly timeout: Duration;
    readonly reservedConcurrentExecutions?: number;
    readonly sourceTable: dynamodb.ITable;
    readonly archiveTable: dynamodb.ITable;
    readonly statisticsTable: dynamodb.ITable;
    readonly identityParameter: ssm.IStringParameter;
    readonly presentationParameter: ssm.IStringParameter;
    readonly allowScan: boolean;
  }): lambda.Function {
    const logGroup = new logs.LogGroup(this, `${options.id}Logs`, {
      logGroupName: `/aws/lambda/${options.functionName}`,
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const role = new iam.Role(this, `${options.id}Role`, {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: `Least-privilege execution role for ${options.functionName}`,
    });
    logGroup.grantWrite(role);
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: options.allowScan ? ["dynamodb:Query", "dynamodb:Scan"] : ["dynamodb:Query"],
        resources: [options.sourceTable.tableArn],
      }),
    );
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:GetItem", "dynamodb:TransactWriteItems"],
        resources: [options.archiveTable.tableArn],
      }),
    );
    if (options.allowScan) {
      role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ["dynamodb:PutItem"],
          resources: [options.archiveTable.tableArn],
        }),
      );
      role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ["dynamodb:GetItem", "dynamodb:PutItem"],
          resources: [options.statisticsTable.tableArn],
        }),
      );
    }
    role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameters"],
        resources: [
          options.identityParameter.parameterArn,
          options.presentationParameter.parameterArn,
        ],
      }),
    );
    const function_ = new lambda.Function(this, options.id, {
      functionName: options.functionName,
      architecture: lambda.Architecture.ARM_64,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: options.handler,
      code: options.code,
      memorySize: 512,
      timeout: options.timeout,
      reservedConcurrentExecutions: options.reservedConcurrentExecutions,
      role,
      logGroup,
      environment: {
        SOURCE_TABLE_NAME: options.sourceTable.tableName,
        ARCHIVE_TABLE_NAME: options.archiveTable.tableName,
        STATISTICS_TABLE_NAME: options.statisticsTable.tableName,
        IDENTITY_HMAC_PARAMETER_NAME: options.identityParameter.parameterName,
        PRESENTATION_PARAMETER_NAME: options.presentationParameter.parameterName,
      },
      loggingFormat: lambda.LoggingFormat.JSON,
    });
    return function_;
  }
}
