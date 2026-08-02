terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }

    # Writes each asset's certificate and key where the agent can read them.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}
