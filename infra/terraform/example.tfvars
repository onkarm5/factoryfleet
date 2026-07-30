# Copy to terraform.tfvars and adjust. terraform.tfvars is git-ignored.
project     = "factoryfleet"
environment = "dev"
aws_region  = "ap-south-1"

mqtt_topic_prefix           = "factoryfleet"
telemetry_staleness_minutes = 10
