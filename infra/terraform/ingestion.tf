# Ingestion: IoT Rules Engine to SQS.
#
# The rules land messages in a queue rather than calling the backend directly, and that
# indirection is the point. A backend deploy, restart, crash, or slow database becomes a
# growing queue instead of lost telemetry, and the plant floor never notices. Machines publish
# on their own schedule and the backend consumes on its own — neither has to be available at
# the same moment as the other.
#
# Telemetry and registration are separated because they are different work: registration is
# rare and changes the fleet's shape, telemetry is constant and high volume. Splitting them
# means a backlog of readings cannot delay a new machine being recognised.

locals {
  telemetry_topic_filter    = "${var.mqtt_topic_prefix}/+/+/telemetry"
  registration_topic_filter = "${var.mqtt_topic_prefix}/+/+/registration"
}

# --- queues -----------------------------------------------------------------------------

resource "aws_sqs_queue" "telemetry_deadletter" {
  name                      = "${local.name_prefix}-telemetry-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum: a poison message is a bug to read, not to lose
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "telemetry" {
  name = "${local.name_prefix}-telemetry"

  # Long enough that a weekend of backend downtime does not cost plant-floor readings.
  message_retention_seconds  = var.telemetry_retention_days * 86400
  visibility_timeout_seconds = 30
  sqs_managed_sse_enabled    = true

  # Long polling. Cuts empty receives, which is both the cost and the latency win over
  # polling in a tight loop.
  receive_wait_time_seconds = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.telemetry_deadletter.arn
    # A batch that fails five times is failing for a reason retrying will not fix. Parking it
    # keeps one malformed payload from blocking every reading behind it.
    maxReceiveCount = 5
  })
}

resource "aws_sqs_queue" "registration_deadletter" {
  name                      = "${local.name_prefix}-registration-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "registration" {
  name = "${local.name_prefix}-registration"

  message_retention_seconds  = var.telemetry_retention_days * 86400
  visibility_timeout_seconds = 30
  sqs_managed_sse_enabled    = true
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.registration_deadletter.arn
    maxReceiveCount     = 5
  })
}

# --- the role the Rules Engine assumes -------------------------------------------------

data "aws_iam_policy_document" "iot_rule_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }

    # Confused-deputy guard: without these, any other account able to name this role's ARN
    # could have their IoT service push messages into these queues.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["${local.iot_arn_prefix}:rule/*"]
    }
  }
}

resource "aws_iam_role" "iot_rule" {
  name               = "${local.name_prefix}-iot-rule"
  assume_role_policy = data.aws_iam_policy_document.iot_rule_assume.json
}

data "aws_iam_policy_document" "iot_rule" {
  statement {
    sid     = "SendToIngestionQueues"
    actions = ["sqs:SendMessage"]
    resources = [
      aws_sqs_queue.telemetry.arn,
      aws_sqs_queue.registration.arn,
    ]
  }

  statement {
    sid       = "ReportRuleFailures"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.iot_rule_errors.arn}:*"]
  }
}

resource "aws_iam_role_policy" "iot_rule" {
  name   = "${local.name_prefix}-iot-rule"
  role   = aws_iam_role.iot_rule.id
  policy = data.aws_iam_policy_document.iot_rule.json
}

# Where a rule reports its own failures. Without an error action a rule that cannot reach its
# queue fails silently, which on a monitoring system reads as a fleet that has gone quiet.
resource "aws_cloudwatch_log_group" "iot_rule_errors" {
  name              = "/aws/iot/${local.name_prefix}/rule-errors"
  retention_in_days = 14
}

# --- rules ------------------------------------------------------------------------------

resource "aws_iot_topic_rule" "telemetry" {
  name        = replace("${local.name_prefix}_telemetry", "-", "_")
  description = "Route every asset's telemetry batches to the ingestion queue."
  enabled     = true

  # timestamp() is the broker's own clock, stamped on arrival. Every other time in the payload
  # comes from the machine, and machine clocks drift, get reset, and occasionally lie by years.
  # Keeping one timestamp the device cannot influence is what makes that detectable.
  sql         = "SELECT *, timestamp() AS ingestedAt FROM '${local.telemetry_topic_filter}'"
  sql_version = "2016-03-23"

  sqs {
    queue_url  = aws_sqs_queue.telemetry.url
    role_arn   = aws_iam_role.iot_rule.arn
    use_base64 = false
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.iot_rule_errors.name
      role_arn       = aws_iam_role.iot_rule.arn
    }
  }
}

resource "aws_iot_topic_rule" "registration" {
  name        = replace("${local.name_prefix}_registration", "-", "_")
  description = "Route asset registrations to the registry queue."
  enabled     = true

  sql         = "SELECT *, timestamp() AS ingestedAt FROM '${local.registration_topic_filter}'"
  sql_version = "2016-03-23"

  sqs {
    queue_url  = aws_sqs_queue.registration.url
    role_arn   = aws_iam_role.iot_rule.arn
    use_base64 = false
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.iot_rule_errors.name
      role_arn       = aws_iam_role.iot_rule.arn
    }
  }
}
