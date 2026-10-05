# Non-secret settings, reviewed in PRs. zone_id comes from the TF_VAR_zone_id environment
# variable and the API token from CLOUDFLARE_API_TOKEN (docs/deploy.md).

# Universal SSL on Cloudflare's free plan covers one subdomain level (*.gaslit.dev), so
# staging uses "staging-app" rather than "app.staging".
hosts = {
  "staging-app" = {
    fly_app = "gaslit-api-staging"
    role    = "app"
    # fly_ownership = "<value from `fly certs setup staging-app.gaslit.dev -a gaslit-api-staging`>"
  }
  "staging-ingest" = {
    fly_app = "gaslit-api-staging"
    role    = "ingest"
    # fly_ownership = "<value from `fly certs setup staging-ingest.gaslit.dev -a gaslit-api-staging`>"
  }

  # Production (M7):
  # "app"    = { fly_app = "gaslit-api-production", role = "app" }
  # "ingest" = { fly_app = "gaslit-api-production", role = "ingest" }
}
