#!/usr/bin/env node
import { App, Environment, Tags, Validations } from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";

import { StatefulStack } from "../lib/stateful-stack";

const PRODUCTION_REGION = "ap-northeast-1";

function productionEnvironment(): Environment {
  const account = process.env.CDK_DEFAULT_ACCOUNT;
  return account === undefined
    ? { region: PRODUCTION_REGION }
    : { account, region: PRODUCTION_REGION };
}

const app = new App();

Tags.of(app).add("Project", "shittim-chest");
Tags.of(app).add("Environment", "production");
Tags.of(app).add("ManagedBy", "cdk");
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));

new StatefulStack(app, "Stateful", {
  env: productionEnvironment(),
  stackName: "ShittimChest-Prod-Stateful",
  terminationProtection: true,
});

app.synth();
