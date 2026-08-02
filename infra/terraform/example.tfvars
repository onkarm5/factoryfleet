# Copy to terraform.tfvars and adjust. terraform.tfvars is git-ignored.
project     = "factoryfleet"
environment = "dev"
aws_region  = "ap-south-1"

mqtt_topic_prefix           = "factoryfleet"
telemetry_staleness_minutes = 10
telemetry_retention_days    = 14

# Machines to provision. Each gets a Thing, its own X.509 certificate, and a policy scoped to
# its own topics. Add an entry and re-apply to onboard a machine; remove one to revoke it.
assets = {
  "PRESS-01" = { site_id = "PLANT-A", machine_type = "HYDRAULIC_PRESS" }
  # "MILL-01"  = { site_id = "PLANT-A", machine_type = "CNC_MILL" }
}

# Where apply writes certificates and keys. Git-ignored — this material authenticates a
# machine to the fleet.
certificate_output_dir = "./certs"
