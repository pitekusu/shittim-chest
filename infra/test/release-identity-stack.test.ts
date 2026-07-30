import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { ReleaseIdentityStack } from "../lib/release-identity-stack";
import { StatefulStack } from "../lib/stateful-stack";

function synthesize(): {
  readonly identity: ReleaseIdentityStack;
  readonly template: Template;
} {
  const app = new App();
  const env = { account: "000000000000", region: "ap-northeast-1" };
  const stateful = new StatefulStack(app, "Stateful", {
    env,
    stackName: "ShittimChest-Prod-Stateful",
    terminationProtection: true,
  });
  const identity = new ReleaseIdentityStack(app, "ReleaseIdentity", {
    debateTable: stateful.debateTable,
    env,
    imageRepository: stateful.imageRepository,
    signingProfileArn: stateful.signingProfile.attrArn,
    stackName: "ShittimChest-Prod-ReleaseIdentity",
    terminationProtection: true,
  });
  identity.addDependency(stateful);
  for (const stack of [stateful, identity]) {
    Tags.of(stack).add("Project", "shittim-chest");
    Tags.of(stack).add("Environment", "production");
    Tags.of(stack).add("ManagedBy", "cdk");
  }
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { identity, template: Template.fromStack(identity) };
}

describe("ReleaseIdentityStack", () => {
  test("reuses the account GitHub provider for three responsibility-separated roles", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::IAM::OIDCProvider", 0);
    template.resourceCountIs("AWS::IAM::Role", 3);
    expect(JSON.stringify(template.toJSON())).toContain(
      "oidc-provider/token.actions.githubusercontent.com",
    );
    for (const roleName of [
      "ShittimChest-Prod-GitHub-ReleasePlan",
      "ShittimChest-Prod-GitHub-ReleaseDeploy",
      "ShittimChest-Prod-GitHub-ReleaseDrift",
    ]) {
      template.hasResourceProperties("AWS::IAM::Role", {
        RoleName: roleName,
        MaxSessionDuration: 3600,
      });
    }
  });

  test("requires exact audience and immutable repository subjects", () => {
    const { template } = synthesize();
    const roles = Object.values(template.findResources("AWS::IAM::Role"));
    const trusts = JSON.stringify(
      roles.map((role) => role.Properties.AssumeRolePolicyDocument),
    );

    expect(trusts).toContain("token.actions.githubusercontent.com:aud");
    expect(trusts).toContain("sts.amazonaws.com");
    expect(trusts).toContain(
      "repo:pitekusu@12059348/shittim-chest@1302516701:ref:refs/heads/main",
    );
    expect(trusts).toContain(
      "repo:pitekusu@12059348/shittim-chest@1302516701:environment:production",
    );
    expect(trusts).not.toContain("StringLike");
    expect(trusts).not.toContain("repo:pitekusu/shittim-chest:");
  });

  test("keeps plan, deploy, and drift permissions distinct", () => {
    const { template } = synthesize();
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const policyFor = (role: string) =>
      JSON.stringify(
        policies.find((policy) =>
          JSON.stringify(policy.Properties.Roles).includes(role),
        ),
      );
    const plan = policyFor("ReleasePlanRole");
    const deploy = policyFor("ReleaseDeployRole");
    const drift = policyFor("ReleaseDriftRole");

    expect(plan).toContain("ecr:PutImage");
    expect(plan).toContain("ecr:BatchGetImage");
    expect(plan).toContain("inspector2:ListFindings");
    expect(plan).toContain("inspector2:ListAccountPermissions");
    expect(plan).toContain("inspector2:ListCoverage");
    expect(plan).not.toContain("inspector2:Enable");
    expect(plan).not.toContain("inspector2:Disable");
    expect(plan).toContain("cloudformation:CreateChangeSet");
    expect(plan).toContain("cloudformation:DescribeStackResources");
    expect(plan).toContain("ssm:DescribeParameters");
    expect(plan).not.toContain('"ssm:GetParameter"');
    expect(plan).not.toContain('"ssm:GetParameters"');
    expect(plan).toContain("s3:PutObject");
    expect(plan).toContain("ap-northeast-1");
    expect(plan).toContain("us-east-1");
    expect(plan).not.toContain("sts:AssumeRole");
    expect(plan).not.toContain("s3:ListBucket");
    expect(plan).not.toContain("cloudformation:GetTemplate");
    expect(plan).not.toContain("cloudformation:ExecuteChangeSet");
    expect(plan).not.toContain("dynamodb:TransactWriteItems");
    expect(deploy).toContain("cloudformation:ExecuteChangeSet");
    expect(deploy).toContain("cloudformation:DeleteChangeSet");
    expect(deploy).toContain("cloudformation:DescribeEvents");
    expect(deploy).not.toContain("cloudformation:DescribeStackEvents");
    expect(deploy).toContain("changeSet/release-*");
    expect(deploy).toContain("ecr:BatchGetImage");
    expect(deploy).toContain("inspector2:ListFindings");
    expect(deploy).toContain("inspector2:ListAccountPermissions");
    expect(deploy).toContain("inspector2:ListCoverage");
    expect(deploy).not.toContain("inspector2:Enable");
    expect(deploy).not.toContain("inspector2:Disable");
    expect(deploy).toContain("dynamodb:ConditionCheckItem");
    expect(deploy).toContain("dynamodb:DeleteItem");
    expect(deploy).toContain("dynamodb:GetItem");
    expect(deploy).toContain("dynamodb:PutItem");
    expect(deploy).toContain("dynamodb:UpdateItem");
    expect(deploy).not.toContain("dynamodb:TransactGetItems");
    expect(deploy).not.toContain("dynamodb:TransactWriteItems");
    expect(deploy).not.toContain("cloudformation:CreateChangeSet");
    expect(deploy).not.toContain("ecr:PutImage");
    expect(deploy).not.toContain("sts:AssumeRole");
    expect(deploy).not.toContain("cloudformation:GetTemplate");
    expect(drift).toContain("cloudformation:DetectStackDrift");
    expect(drift).toContain("ShittimChest-Prod-ReleaseIdentity");
    expect(drift).not.toContain("cloudformation:ExecuteChangeSet");
    expect(drift).not.toContain("dynamodb:");
    expect(drift).not.toContain("sts:AssumeRole");
    expect(JSON.stringify(template.toJSON())).not.toContain("AdministratorAccess");
    expect(JSON.stringify(template.toJSON())).not.toContain("PowerUserAccess");
  });

  test("publishes CDK assets directly to exact content-addressed keys", () => {
    const { template } = synthesize();
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const policyFor = (role: string) =>
      JSON.stringify(
        policies.find((policy) =>
          JSON.stringify(policy.Properties.Roles).includes(role),
        ),
      );
    const plan = policyFor("ReleasePlanRole");
    const deploy = policyFor("ReleaseDeployRole");
    const contentHashPattern = "?".repeat(64);
    const planPolicy = JSON.parse(plan);
    const statements = planPolicy.Properties.PolicyDocument.Statement as Array<{
      Action: string | string[];
      Condition?: object;
      Resource: string | string[];
    }>;
    const rootAssetStatement = statements.find(
      (statement) =>
        Array.isArray(statement.Action) &&
        statement.Action.includes("s3:PutObject") &&
        JSON.stringify(statement.Resource).includes(contentHashPattern),
    );
    expect(rootAssetStatement).toBeDefined();
    expect(rootAssetStatement?.Action).toEqual(["s3:GetObject", "s3:PutObject"]);
    expect(rootAssetStatement?.Resource).toHaveLength(4);

    for (const region of ["ap-northeast-1", "us-east-1"]) {
      expect(JSON.stringify(rootAssetStatement?.Resource)).toContain(`-${region}`);
      for (const extension of ["json", "zip"]) {
        const objectPattern = `${region}/${contentHashPattern}.${extension}`;
        expect(plan).toContain(objectPattern);
        expect(deploy).toContain(objectPattern);
      }
    }
    expect(plan).not.toContain("s3:DeleteObject");
    expect(plan).not.toContain("cdk-hnb659fds-file-publishing-role");
    expect(plan).not.toContain("cdk-hnb659fds-assets-<AWS::AccountId>-ap-northeast-1/*");
  });

  test("constrains pass-role and production wildcard resources", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "iam:PassRole",
            Condition: {
              StringEquals: {
                "iam:PassedToService": "cloudformation.amazonaws.com",
              },
            },
          }),
        ]),
      },
    });
    const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
    expect(policies).toContain("aws:ResourceAccount");
    expect(policies).not.toContain('"Action":"iam:*"');
    expect(policies).not.toContain('"Action":"cloudformation:*"');
  });

  test("has no unsuppressed AWS Solutions findings", () => {
    expect(() => synthesize()).not.toThrow();
  });
});
