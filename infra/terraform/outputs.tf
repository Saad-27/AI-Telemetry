output "hostnames" {
  description = "Public hostnames managed here. Run `fly certs add <hostname> -a <app>` for each."
  value       = { for name, h in var.hosts : "${name}.${var.domain}" => h.fly_app }
}
