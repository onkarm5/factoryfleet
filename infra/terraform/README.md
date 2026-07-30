# FactoryFleet infrastructure

Terraform for the AWS side of the fleet: the MQTT broker and device identities (IoT Core), the
ingestion queues (SQS), the database (RDS PostgreSQL), the backend runtime (ECS Fargate) and
alert delivery (SNS).

## Current state

This is the provider and variable baseline only — **no resources are declared yet, so
`terraform apply` creates nothing and costs nothing.** Resources land with the milestones
below.

| Milestone | Resources |
|---|---|
| 3 | IoT Core thing type, per-asset things, policies and certificates, Rules Engine → SQS telemetry and result queues |
| 4 | RDS PostgreSQL instance, subnet group, security groups, Secrets Manager secret |
| 6 | SNS topic and subscription for alerts |
| 7 | ECR repository, ECS cluster, Fargate service, task role and execution role, CloudWatch log groups |

## Usage

Terraform reads credentials from your local AWS configuration — set up a profile with
`aws configure` (or export `AWS_PROFILE`) before running. No credentials belong in this
repository.

```bash
terraform init
terraform validate
terraform fmt -check

cp example.tfvars terraform.tfvars   # then edit
terraform plan  -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

Tear down when you are done experimenting, so an idle demo environment stops accruing cost:

```bash
terraform destroy -var-file=terraform.tfvars
```

## Cost note

The intended footprint stays inside or close to the AWS free tier while developing: IoT Core
bills per message and per connection-minute, SQS has a large free-tier request allowance, and
RDS `db.t4g.micro` plus a single small Fargate task are the two line items worth watching.
`terraform destroy` between sessions is the cheapest way to work.

## Conventions

- Every resource is prefixed `${var.project}-${var.environment}-` and tagged via the
  provider's `default_tags`, so the whole environment is identifiable and removable by tag.
- IAM policies are least-privilege per component: an asset's IoT policy is scoped to topics
  containing its own `assetId`; the backend task role may publish commands and consume its own
  queues, and nothing more.
- State is local for now. A remote S3 backend with DynamoDB locking is worth adding before
  this is used from more than one machine.
