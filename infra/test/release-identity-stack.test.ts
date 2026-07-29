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
    expect(plan).toContain("cloudformation:CreateChangeSet");
    expect(plan).toContain("s3:PutObject");
    expect(plan).not.toContain("cloudformation:ExecuteChangeSet");
    expect(plan).not.toContain("dynamodb:TransactWriteItems");
    expect(deploy).toContain("cloudformation:ExecuteChangeSet");
    expect(deploy).toContain("ecr:BatchGetImage");
    expect(deploy).toContain("dynamodb:ConditionCheckItem");
    expect(deploy).toContain("dynamodb:DeleteItem");
    expect(deploy).toContain("dynamodb:GetItem");
    expect(deploy).toContain("dynamodb:PutItem");
    expect(deploy).toContain("dynamodb:UpdateItem");
    expect(deploy).not.toContain("dynamodb:TransactGetItems");
    expect(deploy).not.toContain("dynamodb:TransactWriteItems");
    expect(deploy).not.toContain("cloudformation:CreateChangeSet");
    expect(deploy).not.toContain("ecr:PutImage");
    expect(drift).toContain("cloudformation:DetectStackDrift");
    expect(drift).toContain("ShittimChest-Prod-ReleaseIdentity");
    expect(drift).not.toContain("cloudformation:ExecuteChangeSet");
    expect(drift).not.toContain("dynamodb:");
    expect(JSON.stringify(template.toJSON())).not.toContain("AdministratorAccess");
    expect(JSON.stringify(template.toJSON())).not.toContain("PowerUserAccess");
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
