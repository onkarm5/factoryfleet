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

variable "assets" {
  description = <<-EOT
    Machines to provision in IoT Core, keyed by asset id. Each gets a Thing, its own X.509
    certificate, and a policy scoped to its own topics. Add an entry here and re-apply to
    onboard a machine; remove one to revoke it.
  EOT
  type = map(object({
    site_id      = string
    machine_type = string
  }))
  default = {
    "PRESS-01" = {
      site_id      = "PLANT-A"
      machine_type = "HYDRAULIC_PRESS"
    }
  }

  validation {
    # Matches the backend's MachineType enum and the agent's own check. Catching a typo at
    # plan time is better than provisioning a Thing the backend will later reject.
    condition = alltrue([
      for asset in values(var.assets) : contains(
        ["CNC_MILL", "HYDRAULIC_PRESS", "CONVEYOR", "ROBOT_ARM", "INJECTION_MOLDER"],
        asset.machine_type
      )
    ])
    error_message = "machine_type must be one of CNC_MILL, HYDRAULIC_PRESS, CONVEYOR, ROBOT_ARM, INJECTION_MOLDER."
  }

  validation {
    condition     = alltrue([for id in keys(var.assets) : can(regex("^[a-zA-Z0-9:_-]+$", id))])
    error_message = "Asset ids become MQTT topic segments and IoT client ids, so they must contain only letters, digits, colons, hyphens and underscores."
  }
}

variable "certificate_output_dir" {
  description = <<-EOT
    Directory that receives each asset's certificate and private key. Git-ignored: this
    material authenticates a machine to the fleet and must never be committed.
  EOT
  type        = string
  default     = "./certs"
}

variable "telemetry_retention_days" {
  description = <<-EOT
    Days SQS holds telemetry the backend has not yet consumed. Sized so a weekend of backend
    downtime does not lose plant-floor readings.
  EOT
  type        = number
  default     = 14

  validation {
    condition     = var.telemetry_retention_days >= 1 && var.telemetry_retention_days <= 14
    error_message = "SQS supports a retention period between 1 and 14 days."
  }
}
