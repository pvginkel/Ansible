# Phase 4b — k8s VM rebuild scaffolding

**Status**: ✅ Done

## What this phase delivered

The from-scratch shape for the four k8s VMs, staged but not applied. Phase 4c picks up the actual rebuilds.

- `terraform/modules/managed-vm/` grew optional `cloud_init` and `machine` inputs. The module's `lifecycle.ignore_changes` now reserves `disk[2]` and `disk[3]` for Ansible-owned passthrough disks and `disk[0].file_id` for the cloud image (which rolls forward under `current/`).
- `terraform/prd/` gained the from-scratch scaffold — image download per `pve_node`, per-VM `tls_private_key`, cloud-init snippet, `local_file` writing `ansible/files/known_hosts.d/prd`. All gated on `from_scratch = true` per VM, so the resources only materialise once a per-VM rebuild commit lands.
- The four k8s VMs in `terraform/prd/vms.tf` were flipped to the from-scratch shape under their new map keys (`srvk8s1/2/3`, `wrkdevk8s`); VMIDs rotated into the 910-range; deterministic MACs (`02:A7:F3:VV:VV:EE`); `smbios_uuid` dropped (bpg generates fresh on apply); `srvk8s3` folded the seabios → ovmf flip.
- `proxmox_host` role grew a passthrough-disk reconcile task — owns the lifecycle per `decisions.md` "Disk passthrough on managed VMs" so passthroughs no longer sit in TF.
- `ansible/playbooks/rebuild-k8s.yml` drives the Ansible-side post-TF-apply work (bootstrap + baseline + microk8s on the rebuilt host, then `proxmox_host` on its `pve_node` for the passthrough attach, then `zpool import` and the `microk8s status --wait-ready`).
- `ansible/ansible.cfg` lists `files/known_hosts.d/prd` at the head of `UserKnownHostsFile`.
- `docs/runbooks/k8s-rebuild.md` walks the operator orchestration end-to-end.
- `docs/decisions.md` "Tool split" makes the rule explicit: Terraform and Ansible are peer tools — neither invokes the other.

## Pointers

- Runbook: [`docs/runbooks/k8s-rebuild.md`](../runbooks/k8s-rebuild.md).
- Playbook: [`ansible/playbooks/rebuild-k8s.yml`](../../ansible/playbooks/rebuild-k8s.yml).
- Continuation: [`phase-4c-microk8s-rebuild-execution.md`](phase-4c-microk8s-rebuild-execution.md).
