locals {
  ingest_hosts = sort([for name, h in var.hosts : "${name}.${var.domain}" if h.role == "ingest"])
  # Cloudflare rule expressions take a space-separated set of quoted strings.
  ingest_host_set = join(" ", [for h in local.ingest_hosts : "\"${h}\""])
}

# ---- DNS -------------------------------------------------------------------------------

# Each hostname is a proxied CNAME to its Fly app, so traffic passes through Cloudflare's
# TLS, WAF and DDoS protection before reaching Fly.
resource "cloudflare_dns_record" "host" {
  for_each = var.hosts

  zone_id = var.zone_id
  name    = "${each.key}.${var.domain}"
  type    = "CNAME"
  content = "${each.value.fly_app}.fly.dev"
  proxied = true
  ttl     = 1 # automatic; required for proxied records
  comment = "Fly app ${each.value.fly_app} (managed by Terraform)"
}

# Proves domain ownership to Fly so it can issue the origin certificate behind the proxy.
resource "cloudflare_dns_record" "fly_ownership" {
  for_each = { for name, h in var.hosts : name => h if h.fly_ownership != null }

  zone_id = var.zone_id
  name    = "_fly-ownership.${each.key}.${var.domain}"
  type    = "TXT"
  content = each.value.fly_ownership
  ttl     = 300
  comment = "Fly certificate ownership (managed by Terraform)"
}

# ---- TLS (brief §11.2) -----------------------------------------------------------------

# Full (strict): Cloudflare -> Fly is HTTPS with a valid certificate. "Flexible" would send
# traffic to Fly unencrypted and can cause redirect loops.
resource "cloudflare_zone_setting" "ssl" {
  zone_id    = var.zone_id
  setting_id = "ssl"
  value      = "strict"
}

resource "cloudflare_zone_setting" "always_use_https" {
  zone_id    = var.zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_zone_setting" "min_tls_version" {
  zone_id    = var.zone_id
  setting_id = "min_tls_version"
  value      = "1.2"
}

# HSTS, as in the brief. Browsers already force HTTPS for every .dev domain (the TLD is
# preloaded), so this mainly matters for non-browser clients and if the domain ever changes.
resource "cloudflare_zone_setting" "security_header" {
  zone_id    = var.zone_id
  setting_id = "security_header"
  value = {
    strict_transport_security = {
      enabled            = true
      max_age            = 31536000
      include_subdomains = true
      preload            = false
      nosniff            = true
    }
  }
}

# ---- Firewall (brief §11.5) --------------------------------------------------------------

# Ingest hosts only serve the SDK. Anything else (scanners probing /wp-admin, /.env, the
# dashboard API) is blocked at Cloudflare and never reaches our machines.
# Let's Encrypt's HTTP-01 path stays open so Fly can renew certificates.
resource "cloudflare_ruleset" "firewall" {
  count = length(local.ingest_hosts) > 0 ? 1 : 0

  zone_id = var.zone_id
  name    = "Custom firewall rules"
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules = [
    {
      ref         = "ingest_paths_only"
      description = "Ingest hosts: only the SDK, health and ACME paths"
      action      = "block"
      expression = join(" ", [
        "http.host in {${local.ingest_host_set}} and not (",
        "http.request.uri.path in {\"/v1/ingest\" \"/healthz\" \"/readyz\"}",
        "or starts_with(http.request.uri.path, \"/.well-known/acme-challenge/\"))",
      ])
    },
  ]
}

# Coarse per-IP flood protection for ingest. Per-key limits and quotas are enforced in the
# app; this only stops one IP hammering us before the app sees it. The free plan allows one
# rate-limit rule with a 10 s period and 10 s block, keyed on IP and Cloudflare data centre.
resource "cloudflare_ruleset" "rate_limit" {
  count = length(local.ingest_hosts) > 0 ? 1 : 0

  zone_id = var.zone_id
  name    = "Rate limits"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [
    {
      ref         = "ingest_per_ip"
      description = "Ingest: per-IP flood protection"
      action      = "block"
      expression  = "http.host in {${local.ingest_host_set}}"
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 10
        requests_per_period = var.ingest_rate_limit_per_10s
        mitigation_timeout  = 10
      }
    },
  ]
}
