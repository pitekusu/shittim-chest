import {
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  aws_dynamodb as dynamodb,
  aws_ecr as ecr,
  aws_signer as signer,
} from "aws-cdk-lib";
import { Construct } from "constructs";

export class StatefulStack extends Stack {
  public readonly debateTable: dynamodb.Table;
  public readonly imageRepository: ecr.Repository;
  public readonly scanningConfiguration: ecr.CfnRegistryScanningConfiguration;
  public readonly signingConfiguration: ecr.CfnSigningConfiguration;
  public readonly signingProfile: signer.CfnSigningProfile;

  public constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    this.debateTable = new dynamodb.Table(this, "DebateTable", {
      tableName: "shittim-chest-production",
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

    this.debateTable.addGlobalSecondaryIndex({
      indexName: "gsi1",
      partitionKey: { name: "gsi1pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi1sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.debateTable.addGlobalSecondaryIndex({
      indexName: "gsi2",
      partitionKey: { name: "gsi2pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi2sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.imageRepository = new ecr.Repository(this, "ApplicationRepository", {
      repositoryName: "shittim-chest",
      // Basic scan-on-push stays off: the registry-level enhanced scanning
      // configuration below covers this repository instead.
      imageScanOnPush: false,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      lifecycleRules: [
        {
          description: "Expire untagged images after 14 days",
          maxImageAge: Duration.days(14),
          rulePriority: 1,
          tagStatus: ecr.TagStatus.UNTAGGED,
        },
        {
          description: "Expire candidate images after 14 days",
          maxImageAge: Duration.days(14),
          rulePriority: 2,
          tagPatternList: ["candidate-*"],
          tagStatus: ecr.TagStatus.TAGGED,
        },
      ],
      removalPolicy: RemovalPolicy.RETAIN,
      emptyOnDelete: false,
    });

    this.scanningConfiguration = new ecr.CfnRegistryScanningConfiguration(
      this,
      "EnhancedScanningConfiguration",
      {
        scanType: "ENHANCED",
        rules: [
          {
            scanFrequency: "SCAN_ON_PUSH",
            repositoryFilters: [
              {
                filter: "shittim-chest",
                filterType: "WILDCARD_MATCH",
              },
            ],
          },
        ],
      },
    );
    this.scanningConfiguration.node.addDependency(this.imageRepository);

    this.signingProfile = new signer.CfnSigningProfile(this, "ImageSigningProfile", {
      platformId: "Notation-OCI-SHA384-ECDSA",
      profileName: "shittim_chest_ecr",
      signatureValidityPeriod: {
        type: "MONTHS",
        value: 135,
      },
    });
    this.signingProfile.applyRemovalPolicy(RemovalPolicy.RETAIN);

    this.signingConfiguration = new ecr.CfnSigningConfiguration(
      this,
      "ManagedSigningConfiguration",
      {
        rules: [
          {
            signingProfileArn: this.signingProfile.attrArn,
            repositoryFilters: [
              {
                filter: "shittim-chest",
                filterType: "WILDCARD_MATCH",
              },
            ],
          },
        ],
      },
    );
    this.signingConfiguration.node.addDependency(this.imageRepository);
  }
}
