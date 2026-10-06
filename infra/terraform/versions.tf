terraform {
  required_version = ">= 1.10"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.27"
    }
  }

  # State lives in a Cloudflare R2 bucket (S3-compatible), so it is not on anyone's laptop
  # and CI can use it. Credentials and the endpoint come from the environment:
  #   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (an R2 API token for this bucket only)
  #   AWS_ENDPOINT_URL_S3=https://<account_id>.r2.cloudflarestorage.com
  backend "s3" {
    bucket = "gaslit-tfstate"
    key    = "cloudflare/terraform.tfstate"
    region = "auto"

    # R2 is not AWS: skip the AWS-only checks.
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true

    # State locking via a lock object (Terraform 1.10+). Verify R2 accepts it on first
    # `terraform init`; if not, remove this line and rely on the CI concurrency group.
    use_lockfile = true
  }
}

# Reads CLOUDFLARE_API_TOKEN from the environment. The token never goes in a file.
provider "cloudflare" {}
