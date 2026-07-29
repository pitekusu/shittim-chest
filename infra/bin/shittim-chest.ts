#!/usr/bin/env node
import { App, Environment, Tags, Validations } from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";

import { CostGovernanceStack } from "../lib/cost-governance-stack";
import { OperationsStack } from "../lib/operations-stack";
import { ReleaseIdentityStack } from "../lib/release-identity-stack";
import { RuntimeStack } from "../lib/runtime-stack";
import { StatefulStack } from "../lib/stateful-stack";

const COST_MANAGEMENT_REGION = "us-east-1";
const PRODUCTION_REGION = "ap-northeast-1";

function productionEnvironment(): Environment {
  return { account: process.env.CDK_DEFAULT_ACCOUNT, region: PRODUCTION_REGION };
}

function costManagementEnvironment(): Environment {
  return { account: process.env.CDK_DEFAULT_ACCOUNT, region: COST_MANAGEMENT_REGION };
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
const releaseIdentity = new ReleaseIdentityStack(app, "ReleaseIdentity", {
  debateTable: stateful.debateTable,
  env: productionEnvironment(),
  imageRepository: stateful.imageRepository,
  signingProfileArn: stateful.signingProfile.attrArn,
  stackName: "ShittimChest-Prod-ReleaseIdentity",
  terminationProtection: true,
});
releaseIdentity.addDependency(stateful);
const runtime = new RuntimeStack(app, "Runtime", {
  debateTable: stateful.debateTable,
  env: productionEnvironment(),
  imageRepository: stateful.imageRepository,
  signingProfileArn: stateful.signingProfile.attrArn,
  stackName: "ShittimChest-Prod-Runtime",
});
runtime.addDependency(stateful);
const operations = new OperationsStack(app, "Operations", {
  cluster: runtime.cluster,
  debateTable: stateful.debateTable,
  env: productionEnvironment(),
  service: runtime.service,
  stackName: "ShittimChest-Prod-Operations",
});
operations.addDependency(runtime);
new CostGovernanceStack(app, "CostGovernance", {
  env: costManagementEnvironment(),
  stackName: "ShittimChest-Prod-CostGovernance",
});

app.synth();
