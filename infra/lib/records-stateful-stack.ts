import {
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Validations,
  aws_dynamodb as dynamodb,
  aws_s3 as s3,
  aws_sqs as sqs,
} from "aws-cdk-lib";
import { Construct } from "constructs";

export class RecordsStatefulStack extends Stack {
  public readonly archiveTable: dynamodb.Table;
  public readonly statisticsTable: dynamodb.Table;
  public readonly sessionTable: dynamodb.Table;
  public readonly mediaBucket: s3.Bucket;
  public readonly memorialUploadBucket: s3.Bucket;
  public readonly projectorDlq: sqs.Queue;
  public readonly memorialGenerationDlq: sqs.Queue;
  public readonly memorialGenerationQueue: sqs.Queue;

  public constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    this.archiveTable = this.retainedTable("ArchiveTable", "shittim-chest-production-records");
    this.archiveTable.addGlobalSecondaryIndex({
      indexName: "gsi1",
      partitionKey: { name: "gsi1pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi1sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.archiveTable.addGlobalSecondaryIndex({
      indexName: "gsi2",
      partitionKey: { name: "gsi2pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi2sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.archiveTable.addGlobalSecondaryIndex({
      indexName: "gsi3",
      partitionKey: { name: "gsi3pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi3sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.statisticsTable = this.retainedTable(
      "StatisticsTable",
      "shittim-chest-production-records-statistics",
    );
    this.sessionTable = new dynamodb.Table(this, "SessionTable", {
      tableName: "shittim-chest-production-records-sessions",
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      deletionProtection: true,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
        recoveryPeriodInDays: 35,
      },
      timeToLiveAttribute: "expiresAt",
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const accessLogs = new s3.Bucket(this, "MediaAccessLogs", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.mediaBucket = new s3.Bucket(this, "MediaBucket", {
      bucketName: `shittim-chest-production-records-media-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      serverAccessLogsBucket: accessLogs,
      serverAccessLogsPrefix: "media/",
      versioned: true,
    });
    this.memorialUploadBucket = new s3.Bucket(this, "MemorialUploadBucket", {
      bucketName: `shittim-chest-production-records-memorial-upload-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: "ExpireMemorialUploads",
          abortIncompleteMultipartUploadAfter: Duration.days(1),
          expiration: Duration.days(1),
        },
      ],
      cors: [
        {
          allowedHeaders: ["content-type"],
          allowedMethods: [s3.HttpMethods.POST],
          allowedOrigins: ["https://shittim.pitekusu.dev"],
          maxAge: 300,
        },
      ],
      removalPolicy: RemovalPolicy.RETAIN,
      serverAccessLogsBucket: accessLogs,
      serverAccessLogsPrefix: "memorial-upload/",
      versioned: false,
    });

    this.projectorDlq = new sqs.Queue(this, "ProjectorDlq", {
      queueName: "shittim-chest-production-records-projector-dlq",
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: Duration.days(14),
    });
    this.memorialGenerationDlq = new sqs.Queue(this, "MemorialGenerationDlq", {
      queueName: "shittim-chest-production-records-memorial-generation-dlq",
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: Duration.days(14),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.memorialGenerationQueue = new sqs.Queue(this, "MemorialGenerationQueue", {
      queueName: "shittim-chest-production-records-memorial-generation",
      deadLetterQueue: {
        maxReceiveCount: 4,
        queue: this.memorialGenerationDlq,
      },
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: Duration.days(1),
      visibilityTimeout: Duration.minutes(30),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    Validations.of(this.projectorDlq).acknowledge({
      id: "AwsSolutions-SQS3",
      reason:
        "This queue is the terminal failure destination for the bounded DynamoDB Streams retry policy.",
    });
    Validations.of(this.memorialGenerationDlq).acknowledge({
      id: "AwsSolutions-SQS3",
      reason:
        "This queue is the terminal failure destination for the bounded memorial generation retry policy.",
    });
    Validations.of(accessLogs).acknowledge({
      id: "AwsSolutions-S1",
      reason: "The access-log destination cannot recursively log to itself.",
    });
  }

  private retainedTable(id: string, tableName: string): dynamodb.Table {
    return new dynamodb.Table(this, id, {
      tableName,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      deletionProtection: true,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
        recoveryPeriodInDays: 35,
      },
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }
}
