#!/usr/bin/env node
import { App, Environment, Tags, Validations } from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";

import { RuntimeStack } from "../lib/runtime-stack";
import { StatefulStack } from "../lib/stateful-stack";

const PRODUCTION_REGION = "ap-northeast-1";

function productionEnvironment(): Environment {
  return { region: PRODUCTION_REGION };
}

const app = new App();

Tags.of(app).add("Project", "shittim-chest");
Tags.of(app).add("Environment", "production");
Tags.of(app).add("ManagedBy", "cdk");
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));

const stateful = new StatefulStack(app, "Stateful", {
  env: productionEnvironment(),
  stackName: "ShittimChest-Prod-Stateful",
  terminationProtection: true,
});
const runtime = new RuntimeStack(app, "Runtime", {
  debateTable: stateful.debateTable,
  env: productionEnvironment(),
  imageRepository: stateful.imageRepository,
  stackName: "ShittimChest-Prod-Runtime",
});
runtime.addDependency(stateful);

app.synth();
