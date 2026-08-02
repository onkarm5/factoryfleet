# FactoryFleet infrastructure

Terraform for the AWS side of the fleet: the MQTT broker and device identities (IoT Core), the
ingestion queues (SQS), the database (RDS PostgreSQL), the backend runtime (ECS Fargate) and
alert delivery (SNS).

## Current state

Milestone 3 is applied: **this now creates real AWS resources.** Costs are small but not zero
— see [Cost](#cost) and [Tearing down](#tearing-down).

| Milestone | Resources | Status |
|---|---|---|
| 3 | IoT thing type, per-asset things, certificates and policies, SQS telemetry and registration queues with dead-letter queues, IoT rules, rule IAM role, CloudWatch error log group | ✅ declared |
| 4 | RDS PostgreSQL instance, subnet group, security groups, Secrets Manager secret | planned |
| 5 | Command result queue and rule | planned |
| 6 | SNS topic and subscription for alerts | planned |
| 7 | ECR repository, ECS cluster, Fargate service, task and execution roles, CloudWatch log groups | planned |

## What milestone 3 builds

```
agent ──mutual TLS, 8883──► IoT Core ──rule──► SQS telemetry queue    ──► backend (milestone 4)
                              │       ──rule──► SQS registration queue ──► backend (milestone 4)
                              └──rule failures──► CloudWatch log group
```

Every machine gets a Thing, its own X.509 certificate, and a policy naming only its own
topics. That policy is the security boundary: a compromised machine cannot read another
machine's commands, publish telemetry as another machine, or subscribe to the fleet wildcard.
The client id is pinned to the asset id, so a stolen certificate cannot even connect as a
different machine.

The rules deliver into SQS rather than calling the backend, so a backend deploy or outage
becomes a growing queue rather than lost telemetry.

## Usage

Terraform reads credentials from your local AWS configuration — set up a profile with
`aws configure` (or export `AWS_PROFILE`) before running. No credentials belong in this
repository.

```bash
terraform init
terraform validate
terraform fmt -check
```

```bash
cp example.tfvars terraform.tfvars   # then edit — terraform.tfvars is git-ignored
terraform plan -var-file=terraform.tfvars
```

Read the plan before applying. It should create roughly 20 resources and nothing else.

```bash
terraform apply -var-file=terraform.tfvars
```

### Pointing an agent at IoT Core

`apply` writes each asset's certificate and key under `certs/<assetId>/` and prints a ready
`[broker]` block:

```bash
terraform output -raw iot_endpoint
terraform output agent_broker_config
```

Paste that block into `agent/config/agent.toml`, making the two certificate paths either
absolute or relative to the `agent/` directory, and start the agent. Nothing else changes:

```bash
cd ../../agent && .venv/bin/python -m factoryfleet_agent
```

Watch the messages arrive in the MQTT test client in the IoT Core console (subscribe to
`factoryfleet/#`), or read them off the queue:

```bash
aws sqs receive-message \
  --queue-url "$(terraform output -json ingestion_queues | jq -r .telemetry)" \
  --max-number-of-messages 5
```

### Onboarding another machine

Add an entry to `assets` in `terraform.tfvars` and re-apply. Removing an entry deletes that
machine's Thing, certificate and policy, which is how a machine is revoked.

```hcl
assets = {
  "PRESS-01" = { site_id = "PLANT-A", machine_type = "HYDRAULIC_PRESS" }
  "MILL-01"  = { site_id = "PLANT-A", machine_type = "CNC_MILL" }
}
```

## Security

- **Terraform state contains private keys.** IoT Core generates each certificate's key and
  returns it to Terraform, so it is written to state in plain text. State is git-ignored here
  and must be treated as a secret; a remote backend for this project would need encryption at
  rest and restricted access. The production answer is fleet provisioning or a
  device-generated CSR, so the private key never leaves the machine — the right design for a
  real fleet, and more machinery than provisioning a few demo assets from one laptop needs.
- `certs/`, `*.pem`, `*.key`, `*.crt`, `*.tfstate*` and `*.tfvars` are all git-ignored.
- Asset policies grant `iot:Connect` only for a client id equal to the asset id, publish only
  to that asset's own topics, and subscribe only to its own command topic.
- The IoT rule role can send to exactly the two ingestion queues, and its trust policy is
  constrained by `aws:SourceAccount` and `aws:SourceArn` so another account cannot use it.
- Queues are encrypted with SQS-managed keys.

## Cost

Small, but not free, and it accrues while idle.

| Resource | Cost driver |
|---|---|
| IoT Core | Per connection-minute and per message. One agent sampling every 10s and publishing every 15s is a few thousand messages a day — cents. |
| SQS | Requests. The free tier covers a million a month, which one agent will not approach. |
| CloudWatch Logs | Only written when a rule fails; 14-day retention. |
| Things, certificates, policies, IAM roles | No charge. |

The line items that matter arrive in later milestones: RDS and Fargate. Nothing here runs a
server, so an idle environment costs very little — but it is not zero, and an agent left
running publishes continuously.

## Tearing down

```bash
terraform destroy -var-file=terraform.tfvars
```

This deletes the certificates as well, so agents pointed at IoT Core stop being able to
connect. Delete the local `certs/` directory too — the material in it is dead once the
certificate is gone, and leaving private keys lying around serves no purpose.

## Conventions

- Every resource is prefixed `${var.project}-${var.environment}-` and tagged via the
  provider's `default_tags`, so the whole environment is identifiable and removable by tag.
- IAM policies are least-privilege per component: an asset's IoT policy is scoped to topics
  containing its own `assetId`; the backend task role will be allowed to publish commands and
  consume its own queues, and nothing more.
- State is local for now. A remote S3 backend with locking is worth adding before this is used
  from more than one machine — with the caveat about private keys above.
