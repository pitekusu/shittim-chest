import {
  CfnParameter,
  Stack,
  StackProps,
  aws_budgets as budgets,
  aws_ce as ce,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const ACCOUNT_BUDGET_USD = 30;
const ANOMALY_IMPACT_THRESHOLD_USD = 10;
const OPERATOR_EMAIL_PATTERN =
  "^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\\.[A-Za-z0-9-]+)+$";
const PROJECT_BUDGET_USD = 20;
const SERVICE_MONITOR_ARN_PATTERN =
  "^arn:(aws|aws-us-gov|aws-cn|aws-iso|aws-iso-b):ce::[0-9]{12}:anomalymonitor/[A-Za-z0-9-]+$";

export class CostGovernanceStack extends Stack {
  public readonly accountBudget: budgets.CfnBudget;
  public readonly anomalySubscription: ce.CfnAnomalySubscription;
  public readonly projectBudget: budgets.CfnBudget;

  public constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const operatorEmail = new CfnParameter(this, "OperatorNotificationEmail", {
      allowedPattern: OPERATOR_EMAIL_PATTERN,
      description:
        "Private operator email; must match the OperationsStack notification email",
      noEcho: true,
      type: "String",
    });
    const serviceMonitorArn = new CfnParameter(
      this,
      "ExistingServiceAnomalyMonitorArn",
      {
        allowedPattern: SERVICE_MONITOR_ARN_PATTERN,
        description:
          "Existing AWS managed SERVICE Cost Anomaly Detection monitor ARN",
        type: "String",
      },
    );

    const notifications = this.budgetNotifications(operatorEmail.valueAsString);
    this.projectBudget = new budgets.CfnBudget(this, "ProjectBudget", {
      budget: {
        budgetLimit: { amount: PROJECT_BUDGET_USD, unit: "USD" },
        budgetName: "shittim-chest-production-project",
        budgetType: "COST",
        filterExpression: {
          tags: {
            key: "user:Project",
            matchOptions: ["EQUALS"],
            values: ["shittim-chest"],
          },
        },
        metrics: ["NET_UNBLENDED_COST"],
        timeUnit: "MONTHLY",
      },
      notificationsWithSubscribers: notifications,
      resourceTags: this.resourceTags(),
    });
    this.accountBudget = new budgets.CfnBudget(this, "AccountBudget", {
      budget: {
        budgetLimit: { amount: ACCOUNT_BUDGET_USD, unit: "USD" },
        budgetName: "shittim-chest-production-account",
        budgetType: "COST",
        metrics: ["NET_UNBLENDED_COST"],
        timeUnit: "MONTHLY",
      },
      notificationsWithSubscribers: notifications,
      resourceTags: this.resourceTags(),
    });

    this.anomalySubscription = new ce.CfnAnomalySubscription(
      this,
      "CostAnomalySubscription",
      {
        frequency: "DAILY",
        monitorArnList: [serviceMonitorArn.valueAsString],
        resourceTags: this.resourceTags(),
        subscribers: [
          {
            address: operatorEmail.valueAsString,
            type: "EMAIL",
          },
        ],
        subscriptionName: "shittim-chest-production-cost-anomalies",
        thresholdExpression: JSON.stringify({
          Dimensions: {
            Key: "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
            MatchOptions: ["GREATER_THAN_OR_EQUAL"],
            Values: [String(ANOMALY_IMPACT_THRESHOLD_USD)],
          },
        }),
      },
    );
  }

  private budgetNotifications(
    emailAddress: string,
  ): budgets.CfnBudget.NotificationWithSubscribersProperty[] {
    const subscriber: budgets.CfnBudget.SubscriberProperty = {
      address: emailAddress,
      subscriptionType: "EMAIL",
    };
    const notification = (
      notificationType: "ACTUAL" | "FORECASTED",
      threshold: number,
    ): budgets.CfnBudget.NotificationWithSubscribersProperty => ({
      notification: {
        comparisonOperator: "GREATER_THAN",
        notificationType,
        threshold,
        thresholdType: "PERCENTAGE",
      },
      subscribers: [subscriber],
    });
    return [
      notification("ACTUAL", 80),
      notification("ACTUAL", 100),
      notification("FORECASTED", 100),
    ];
  }

  private resourceTags(): Array<{ key: string; value: string }> {
    return [
      { key: "Environment", value: "production" },
      { key: "ManagedBy", value: "cdk" },
      { key: "Project", value: "shittim-chest" },
    ];
  }
}
