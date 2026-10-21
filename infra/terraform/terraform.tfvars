# Non-secret settings, reviewed in PRs. zone_id comes from the TF_VAR_zone_id environment
# variable and the API token from CLOUDFLARE_API_TOKEN (docs/deploy.md).

# Production only (ADR 0006). Until M3 splits ingest into its own Fly app, both hostnames
# point at the one app.
hosts = {
  "app" = {
    fly_app = "gaslit-api-production"
    role    = "app"
    # From `fly certs setup app.gaslit.dev -a gaslit-api-production` (runbook step 6).
    fly_ownership  = "app-o9znqe3"
    acme_challenge = "app.gaslit.dev.o9znqe3.flydns.net"
  }
  "ingest" = {
    fly_app = "gaslit-api-production"
    role    = "ingest"
    # From `fly certs setup ingest.gaslit.dev -a gaslit-api-production` (runbook step 6).
    fly_ownership  = "app-o9znqe3"
    acme_challenge = "ingest.gaslit.dev.o9znqe3.flydns.net"
  }
}
