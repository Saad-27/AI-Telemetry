variable "zone_id" {
  description = "Cloudflare zone ID for the domain (Cloudflare dashboard > domain > Overview). Set via TF_VAR_zone_id."
  type        = string
}

variable "domain" {
  description = "Apex domain (D1)."
  type        = string
  default     = "gaslit.dev"
}

variable "hosts" {
  description = <<-EOT
    Subdomain => where it points. `fly_app` is the Fly app it routes to; `role` is "app"
    (dashboard) or "ingest" (SDK traffic, which gets stricter firewall rules).
    `fly_ownership` (the _fly-ownership TXT value) and `acme_challenge` (the _acme-challenge
    CNAME target) are what `fly certs setup` prints; Fly needs both to issue and renew a
    certificate behind Cloudflare's proxy.
  EOT
  type = map(object({
    fly_app        = string
    role           = string
    fly_ownership  = optional(string)
    acme_challenge = optional(string)
  }))

  validation {
    condition     = alltrue([for h in values(var.hosts) : contains(["app", "ingest"], h.role)])
    error_message = "Each host's role must be \"app\" or \"ingest\"."
  }
}

variable "ingest_rate_limit_per_10s" {
  description = "Requests per 10 s per client IP on ingest hosts before Cloudflare blocks for 10 s. An SDK process sends ~2 requests per 10 s, so 300 allows ~150 processes behind one NAT IP."
  type        = number
  default     = 300
}

variable "edge_secret" {
  description = "Value of the X-Gaslit-Edge header Cloudflare adds to every request; the same value is the app's EDGE_SECRET Fly secret (ADR 0027). Set via TF_VAR_edge_secret."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.edge_secret) >= 32 && can(regex("^[0-9A-Za-z]+$", var.edge_secret))
    error_message = "edge_secret must be at least 32 letters or digits (openssl rand -hex 32)."
  }
}
