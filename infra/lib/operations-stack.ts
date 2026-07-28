import {
  CfnParameter,
  Duration,
  Stack,
  StackProps,
  Validations,
  aws_cloudwatch as cloudwatch,
  aws_cloudwatch_actions as cloudwatchActions,
  aws_dynamodb as dynamodb,
  aws_ecs as ecs,
  aws_events as events,
  aws_events_targets as eventTargets,
  aws_sns as sns,
  aws_sns_subscriptions as subscriptions,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const METRIC_NAMESPACE = "ShittimChest/Prod";
const METRIC_PERIOD = Duration.minutes(1);
const OPERATOR_EMAIL_PATTERN =
  "^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\\.[A-Za-z0-9-]+)+$";

export interface OperationsStackProps extends StackProps {
  readonly cluster: ecs.ICluster;
  readonly debateTable: dynamodb.Table;
  readonly service: ecs.FargateService;
}

export class OperationsStack extends Stack {
  public readonly alertTopic: sns.Topic;
  public readonly criticalAlarm: cloudwatch.CompositeAlarm;
  public readonly dashboard: cloudwatch.Dashboard;
  public readonly taskStoppedRule: events.Rule;
  public readonly warningAlarm: cloudwatch.CompositeAlarm;

  public constructor(scope: Construct, id: string, props: OperationsStackProps) {
    super(scope, id, props);

    const operatorEmail = new CfnParameter(this, "OperatorNotificationEmail", {
      allowedPattern: OPERATOR_EMAIL_PATTERN,
      description:
        "Private operator email for runtime alarms; reuse for Budget and Cost Anomaly Detection",
      noEcho: true,
      type: "String",
    });

    this.alertTopic = new sns.Topic(this, "RuntimeAlertTopic", {
      displayName: "The Shittim Chest production operations",
      enforceSSL: true,
      topicName: "shittim-chest-production-operations",
    });
    this.alertTopic.addSubscription(
      new subscriptions.EmailSubscription(operatorEmail.valueAsString),
    );
    Validations.of(this.alertTopic).acknowledge({
      id: "AwsSolutions-SNS2",
      reason:
        "The topic carries content-free CloudWatch alarm and filtered ECS lifecycle metadata only. A customer KMS key would add cost and key-policy operations without protecting user content.",
    });

    const runtimeState = this.customMetric("RuntimeStateCode", "reconciler");
    const desiredCount = this.customMetric("RuntimeDesiredCount", "reconciler");
    const runningCount = this.customMetric("EcsRunningCount", "reconciler");
    const pendingCount = this.customMetric("EcsPendingCount", "reconciler");
    const ingressPending = this.customMetric("IngressPending", "reconciler");
    const outboxPending = this.customMetric("OutboxPending", "reconciler");
    const reconcilerFailed = this.customMetric(
      "ReconcilerFailed",
      "reconciler",
      "Sum",
    );
    const statusPublishFailed = this.customMetric(
      "StatusPublishFailed",
      "reconciler",
      "Sum",
    );
    const botReady = this.customMetric("BotReady", "runtime");
    const heartbeatAge = this.customMetric("HeartbeatAgeSeconds", "runtime");
    const dynamodbThrottles = new cloudwatch.MathExpression({
      expression: "txWrite + txGet + getItem + putItem + query + scan",
      label: "DynamoDB throttled requests",
      period: METRIC_PERIOD,
      usingMetrics: {
        getItem: props.debateTable.metricThrottledRequestsForOperation("GetItem", {
          period: METRIC_PERIOD,
          statistic: "Sum",
        }),
        putItem: props.debateTable.metricThrottledRequestsForOperation("PutItem", {
          period: METRIC_PERIOD,
          statistic: "Sum",
        }),
        query: props.debateTable.metricThrottledRequestsForOperation("Query", {
          period: METRIC_PERIOD,
          statistic: "Sum",
        }),
        scan: props.debateTable.metricThrottledRequestsForOperation("Scan", {
          period: METRIC_PERIOD,
          statistic: "Sum",
        }),
        txGet: props.debateTable.metricThrottledRequestsForOperation(
          "TransactGetItems",
          { period: METRIC_PERIOD, statistic: "Sum" },
        ),
        txWrite: props.debateTable.metricThrottledRequestsForOperation(
          "TransactWriteItems",
          { period: METRIC_PERIOD, statistic: "Sum" },
        ),
      },
    });

    const runtimeActive = new cloudwatch.MathExpression({
      expression:
        "IF(((state == 2) OR (state == 4) OR (state == 7)) AND (running >= 1), 1, 0)",
      label: "Runtime active with a running task",
      period: METRIC_PERIOD,
      usingMetrics: { running: runningCount, state: runtimeState },
    });
    const runtimeActiveAlarm = this.metricAlarm("RuntimeActive", runtimeActive, {
      datapointsToAlarm: 2,
      evaluationPeriods: 3,
      threshold: 0.5,
    });
    const botNotReadyAlarm = this.metricAlarm("BotNotReady", botReady, {
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      datapointsToAlarm: 2,
      evaluationPeriods: 3,
      threshold: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    const heartbeatStaleAlarm = this.metricAlarm("HeartbeatStale", heartbeatAge, {
      datapointsToAlarm: 2,
      evaluationPeriods: 3,
      threshold: 60,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });

    const ingressMismatch = new cloudwatch.MathExpression({
      expression: "IF((ingress > 0) AND (desired < 1), 1, 0)",
      label: "Pending ingress while desired count is zero",
      period: METRIC_PERIOD,
      usingMetrics: { desired: desiredCount, ingress: ingressPending },
    });
    const ingressMismatchAlarm = this.metricAlarm(
      "IngressRuntimeMismatch",
      ingressMismatch,
      { datapointsToAlarm: 2, evaluationPeriods: 3, threshold: 0.5 },
    );
    const idleStillRunning = new cloudwatch.MathExpression({
      expression: "IF((state == 5) AND (desired >= 1), 1, 0)",
      label: "Idle runtime still desired",
      period: METRIC_PERIOD,
      usingMetrics: { desired: desiredCount, state: runtimeState },
    });
    const idleStillRunningAlarm = this.metricAlarm(
      "IdleStillRunning",
      idleStillRunning,
      { datapointsToAlarm: 35, evaluationPeriods: 35, threshold: 0.5 },
    );
    const reconcilerFailedAlarm = this.metricAlarm(
      "ReconcilerFailure",
      reconcilerFailed,
      { datapointsToAlarm: 1, evaluationPeriods: 3, threshold: 0.5 },
    );
    const statusPublishFailedAlarm = this.metricAlarm(
      "StatusPublishFailure",
      statusPublishFailed,
      { datapointsToAlarm: 2, evaluationPeriods: 3, threshold: 0.5 },
    );
    const outboxBacklogAlarm = this.metricAlarm("OutboxBacklog", outboxPending, {
      datapointsToAlarm: 10,
      evaluationPeriods: 10,
      threshold: 0,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    const dynamodbThrottleAlarm = this.metricAlarm(
      "DynamoDbThrottle",
      dynamodbThrottles,
      {
        datapointsToAlarm: 1,
        evaluationPeriods: 3,
        threshold: 0,
        treatMissingData: cloudwatch.TreatMissingData.IGNORE,
      },
    );

    this.criticalAlarm = new cloudwatch.CompositeAlarm(this, "CriticalAlarm", {
      alarmDescription: "Critical runtime health, convergence, or reconciler failure",
      alarmRule: cloudwatch.AlarmRule.anyOf(
        cloudwatch.AlarmRule.allOf(
          cloudwatch.AlarmRule.fromAlarm(
            runtimeActiveAlarm,
            cloudwatch.AlarmState.ALARM,
          ),
          cloudwatch.AlarmRule.anyOf(
            cloudwatch.AlarmRule.fromAlarm(
              botNotReadyAlarm,
              cloudwatch.AlarmState.ALARM,
            ),
            cloudwatch.AlarmRule.fromAlarm(
              heartbeatStaleAlarm,
              cloudwatch.AlarmState.ALARM,
            ),
          ),
        ),
        cloudwatch.AlarmRule.fromAlarm(
          ingressMismatchAlarm,
          cloudwatch.AlarmState.ALARM,
        ),
        cloudwatch.AlarmRule.fromAlarm(
          idleStillRunningAlarm,
          cloudwatch.AlarmState.ALARM,
        ),
        cloudwatch.AlarmRule.fromAlarm(
          reconcilerFailedAlarm,
          cloudwatch.AlarmState.ALARM,
        ),
      ),
      compositeAlarmName: "shittim-chest-production-critical",
    });
    this.warningAlarm = new cloudwatch.CompositeAlarm(this, "WarningAlarm", {
      alarmDescription: "Warning status publication, outbox, or DynamoDB condition",
      alarmRule: cloudwatch.AlarmRule.anyOf(
        cloudwatch.AlarmRule.fromAlarm(
          statusPublishFailedAlarm,
          cloudwatch.AlarmState.ALARM,
        ),
        cloudwatch.AlarmRule.fromAlarm(
          outboxBacklogAlarm,
          cloudwatch.AlarmState.ALARM,
        ),
        cloudwatch.AlarmRule.fromAlarm(
          dynamodbThrottleAlarm,
          cloudwatch.AlarmState.ALARM,
        ),
      ),
      compositeAlarmName: "shittim-chest-production-warning",
    });
    const topicAction = new cloudwatchActions.SnsAction(this.alertTopic);
    this.criticalAlarm.addAlarmAction(topicAction);
    this.warningAlarm.addAlarmAction(topicAction);

    this.taskStoppedRule = new events.Rule(this, "AbnormalTaskStoppedRule", {
      description: "Notify only abnormal singleton runtime task stops",
      eventPattern: {
        detail: {
          clusterArn: [props.cluster.clusterArn],
          group: [`service:${props.service.serviceName}`],
          lastStatus: ["STOPPED"],
          stopCode: [
            "TaskFailedToStart",
            "EssentialContainerExited",
            "SpotInterruption",
            "TerminationNotice",
          ],
        },
        detailType: ["ECS Task State Change"],
        source: ["aws.ecs"],
      },
      ruleName: "shittim-chest-production-abnormal-task-stopped",
    });
    this.taskStoppedRule.addTarget(
      new eventTargets.SnsTopic(this.alertTopic, {
        message: events.RuleTargetInput.fromObject({
          cluster_arn: events.EventField.fromPath("$.detail.clusterArn"),
          event: "ecs_task_stopped_abnormally",
          exit_codes: events.EventField.fromPath("$.detail.containers[*].exitCode"),
          schema_version: 1,
          stop_code: events.EventField.fromPath("$.detail.stopCode"),
          stopped_reason: events.EventField.fromPath("$.detail.stoppedReason"),
          task_arn: events.EventField.fromPath("$.detail.taskArn"),
          time: events.EventField.time,
        }),
      }),
    );

    const cpu = props.service.metricCpuUtilization({ period: METRIC_PERIOD });
    const memory = props.service.metricMemoryUtilization({ period: METRIC_PERIOD });
    this.dashboard = new cloudwatch.Dashboard(this, "OperationsDashboard", {
      dashboardName: "shittim-chest-production",
      periodOverride: cloudwatch.PeriodOverride.INHERIT,
      start: "-PT8H",
    });
    this.dashboard.addWidgets(
      new cloudwatch.TextWidget({
        height: 2,
        markdown:
          "# The Shittim Chest production\nTask count 0 and missing runtime health metrics are normal while STOPPED/IDLE. RuntimeStateCode: 0 unknown, 1 stopped, 2 starting, 3 ready, 4 busy, 5 idle, 6 stopping, 7 degraded.",
        width: 24,
      }),
      new cloudwatch.AlarmStatusWidget({
        alarms: [this.criticalAlarm, this.warningAlarm],
        height: 4,
        title: "Operations alarms",
        width: 24,
      }),
      new cloudwatch.GraphWidget({
        height: 6,
        left: [runtimeState, desiredCount, runningCount, pendingCount],
        leftYAxis: { min: 0 },
        title: "Runtime state and ECS convergence",
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        height: 6,
        left: [ingressPending, outboxPending],
        leftYAxis: { min: 0 },
        title: "Durable work backlog",
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        height: 6,
        left: [botReady],
        leftYAxis: { max: 1, min: 0 },
        right: [heartbeatAge],
        rightYAxis: { min: 0 },
        title: "Bot readiness and heartbeat age",
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        height: 6,
        left: [reconcilerFailed, statusPublishFailed],
        leftYAxis: { min: 0 },
        title: "Control-plane failures",
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        height: 6,
        left: [cpu, memory],
        leftYAxis: { max: 100, min: 0 },
        title: "ECS CPU and memory utilization",
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        height: 6,
        left: [dynamodbThrottles],
        leftYAxis: { min: 0 },
        title: "DynamoDB throttled requests",
        width: 12,
      }),
    );
  }

  private customMetric(
    metricName: string,
    service: "reconciler" | "runtime",
    statistic = "Maximum",
  ): cloudwatch.Metric {
    return new cloudwatch.Metric({
      dimensionsMap: { Service: service },
      metricName,
      namespace: METRIC_NAMESPACE,
      period: METRIC_PERIOD,
      statistic,
    });
  }

  private metricAlarm(
    id: string,
    metric: cloudwatch.IMetric,
    options: {
      readonly comparisonOperator?: cloudwatch.ComparisonOperator;
      readonly datapointsToAlarm: number;
      readonly evaluationPeriods: number;
      readonly threshold: number;
      readonly treatMissingData?: cloudwatch.TreatMissingData;
    },
  ): cloudwatch.Alarm {
    return new cloudwatch.Alarm(this, `${id}Alarm`, {
      actionsEnabled: false,
      alarmName: `shittim-chest-production-${id
        .replaceAll(/([a-z])([A-Z])/g, "$1-$2")
        .toLowerCase()}`,
      comparisonOperator:
        options.comparisonOperator ??
        cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      datapointsToAlarm: options.datapointsToAlarm,
      evaluationPeriods: options.evaluationPeriods,
      metric,
      threshold: options.threshold,
      treatMissingData:
        options.treatMissingData ?? cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
