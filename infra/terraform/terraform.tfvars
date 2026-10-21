# Non-secret settings, reviewed in PRs. zone_id comes from the TF_VAR_zone_id environment
# variable and the API token from CLOUDFLARE_API_TOKEN (docs/deploy.md).

# Production only (ADR 0006). Until M3 splits ingest into its own Fly app, both hostnames
# point at the one app.
hosts = {
  "app" = {
    fly_app = "gaslit-api-production"
    role    = "app"
    # fly_ownership = "<value from `fly certs setup app.gaslit.dev -a gaslit-api-production`>"
    # acme_challenge = "<CNAME target from the same command>"
  }
  "ingest" = {
    fly_app = "gaslit-api-production"
    role    = "ingest"
    # fly_ownership = "<value from `fly certs setup ingest.gaslit.dev -a gaslit-api-production`>"
    # acme_challenge = "<CNAME target from the same command>"
  }
}
