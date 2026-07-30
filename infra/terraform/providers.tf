provider "aws" {
  region = var.aws_region

  # Tag everything so fleet infrastructure is attributable in Cost Explorer and easy to
  # tear down by tag when the demo environment is no longer needed.
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
