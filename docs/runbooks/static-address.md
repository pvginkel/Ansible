# Moving a managed VM to a static address

When to use this runbook: a Terraform-managed VM leaves the dnsmasq reservation API for a static
address on its vmbr0 NIC, or a static host's vmbr0 address changes. The first use was srvk8s4 and
srviac after the 2026-09-25 DHCP outage. Doctrine: `decisions.md` "MAC addressing for
managed VMs" (the bring-up tier) and "Cloud-init is a first-boot artefact".

## Why it is more than a host_vars edit

Four things bite if you just edit `network_devices` and run `site.yml`:

- **`netplan apply` cuts Ansible's own connection.** The handler moves the address Ansible is
  connected on, so the run hangs mid-handler. Render with `-e baseline_netplan_apply=false`
  instead, and reboot to switch.
- **The name has to follow the host.** A reservation host resolves through the reservation API.
  A static host resolves through DnsmasqDeploy's static-hosts. When both hold the name, dnsmasq
  answers with both addresses and ssh tries each in turn, so Ansible reaches the host before and
  after its reboot. The order below keeps that overlap until the host has moved.
- **Terraform's `ipconfig0` change churns the cloud-init instance-id.** On the next cold start
  cloud-init treats the VM as a new instance and re-renders netplan from Proxmox's network-data,
  which describes net0 only. baseline's `99-homelab-network.cfg` drop-in (tag `cloud_init`)
  switches that stage off. Land the drop-in before the host's next cold start.
- **A microk8s node's kubelet serving cert names its IPs.** The apiserver verifies it
  (`--kubelet-certificate-authority`, preferring InternalIP), and on a joined node nothing
  re-issues it: the apiserver kicker exits on clustered nodes. After an IP change,
  `kubectl logs/exec` and metrics for that node fail on x509 until the node re-joins. So a k8s
  node leaves the cluster, moves, and joins again. The join issues a cert for the new address.

## Procedure

Prerequisites, all committed:

- the host_var: `addresses`, `gateway`, `accept_ra: false` and `nameservers` on
  `network_devices[0]`;
- `static_ip = true` in `terraform/prd/vms.tf`;
- the static-hosts entry in DnsmasqDeploy `chart/templates/stage-manifests.yaml`, with a bump of
  `deployment.timestamp` in `chart/templates/_helpers.tpl`. The init container reads
  static-hosts only at pod start.

Pick a free address first: grep static-hosts, then ping it and check `ip neigh` from the router.

### 1. Land the cloud-init drop-in (KubeCoder)

```sh
cd ansible && cexec iac poetry run ansible-playbook playbooks/site.yml --limit <host> --tags cloud_init
```

For a k8s node, use `playbooks/site-k8s.yml` here and in 3b. `site.yml` skips the k8s groups.

### 2. Push Ansible, then the static-hosts commit

Push the Ansible commit first. `iac -c` on srviac runs pushed `main`, and the scheduled drift
check turns red for `<host>` until the switch is done, so push only when you are about to run
the switch. Then push DnsmasqDeploy. Argo syncs it and the dnsmasq pods roll. Check that the
name returns both addresses:

```sh
dig +short <host>.home @10.2.1.2
```

### 3a. A host outside the cluster (KubeCoder)

Check that nothing is running on it (for srviac: no Jenkins build in progress), then:

```sh
cd ansible && cexec iac poetry run ansible-playbook playbooks/site.yml --limit <host> --tags netplan -e baseline_netplan_apply=false
cd ansible && cexec iac poetry run ansible <host> -m ansible.builtin.reboot --become
```

`--tags netplan` also lands the resolved `home` routing drop-in that goes with public resolvers.

### 3b. A k8s node (from srviac, not KubeCoder)

The drain evicts every pod on the node. If KubeCoder runs there, and on srvk8s4 it does, this
session goes with it, `cexec iac` included. Run the steps from the desktop through srviac
(`ssh srviac`, then `sudo iac -c '…'`, which applies pushed `main`). Don't leave a render
waiting on disk overnight. `update-k8s.yml` (`IaC/scheduled-update`, `H 4 * * 0`, Sunday morning) reboots a node
when anything is pending, and a node that comes up on its new address before it re-joins has the x509 problem
above.

```sh
sudo iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/evict-k8s.yml -e evict_target=<node>'
sudo iac -c 'cd /work/Ansible/ansible && ansible <node> -m command -a "microk8s leave" --become'
sudo iac -c 'cd /work/Ansible/ansible && ansible srvk8s1 -m command -a "microk8s remove-node <node>" --become'
sudo iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/site-k8s.yml --limit <node> --tags netplan -e baseline_netplan_apply=false'
sudo iac -c 'cd /work/Ansible/ansible && ansible <node> -m ansible.builtin.reboot --become'
sudo iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/rebuild-k8s.yml -e rebuild_target=<node>'
```

`rebuild-k8s.yml` works on the existing VM as it does on a rebuilt one. It joins (the new
kubelet cert carries the new address), reapplies labels and taints, keeps the node cordoned until
its DaemonSets are Ready, then uncordons. ZFS pools are create-if-absent, so `zpool5` on srvk8s4
is kept. If the node is srvk8s1, remove it from srvk8s2 instead.

### 4. `terraform apply` (KubeCoder)

```sh
cd terraform/prd && cexec iac terraform plan
```

Expect, per host: `homelab_dns_reservation` destroyed, the cloud-init snippet file replaced, and
the VM updated in place (`initialization.ip_config`). Nothing replaced. Then `terraform apply`.
The name now resolves to the static address only.

The `ipconfig0` change is PENDING on the VM. Commit it to the cloud-init drive without a reboot,
so the next `update-k8s.yml` run does not cold-cycle the node for it:

```sh
ssh root@pve 'qm cloudinit update <vmid> && qm pending <vmid> | grep ipconfig'
```

### 5. Verify

- `ip -br addr` on the host: the static address on eth0, and the other NICs unchanged.
- `resolvectl status`: link DNS 8.8.8.8/8.8.4.4; `home` on the global scope.
- k8s node: `kubectl get node <node> -o wide` shows the new INTERNAL-IP; `kubectl logs` of a pod
  on it works. `openssl x509 -in /var/snap/microk8s/current/certs/kubelet.crt -noout -ext
  subjectAltName` names the new address. The node's `projectcalico.org/IPv4Address`
  annotation matches.
- srviac: the Jenkins agent is online, and `sudo iac -c 'getent hosts dns srvk8s1'` resolves
  both inside the container.
- `site.yml --limit <host> --check` (`site-k8s.yml` for a k8s node): `changed=0`.
