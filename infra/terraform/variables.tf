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
    `fly_ownership` is the value of the _fly-ownership TXT record that `fly certs setup`
    prints; Fly needs it to issue a certificate behind Cloudflare's proxy.
  EOT
  type = map(object({
    fly_app       = string
    role          = string
    fly_ownership = optional(string)
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
