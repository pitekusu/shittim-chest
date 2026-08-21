import { App, Tags, Validations } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { describe, expect, test } from "vitest";

import { RecordsEdgeStack } from "../lib/records-edge-stack";

function synthesize(): {
  readonly checks: AwsSolutionsChecks;
  readonly stack: RecordsEdgeStack;
  readonly template: Template;
} {
  const app = new App();
  const stack = new RecordsEdgeStack(app, "RecordsEdge", {
    env: { account: "000000000000", region: "us-east-1" },
    stackName: "ShittimChest-Prod-RecordsEdge",
    terminationProtection: true,
  });
  Tags.of(stack).add("Project", "shittim-chest");
  Tags.of(stack).add("Environment", "production");
  Tags.of(stack).add("ManagedBy", "cdk");
  const checks = new AwsSolutionsChecks(app, { verbose: true });
  Validations.of(app).addPlugins(checks);
  app.synth();
  return { checks, stack, template: Template.fromStack(stack) };
}

describe("RecordsEdgeStack", () => {
  test("creates a retained private versioned web bucket behind OAC", () => {
    const { stack, template } = synthesize();

    expect(stack.terminationProtection).toBe(true);
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "shittim-chest-production-records-web-000000000000",
      BucketEncryption: Match.anyValue(),
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: "Enabled" },
    });
    template.resourceCountIs("AWS::CloudFront::OriginAccessControl", 1);
    const serialized = JSON.stringify(template.toJSON());
    expect(serialized).not.toContain("WebsiteConfiguration");
    expect(serialized).not.toContain("AWS::CloudFront::CloudFrontOriginAccessIdentity");
  });

  test("separates same-origin API and immutable asset cache behavior", () => {
    const { template } = synthesize();

    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        Aliases: [{ Ref: "RecordsPublicHostname" }],
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({
            PathPattern: "/api/*",
            AllowedMethods: ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
            CachePolicyId: "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
            OriginRequestPolicyId: "b689b0a8-53d0-40ab-baf2-68738e2966ac",
          }),
          Match.objectLike({ PathPattern: "/assets/*" }),
        ]),
        DefaultRootObject: "index.html",
        HttpVersion: "http2and3",
        IPV6Enabled: true,
        PriceClass: "PriceClass_200",
        ViewerCertificate: Match.objectLike({ MinimumProtocolVersion: "TLSv1.2_2021" }),
      }),
    });
  });

  test("rewrites only extensionless SPA routes and preserves API and assets", () => {
    const { template } = synthesize();
    const functions = template.findResources("AWS::CloudFront::Function");
    const code = JSON.stringify(functions);

    expect(code).toContain('uri === \\"/api\\"');
    expect(code).toContain('uri.indexOf(\\"/api/\\")');
    expect(code).toContain('uri.indexOf(\\"/assets/\\")');
    expect(code).toContain('request.uri = \\"/index.html\\"');
  });

  test("sets exact domain parameters, certificate, aliases, and security headers", () => {
    const { template } = synthesize();
    const json = template.toJSON();
    const serialized = JSON.stringify(json);

    for (const parameter of [
      "RecordsPublicHostname",
      "RecordsHostedZoneId",
      "RecordsApiOriginDomain",
      "RecordsMediaOriginDomain",
    ]) {
      expect(json.Parameters[parameter].Default).toBeUndefined();
    }
    template.resourceCountIs("AWS::CertificateManager::Certificate", 1);
    template.resourceCountIs("AWS::Route53::RecordSet", 2);
    template.resourcePropertiesCountIs(
      "AWS::Route53::RecordSet",
      {
        Name: {
          "Fn::Join": ["", [{ Ref: "RecordsPublicHostname" }, "."]],
        },
      },
      2,
    );
    expect(serialized).toContain("ContentSecurityPolicy");
    expect(serialized).not.toContain("unsafe-eval");
    expect(serialized).toContain("StrictTransportSecurity");
    expect(serialized).toContain("Permissions-Policy");
    expect(serialized).toContain("RecordsMediaOriginDomain");
  });

  test("publishes only operational edge outputs", () => {
    const { template } = synthesize();
    const outputs = template.toJSON().Outputs;

    expect(Object.keys(outputs).sort()).toEqual([
      "RecordsCertificateArn",
      "RecordsDistributionDomainName",
      "RecordsDistributionId",
      "RecordsPublicOrigin",
      "RecordsWebBucketName",
    ]);
  });

  test("has no unacknowledged AWS Solutions findings", () => {
    const { checks, stack } = synthesize();

    expect(checks.validateScope(stack).success).toBe(true);
  });
});
