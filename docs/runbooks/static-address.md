# Moving a managed VM to a static address

When to use this runbook: a Terraform-managed VM leaves the dnsmasq reservation API for a static
address on its vmbr0 NIC, or a static host's vmbr0 address changes. srviac moved this way on
2026-09-25, following the non-k8s path below. The k8s-node path has not been run yet; srvk8s4 is
the node waiting for it. Doctrine: `decisions.md` "MAC addressing for managed VMs" (the bring-up
tier) and "Cloud-init is a first-boot artefact".

## What moves the address, and what bites

- **Cloud-init moves the address.** Terraform writes the new `ipconfig0`, and Proxmox's
  instance-id is a hash of the cloud-init data, so the VM's next start is a "new instance".
  cloud-init then renders netplan from Proxmox's network-data and brings `eth0` up on the static
  address. The same step is what keeps the NIC named `eth0`. Don't switch cloud-init's network
  stage off (`network: {config: disabled}`): the NIC would come back as `enp6s18`, and keepalived
  and Calico on the k8s nodes are bound to `eth0`.
- **That boot comes up half-configured.** Proxmox's network-data describes net0 only, with the
  PVE host's resolvers (the dnsmasq pair). The VLAN-2 and vmbr1 NICs stay down and DNS runs
  through dnsmasq until a `--tags netplan` run applies the full render. That run is step 4.
- **The name has to follow the host.** A reservation host resolves through the reservation API;
  a static host resolves through DnsmasqDeploy's static-hosts. With both, dnsmasq answers with
  both addresses and ssh tries each. After `terraform apply` drops the reservation, the name
  points only at the new address, so the host is unreachable by name until its reboot. The
  reboot therefore goes through Proxmox, not Ansible.
- **Applying from KubeCoder needs the homelab host CA in `~/.ssh/known_hosts`.** The provider
  uploads the cloud-init snippet over SSH and reads that file only. `kc project setup` installs
  the CA (`scripts/kubecoder-keys.sh`). Without it, the apply deletes the old snippet and then
  fails to upload the new one, and the VM can't start until a re-apply puts the snippet back.
- **A microk8s node's kubelet serving cert names its IPs.** The apiserver verifies it
  (`--kubelet-certificate-authority`, preferring InternalIP), and on a joined node nothing
  re-issues it: the apiserver kicker exits on clustered nodes. After an IP change,
  `kubectl logs/exec` and metrics for that node fail on x509. So a k8s node leaves the cluster,
  moves, and joins again, and the join issues a cert for the new address.

## Procedure

Prerequisites, all committed:

- the host_var: `addresses`, `gateway`, `accept_ra: false` and `nameservers` on
  `network_devices[0]`;
- `static_ip = true` in `terraform/prd/vms.tf`;
- the static-hosts entry in DnsmasqDeploy `chart/templates/stage-manifests.yaml`, with a bump of
  `deployment.timestamp` in `chart/templates/_helpers.tpl`. The init container reads
  static-hosts only at pod start.

Pick a free address first: grep static-hosts, then ping it and check `ip neigh` from the router.

### 1. Push DnsmasqDeploy, then Ansible

Argo syncs DnsmasqDeploy and the dnsmasq pods roll. `dig +short <host>.home @10.2.1.2` now
returns both addresses. Push Ansible when you are about to run the rest: the scheduled drift
check turns red for the host until step 4.

### 2. For a k8s node: take it out of the cluster (from srviac, not KubeCoder)

The drain evicts every pod on the node. If KubeCoder runs there, and on srvk8s4 it does, this
session goes with it, `cexec iac` included. Run steps 2–5 from the desktop through srviac
(`ssh srviac`, then `sudo iac -c '…'`, which applies pushed `main`).

```sh
sudo iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/evict-k8s.yml -e evict_target=<node>'
sudo iac -c 'cd /work/Ansible/ansible && ansible <node> -m command -a "microk8s leave" --become'
sudo iac -c 'cd /work/Ansible/ansible && ansible srvk8s1 -m command -a "microk8s remove-node <node>" --become'
```

If the node is srvk8s1, remove it from srvk8s2 instead. Skip this step for any other host.

### 3. `terraform apply`, then a cold start through Proxmox

```sh
cd terraform/prd && cexec iac terraform plan -out=/tmp/<host>.tfplan
```

Expect, for the host: `homelab_dns_reservation` destroyed, the cloud-init snippet file replaced,
and the VM updated in place (`initialization.ip_config` only). Nothing replaced. Apply that plan
file. Then check that nothing is running on the host (for srviac: the IaC Agent is idle in
Jenkins), and on its PVE node:

```sh
qm cloudinit update <vmid> && qm reboot <vmid>
```

`ping` the new address: srviac answered 10 s after `qm reboot`.

### 4. Apply the full netplan

```sh
cd ansible && cexec iac poetry run ansible-playbook playbooks/site.yml --limit <host> --tags netplan
```

Use `playbooks/site-k8s.yml` for a k8s node; `site.yml` skips the k8s groups. The address
doesn't move in this step, so `netplan apply` keeps the connection.

### 5. For a k8s node: join again

```sh
sudo iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/rebuild-k8s.yml -e rebuild_target=<node>'
```

`rebuild-k8s.yml` works on the existing VM as it does on a rebuilt one. It joins (the new
kubelet cert carries the new address), reapplies labels and taints, keeps the node cordoned until
its DaemonSets are Ready, then uncordons. ZFS pools are create-if-absent, so `zpool5` on srvk8s4
is kept.

### 6. Verify

- `ip -br addr` on the host: the static address on `eth0`, the other NICs back on their
  addresses, and the default route via 10.1.0.1.
- `resolvectl dns`: link DNS 8.8.8.8/8.8.4.4 and global 10.2.1.2/10.2.1.3.
- `dig +short <host>.home @10.2.1.2` returns the new address only.
- k8s node: `kubectl get node <node> -o wide` shows the new INTERNAL-IP; `kubectl logs` of a pod
  on it works. `openssl x509 -in /var/snap/microk8s/current/certs/kubelet.crt -noout -ext
  subjectAltName` names the new address. The node's `projectcalico.org/IPv4Address`
  annotation matches.
- srviac: the Jenkins agent is online, and `sudo iac -c 'getent hosts dns srvk8s1 pve.home'`
  resolves inside the container.
- `site.yml --limit <host> --check` (`site-k8s.yml` for a k8s node): `changed=0`.
