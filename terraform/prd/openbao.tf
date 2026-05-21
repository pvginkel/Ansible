# Card #12 — daily JSON dump from each srvvaultN to the in-cluster
# backup-server. The provider mints a scope-bound upload token on
# create; Play 0 in site-openbao.yml reads it via `terraform output`
# and ships it to /etc/openbao/backup-token on each peer. Rotate with
# `terraform taint homelab_backup_credential.openbao` then apply +
# re-run site-openbao.yml.
resource "homelab_backup_credential" "openbao" {
  scope     = "openbao"
  retention = 14
}

output "openbao_backup_token" {
  description = "Bearer token authorizing srvvaultN to POST to the `openbao` scope on the backup-server. Sensitive; persisted in tfstate."
  value       = homelab_backup_credential.openbao.token
  sensitive   = true
}
