output "iot_endpoint" {
  description = "MQTT endpoint for agents. Goes in the agent's [broker] host, with port 8883."
  value       = data.aws_iot_endpoint.ats.endpoint_address
}

output "assets" {
  description = "Provisioned machines and the certificate paths their agents need."
  value = {
    for id, asset in var.assets : id => {
      thing_arn        = aws_iot_thing.asset[id].arn
      topic_prefix     = local.asset_topics[id]
      certificate_path = local_sensitive_file.asset_certificate[id].filename
      private_key_path = local_sensitive_file.asset_private_key[id].filename
    }
  }
}

output "ingestion_queues" {
  description = "Queues the backend consumes in milestone 4, and the dead-letter queues to watch."
  value = {
    telemetry            = aws_sqs_queue.telemetry.url
    telemetry_deadletter = aws_sqs_queue.telemetry_deadletter.url
    registration         = aws_sqs_queue.registration.url

    registration_deadletter = aws_sqs_queue.registration_deadletter.url
  }
}

output "agent_broker_config" {
  description = <<-EOT
    Ready-to-paste [broker] section for each asset's agent.toml. Paths are as written by this
    module; make them absolute, or relative to the agent directory, before use.
  EOT
  value = {
    for id, asset in var.assets : id => <<-TOML
      [broker]
      host = "${data.aws_iot_endpoint.ats.endpoint_address}"
      port = 8883
      tls = true
      keepalive_seconds = 30
      client_id = "${id}"
      client_cert_path = "${local_sensitive_file.asset_certificate[id].filename}"
      client_key_path = "${local_sensitive_file.asset_private_key[id].filename}"
    TOML
  }
}
