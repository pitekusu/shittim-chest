import {
  CfnOutput,
  CfnParameter,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Validations,
  aws_certificatemanager as acm,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins,
  aws_route53 as route53,
  aws_route53_targets as targets,
  aws_s3 as s3,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const SPA_REWRITE_CODE = `function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri === "/api" || uri.indexOf("/api/") === 0 ||
      uri.indexOf("/assets/") === 0 || /\\.[^/]+$/.test(uri)) {
    return request;
  }
  request.uri = "/index.html";
  return request;
}`;

export class RecordsEdgeStack extends Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly webBucket: s3.Bucket;

  public constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    const publicHostname = new CfnParameter(this, "RecordsPublicHostname", {
      type: "String",
      allowedPattern:
        "^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    });
    const hostedZoneId = new CfnParameter(this, "RecordsHostedZoneId", {
      type: "String",
      allowedPattern: "^Z[A-Z0-9]+$",
    });
    const hostedZoneName = new CfnParameter(this, "RecordsHostedZoneName", {
      type: "String",
      allowedPattern:
        "^(?=.{1,253}\\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.?$",
    });
    const apiOriginDomain = new CfnParameter(this, "RecordsApiOriginDomain", {
      type: "String",
      allowedPattern: "^[a-z0-9]+\\.execute-api\\.ap-northeast-1\\.amazonaws\\.com$",
    });
    const mediaOriginDomain = new CfnParameter(this, "RecordsMediaOriginDomain", {
      type: "String",
      allowedPattern:
        "^shittim-chest-production-records-media-[0-9]{12}\\.s3\\.ap-northeast-1\\.amazonaws\\.com$",
    });

    const hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, "HostedZone", {
      hostedZoneId: hostedZoneId.valueAsString,
      zoneName: hostedZoneName.valueAsString,
    });
    const certificate = new acm.Certificate(this, "Certificate", {
      domainName: publicHostname.valueAsString,
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    const accessLogs = new s3.Bucket(this, "WebAccessLogs", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: Duration.days(90) }],
      removalPolicy: RemovalPolicy.RETAIN,
    });
    Validations.of(accessLogs).acknowledge({
      id: "AwsSolutions-S1",
      reason: "The access-log destination cannot recursively log to itself.",
    });
    this.webBucket = new s3.Bucket(this, "WebBucket", {
      bucketName: `shittim-chest-production-records-web-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      serverAccessLogsBucket: accessLogs,
      serverAccessLogsPrefix: "web/",
      versioned: true,
    });

    const responseHeaders = new cloudfront.ResponseHeadersPolicy(
      this,
      "SecurityHeaders",
      {
        responseHeadersPolicyName: "shittim-chest-production-records-security",
        securityHeadersBehavior: {
          contentSecurityPolicy: {
            contentSecurityPolicy: [
              "default-src 'self'",
              "base-uri 'self'",
              "connect-src 'self'",
              "font-src 'self'",
              `img-src 'self' data: https://${mediaOriginDomain.valueAsString}`,
              "object-src 'none'",
              "frame-ancestors 'none'",
              "script-src 'self'",
              "style-src 'self'",
            ].join("; "),
            override: true,
          },
          contentTypeOptions: { override: true },
          frameOptions: {
            frameOption: cloudfront.HeadersFrameOption.DENY,
            override: true,
          },
          referrerPolicy: {
            referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER,
            override: true,
          },
          strictTransportSecurity: {
            accessControlMaxAge: Duration.days(365),
            includeSubdomains: false,
            override: true,
            preload: false,
          },
          xssProtection: { protection: true, modeBlock: true, override: true },
        },
        customHeadersBehavior: {
          customHeaders: [
            {
              header: "Permissions-Policy",
              value: "camera=(), geolocation=(), microphone=()",
              override: true,
            },
          ],
        },
      },
    );
    const spaRewrite = new cloudfront.Function(this, "SpaRewrite", {
      functionName: "shittim-chest-production-records-spa-rewrite",
      code: cloudfront.FunctionCode.fromInline(SPA_REWRITE_CODE),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });
    const webOrigin = origins.S3BucketOrigin.withOriginAccessControl(this.webBucket);
    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      certificate,
      comment: "Authenticated Shittim Chest Records web application",
      defaultBehavior: {
        origin: webOrigin,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        compress: true,
        functionAssociations: [
          {
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
            function: spaRewrite,
          },
        ],
        responseHeadersPolicy: responseHeaders,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      additionalBehaviors: {
        "/api/*": {
          origin: new origins.HttpOrigin(apiOriginDomain.valueAsString, {
            protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
          }),
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          compress: true,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: responseHeaders,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        },
        "/assets/*": {
          origin: webOrigin,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          compress: true,
          responseHeadersPolicy: responseHeaders,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        },
      },
      defaultRootObject: "index.html",
      domainNames: [publicHostname.valueAsString],
      enableIpv6: true,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_200,
    });
    Validations.of(this.distribution).acknowledge({
      id: "AwsSolutions-CFR1",
      reason: "The authenticated friend-scale application is intentionally available globally.",
    });
    Validations.of(this.distribution).acknowledge({
      id: "AwsSolutions-CFR2",
      reason:
        "Guild authentication, API throttling, and private origins provide the approved v1 boundary without recurring WAF cost.",
    });
    Validations.of(this.distribution).acknowledge({
      id: "AwsSolutions-CFR3",
      reason:
        "Standard CloudFront logs are disabled so authenticated record identifiers are not persisted in URI logs; aggregate metrics and content-free API logs remain available.",
    });

    new route53.ARecord(this, "Ipv4Alias", {
      zone: hostedZone,
      recordName: publicHostname.valueAsString,
      target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(this.distribution)),
    });
    new route53.AaaaRecord(this, "Ipv6Alias", {
      zone: hostedZone,
      recordName: publicHostname.valueAsString,
      target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(this.distribution)),
    });

    new CfnOutput(this, "RecordsPublicOrigin", {
      value: `https://${publicHostname.valueAsString}`,
    });
    new CfnOutput(this, "RecordsWebBucketName", { value: this.webBucket.bucketName });
    new CfnOutput(this, "RecordsDistributionId", {
      value: this.distribution.distributionId,
    });
    new CfnOutput(this, "RecordsDistributionDomainName", {
      value: this.distribution.distributionDomainName,
    });
    new CfnOutput(this, "RecordsCertificateArn", { value: certificate.certificateArn });
  }
}
