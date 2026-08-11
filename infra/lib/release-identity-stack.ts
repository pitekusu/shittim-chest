import {
  Aws,
  CfnOutput,
  Duration,
  Stack,
  StackProps,
  Validations,
  aws_dynamodb as dynamodb,
  aws_ecr as ecr,
  aws_iam as iam,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const OIDC_AUDIENCE = "sts.amazonaws.com";
const GITHUB_OIDC_PROVIDER_ARN =
  `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com`;
const MAIN_SUBJECT =
  "repo:pitekusu@12059348/shittim-chest@1302516701:ref:refs/heads/main";
const DEPLOY_SUBJECT =
  "repo:pitekusu@12059348/shittim-chest@1302516701:environment:production";
const TOKYO_REGION = "ap-northeast-1";
const COST_REGION = "us-east-1";
const ENHANCED_SCAN_READ_ACTIONS = [
  "inspector2:ListAccountPermissions",
  "inspector2:ListCoverage",
  "inspector2:ListFindings",
] as const;
const RELEASE_STACK_NAMES = [
  "ShittimChest-Prod-Stateful",
  "ShittimChest-Prod-Runtime",
  "ShittimChest-Prod-Operations",
  "ShittimChest-Prod-CostGovernance",
] as const;
const DRIFT_STACK_NAMES = [
  ...RELEASE_STACK_NAMES,
  "ShittimChest-Prod-ReleaseIdentity",
] as const;
// Read-handler permissions from the current CloudFormation resource provider schemas
// used by the five production stacks. Conditional permissions for properties absent
// from the synthesized templates (s3:GetObject, kms:Decrypt/DescribeKey, iam:PassRole)
// remain intentionally excluded so this role cannot read payloads or pass roles.
const DRIFT_RESOURCE_READ_ACTIONS = [
  "apigateway:GET",
  "ce:GetAnomalySubscriptions",
  "ce:ListTagsForResource",
  "cloudwatch:DescribeAlarms",
  "cloudwatch:GetDashboard",
  "cloudwatch:ListTagsForResource",
  "dynamodb:DescribeContinuousBackups",
  "dynamodb:DescribeContributorInsights",
  "dynamodb:DescribeKinesisStreamingDestination",
  "dynamodb:DescribeTable",
  "dynamodb:DescribeTimeToLive",
  "dynamodb:GetResourcePolicy",
  "dynamodb:ListTagsOfResource",
  "ec2:DescribeInternetGateways",
  "ec2:DescribeNetworkAcls",
  "ec2:DescribeRouteTables",
  "ec2:DescribeSecurityGroups",
  "ec2:DescribeSubnets",
  "ec2:DescribeVpcAttribute",
  "ec2:DescribeVpcEncryptionControls",
  "ec2:DescribeVpcs",
  "ec2:DescribeVpnGateways",
  "ecr:DescribeRepositories",
  "ecr:GetLifecyclePolicy",
  "ecr:GetRegistryScanningConfiguration",
  "ecr:GetRepositoryPolicy",
  "ecr:GetSigningConfiguration",
  "ecr:ListTagsForResource",
  "ecs:DescribeClusters",
  "ecs:DescribeServices",
  "ecs:DescribeTaskDefinition",
  "events:DescribeRule",
  "events:ListTagsForResource",
  "events:ListTargetsByRule",
  "iam:GetRole",
  "iam:GetRolePolicy",
  "iam:ListAttachedRolePolicies",
  "iam:ListRolePolicies",
  "lambda:GetAlias",
  "lambda:GetFunction",
  "lambda:GetFunctionCodeSigningConfig",
  "lambda:GetFunctionConfiguration",
  "lambda:GetFunctionEventInvokeConfig",
  "lambda:GetFunctionRecursionConfig",
  "lambda:GetFunctionScalingConfig",
  "lambda:GetPolicy",
  "lambda:GetProvisionedConcurrencyConfig",
  "lambda:GetRuntimeManagementConfig",
  "logs:DescribeIndexPolicies",
  "logs:DescribeLogGroups",
  "logs:DescribeResourcePolicies",
  "logs:GetDataProtectionPolicy",
  "logs:ListTagsForResource",
  "scheduler:GetSchedule",
  "signer:GetSigningProfile",
  "sns:GetDataProtectionPolicy",
  "sns:GetSubscriptionAttributes",
  "sns:GetTopicAttributes",
  "sns:ListSubscriptionsByTopic",
  "sns:ListTagsForResource",
] as const;

export interface ReleaseIdentityStackProps extends StackProps {
  readonly debateTable: dynamodb.ITable;
  readonly imageRepository: ecr.IRepository;
  readonly signingProfileArn: string;
}

export class ReleaseIdentityStack extends Stack {
  public readonly deployRole: iam.Role;
  public readonly driftRole: iam.Role;
  public readonly oidcProvider: iam.IOpenIdConnectProvider;
  public readonly planRole: iam.Role;

  public constructor(scope: Construct, id: string, props: ReleaseIdentityStackProps) {
    super(scope, id, props);

    this.oidcProvider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
      this,
      "GitHubOidcProvider",
      GITHUB_OIDC_PROVIDER_ARN,
    );
    this.planRole = this.githubRole(
      "ReleasePlanRole",
      "ShittimChest-Prod-GitHub-ReleasePlan",
      MAIN_SUBJECT,
      "Build, attest, and prepare immutable production change sets",
    );
    this.deployRole = this.githubRole(
      "ReleaseDeployRole",
      "ShittimChest-Prod-GitHub-ReleaseDeploy",
      DEPLOY_SUBJECT,
      "Execute only approved change sets while holding the deployment fence",
    );
    this.driftRole = this.githubRole(
      "ReleaseDriftRole",
      "ShittimChest-Prod-GitHub-ReleaseDrift",
      MAIN_SUBJECT,
      "Detect CloudFormation drift without changing production resources",
    );

    this.grantPlanPermissions(props);
    this.grantDeployPermissions(props);
    this.grantDriftPermissions();

    new CfnOutput(this, "PlanRoleArn", { value: this.planRole.roleArn });
    new CfnOutput(this, "DeployRoleArn", { value: this.deployRole.roleArn });
    new CfnOutput(this, "DriftRoleArn", { value: this.driftRole.roleArn });
  }

  private githubRole(
    id: string,
    roleName: string,
    subject: string,
    description: string,
  ): iam.Role {
    const principal = new iam.FederatedPrincipal(
      this.oidcProvider.openIdConnectProviderArn,
      {
        StringEquals: {
          "token.actions.githubusercontent.com:aud": OIDC_AUDIENCE,
          "token.actions.githubusercontent.com:sub": subject,
        },
      },
      "sts:AssumeRoleWithWebIdentity",
    );
    return new iam.Role(this, id, {
      assumedBy: principal,
      description,
      maxSessionDuration: Duration.hours(1),
      roleName,
    });
  }

  private grantPlanPermissions(props: ReleaseIdentityStackProps): void {
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImageScanFindings",
          "ecr:DescribeImageSigningStatus",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ],
        resources: [props.imageRepository.repositoryArn],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [...ENHANCED_SCAN_READ_ACTIONS],
        resources: ["*"],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["signer:GetSigningProfile", "signer:SignPayload"],
        resources: [props.signingProfileArn],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["signer:GetRevocationStatus"],
        resources: ["*"],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ce:GetAnomalyMonitors", "ce:ListCostAllocationTags"],
        resources: ["*"],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:DescribeParameters"],
        resources: ["*"],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ec2:DescribeAvailabilityZones"],
        resources: ["*"],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetBucketLocation"],
        resources: this.cdkAssetBucketArns(),
      }),
    );
    const bucketName = `cdk-hnb659fds-assets-${Aws.ACCOUNT_ID}-${TOKYO_REGION}`;
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject"],
        resources: [
          `arn:aws:s3:::${bucketName}/lambda/shittim-chest/*`,
          `arn:aws:s3:::${bucketName}/templates/shittim-chest/*`,
        ],
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "cloudformation:CreateChangeSet",
          "cloudformation:DeleteChangeSet",
          "cloudformation:DescribeChangeSet",
          "cloudformation:DescribeStackResources",
          "cloudformation:DescribeStacks",
          "cloudformation:ListChangeSets",
        ],
        conditions: {
          StringEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID },
          StringLikeIfExists: { "cloudformation:ChangeSetName": "release-*" },
        },
        resources: this.stackArns(RELEASE_STACK_NAMES),
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["iam:PassRole"],
        conditions: {
          StringEquals: { "iam:PassedToService": "cloudformation.amazonaws.com" },
        },
        resources: this.cloudFormationExecutionRoleArns(),
      }),
    );
    this.planRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject"],
        resources: this.cdkAssetObjectArns(),
      }),
    );
    this.acknowledgeRoleWildcards(this.planRole, [
      "AwsSolutions-IAM5[Resource::*]",
      ...this.stackWildcardAcknowledgments(RELEASE_STACK_NAMES),
    ]);
    // CDK 2.261 rejects a validation ID containing the S3 ARN's `:::` even
    // though AwsSolutions emits that exact granular ID. Record the same
    // evidence metadata directly until the validation ID parser supports it.
    this.planRole.node.addMetadata(
      Validations.ACKNOWLEDGED_RULES_METADATA_KEY,
      {
        [`AwsSolutions::AwsSolutions-IAM5[Resource::arn:aws:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-${TOKYO_REGION}/lambda/shittim-chest/*]`]:
          "The plan role can write only content-addressed image-admission bundles in the account's fixed CDK asset bucket.",
        [`AwsSolutions::AwsSolutions-IAM5[Resource::arn:aws:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-${TOKYO_REGION}/templates/shittim-chest/*]`]:
          "The plan role can write only content-addressed oversized CloudFormation templates in the account's fixed CDK asset bucket.",
        ...Object.fromEntries(
          [TOKYO_REGION, COST_REGION].flatMap((region) =>
            this.cdkAssetObjectKeys().map((key) => [
              `AwsSolutions::AwsSolutions-IAM5[Resource::arn:aws:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-${region}/${key}]`,
              "The plan role can read or upload only an exact 64-character content-addressed single-part CDK JSON or ZIP asset; it cannot delete any bootstrap asset.",
            ]),
          ),
        ),
      },
    );
  }

  private grantDeployPermissions(props: ReleaseIdentityStackProps): void {
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "cloudformation:DescribeChangeSet",
          "cloudformation:DescribeStacks",
          "cloudformation:DeleteChangeSet",
          "cloudformation:ExecuteChangeSet",
        ],
        conditions: {
          StringEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID },
          StringLikeIfExists: { "cloudformation:ChangeSetName": "release-*" },
        },
        resources: this.stackArns(RELEASE_STACK_NAMES),
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["cloudformation:DescribeEvents"],
        conditions: {
          StringEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID },
        },
        // Executed change sets can be authorized as either the change set or
        // its owning stack when CloudFormation resolves operation events.
        resources: [
          ...this.stackArns(RELEASE_STACK_NAMES),
          ...this.changeSetArns(),
        ],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:ConditionCheckItem",
          "dynamodb:DeleteItem",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ],
        resources: [props.debateTable.tableArn],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "ecr:BatchGetImage",
          "ecr:DescribeImageScanFindings",
          "ecr:DescribeImageSigningStatus",
          "ecr:DescribeImages",
          "ecr:GetDownloadUrlForLayer",
        ],
        resources: [props.imageRepository.repositoryArn],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecr:GetAuthorizationToken", "signer:GetRevocationStatus"],
        resources: ["*"],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [...ENHANCED_SCAN_READ_ACTIONS],
        resources: ["*"],
      }),
    );
    const bucketName = `cdk-hnb659fds-assets-${Aws.ACCOUNT_ID}-${TOKYO_REGION}`;
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject"],
        resources: [
          `arn:aws:s3:::${bucketName}/lambda/shittim-chest/*`,
          ...this.cdkAssetObjectArns(),
        ],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeServices"],
        resources: [
          `arn:aws:ecs:${TOKYO_REGION}:*:service/shittim-chest-production/shittim-chest-production`,
        ],
        conditions: {
          StringEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID },
        },
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeTaskDefinition"],
        resources: ["*"],
      }),
    );
    this.deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["lambda:GetFunctionConfiguration"],
        resources: [
          `arn:aws:lambda:${TOKYO_REGION}:${Aws.ACCOUNT_ID}:function:shittim-chest-production-image-admission`,
        ],
      }),
    );
    this.acknowledgeRoleWildcards(this.deployRole, [
      "AwsSolutions-IAM5[Resource::*]",
      `AwsSolutions-IAM5[Resource::arn:aws:ecs:${TOKYO_REGION}:*:service/shittim-chest-production/shittim-chest-production]`,
      ...this.changeSetWildcardAcknowledgments(),
      ...this.stackWildcardAcknowledgments(RELEASE_STACK_NAMES),
    ]);
    this.deployRole.node.addMetadata(
      Validations.ACKNOWLEDGED_RULES_METADATA_KEY,
      {
        [`AwsSolutions::AwsSolutions-IAM5[Resource::arn:aws:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-${TOKYO_REGION}/lambda/shittim-chest/*]`]:
          "The deploy role can read only content-addressed image-admission bundles in the account's fixed CDK asset bucket.",
      },
    );
  }

  private grantDriftPermissions(): void {
    this.driftRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "cloudformation:DescribeStacks",
          "cloudformation:DetectStackDrift",
          "cloudformation:DetectStackResourceDrift",
        ],
        conditions: {
          StringEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID },
        },
        resources: this.stackArns(DRIFT_STACK_NAMES),
      }),
    );
    this.driftRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "cloudformation:BatchDescribeTypeConfigurations",
          "cloudformation:DescribeStackDriftDetectionStatus",
        ],
        resources: ["*"],
      }),
    );
    this.driftRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [...DRIFT_RESOURCE_READ_ACTIONS],
        resources: ["*"],
      }),
    );
    this.acknowledgeRoleWildcards(this.driftRole, [
      "AwsSolutions-IAM5[Resource::*]",
      ...this.stackWildcardAcknowledgments(DRIFT_STACK_NAMES),
    ]);
  }

  private stackArns(names: readonly string[]): string[] {
    return names.map((name) => {
      const region = name === "ShittimChest-Prod-CostGovernance" ? COST_REGION : TOKYO_REGION;
      return `arn:aws:cloudformation:${region}:*:stack/${name}/*`;
    });
  }

  private stackWildcardAcknowledgments(names: readonly string[]): string[] {
    return names.map((name) => {
      const region = name === "ShittimChest-Prod-CostGovernance" ? COST_REGION : TOKYO_REGION;
      return `AwsSolutions-IAM5[Resource::arn:aws:cloudformation:${region}:*:stack/${name}/*]`;
    });
  }

  private changeSetArns(): string[] {
    return [TOKYO_REGION, COST_REGION].map(
      (region) => `arn:aws:cloudformation:${region}:*:changeSet/release-*/*`,
    );
  }

  private changeSetWildcardAcknowledgments(): string[] {
    return [TOKYO_REGION, COST_REGION].map(
      (region) =>
        `AwsSolutions-IAM5[Resource::arn:aws:cloudformation:${region}:*:changeSet/release-*/*]`,
    );
  }

  private cloudFormationExecutionRoleArns(): string[] {
    return [TOKYO_REGION, COST_REGION].map(
      (region) =>
        `arn:aws:iam::${Aws.ACCOUNT_ID}:role/cdk-hnb659fds-cfn-exec-role-${Aws.ACCOUNT_ID}-${region}`,
    );
  }

  private cdkAssetBucketArns(): string[] {
    return [TOKYO_REGION, COST_REGION].map(
      (region) => `arn:aws:s3:::cdk-hnb659fds-assets-${Aws.ACCOUNT_ID}-${region}`,
    );
  }

  private cdkAssetObjectKeys(): string[] {
    const contentHash = "?".repeat(64);
    return [`${contentHash}.json`, `${contentHash}.zip`];
  }

  private cdkAssetObjectArns(): string[] {
    return [TOKYO_REGION, COST_REGION].flatMap((region) => {
      const bucketName = `cdk-hnb659fds-assets-${Aws.ACCOUNT_ID}-${region}`;
      return this.cdkAssetObjectKeys().map((key) => `arn:aws:s3:::${bucketName}/${key}`);
    });
  }

  private acknowledgeRoleWildcards(role: iam.Role, ids: string[]): void {
    for (const id of ids) {
      Validations.of(role).acknowledge({
        id,
        reason:
          "The wildcard is limited by a named production resource, aws:ResourceAccount, or an AWS API that does not support resource-level permissions.",
      });
    }
  }
}
