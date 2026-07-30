variable "project" {
  description = "Project name, used as a prefix and tag on every resource."
  type        = string
  default     = "factoryfleet"
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region hosting IoT Core, the queues, the database and the backend."
  type        = string
  default     = "ap-south-1"
}

variable "mqtt_topic_prefix" {
  description = <<-EOT
    Root MQTT topic segment for the fleet. Asset topics are
    "<prefix>/<siteId>/<assetId>/<channel>", and each asset's IoT policy is scoped to its own
    assetId so one machine cannot read or publish another machine's traffic.
  EOT
  type        = string
  default     = "factoryfleet"
}

variable "telemetry_staleness_minutes" {
  description = "Minutes without telemetry after which an asset is marked OFFLINE and alerted on."
  type        = number
  default     = 10
}
