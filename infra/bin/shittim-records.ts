#!/usr/bin/env node
import {
  App,
  CliCredentialsStackSynthesizer,
  Environment,
  Tags,
  Validations,
} from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";

import { RecordsApplicationStack } from "../lib/records-application-stack";
import { RecordsEdgeStack } from "../lib/records-edge-stack";
import { RecordsStatefulStack } from "../lib/records-stateful-stack";

const PRODUCTION_REGION = "ap-northeast-1";
const EDGE_REGION = "us-east-1";

function productionEnvironment(): Environment {
  return { account: process.env.CDK_DEFAULT_ACCOUNT, region: PRODUCTION_REGION };
}

const app = new App();
Tags.of(app).add("Project", "shittim-chest");
Tags.of(app).add("Environment", "production");
Tags.of(app).add("ManagedBy", "cdk");
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));

new RecordsStatefulStack(app, "RecordsStateful", {
  env: productionEnvironment(),
  stackName: "ShittimChest-Prod-RecordsStateful",
  synthesizer: new CliCredentialsStackSynthesizer(),
  terminationProtection: true,
});
new RecordsApplicationStack(app, "RecordsApplication", {
  env: productionEnvironment(),
  stackName: "ShittimChest-Prod-RecordsApplication",
  synthesizer: new CliCredentialsStackSynthesizer(),
  terminationProtection: true,
});
new RecordsEdgeStack(app, "RecordsEdge", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: EDGE_REGION },
  stackName: "ShittimChest-Prod-RecordsEdge",
  synthesizer: new CliCredentialsStackSynthesizer(),
  terminationProtection: true,
});

app.synth();
