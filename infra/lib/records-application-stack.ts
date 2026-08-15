import {
  CfnParameter,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Validations,
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

export class RecordsApplicationStack extends Stack {
  public readonly projectorFunction: lambda.Function;
  public readonly backfillFunction: lambda.Function;

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
      environment: {
        SOURCE_TABLE_NAME: options.sourceTable.tableName,
        ARCHIVE_TABLE_NAME: options.archiveTable.tableName,
        STATISTICS_TABLE_NAME: options.statisticsTable.tableName,
        IDENTITY_HMAC_PARAMETER_NAME: options.identityParameter.parameterName,
        PRESENTATION_PARAMETER_NAME: options.presentationParameter.parameterName,
      },
      loggingFormat: lambda.LoggingFormat.JSON,
    });
    function_.node.addDependency(logGroup);
    return function_;
  }
}
