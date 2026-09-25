# Estate cold boot

When to use this runbook: the whole estate restarted at once. A power cut takes all three PVE
hosts down together, as on 2026-09-25 (report and follow-up: AnsibleSpecs
`handovers/dhcp-outage-2026-09-25/`, EPIC-5). It gives the order things come back in, a check
per layer, and the break-glass moves for when a layer doesn't come back. When OpenBao is the
layer that's down and srviac has to run without it, the escape hatch is
[`iac-cold-boot.md`](iac-cold-boot.md).

## What happens on its own

Every VM has `onboot: 1` and no startup order, so all of them start within a minute of their
PVE host. The layers then wait on each other through retries. On 2026-09-25 the PVE hosts were up
at 15:03 UTC and pods were starting at 15:11. Everything recovered unaided except two things:

- **Keycloak.** Its pinned image digest had been garbage-collected from the registry.
  `dhcpapp-app` crash-looped on OIDC discovery and held the `dhcp` pod not-Ready, so there was
  no DHCP for 2h45m. DHCPApp has been out of the dhcp pod since (DnsmasqDeploy `ca30bbe`), and
  registry-cleanup is suspended until DI-8.
- **srvk8s4.** It took its address from the in-cluster DHCP and stayed off the LAN.
  srvk8s4 and srviac are static now ([`static-address.md`](static-address.md)).

So the job is mostly to watch, top to bottom, and step in at the first layer that doesn't come up.

## Order and checks

The kubectl lines work from KubeCoder (`cexec iac kubectl --kubeconfig ~/.kube/config-prd-write
…`) once srvk8s4 is back. Before that, run them on a control-plane node:
`ssh ansible@srvk8s1 sudo microk8s kubectl …`. With DNS down, use SSH by IP (break-glass below).

1. **PVE**: pve 10.1.0.20, pve1 .21, pve2 .22.
   `pvecm status` is quorate, and `qm list` on each host shows its guests running.
2. **Ceph**: srvceph1–3 at 10.1.0.24–26, VIP `ceph.home` 10.1.0.38. The prd Ceph nodes reject
   the ansible key, so go through the guest agent:
   `ssh root@pve1 "qm guest exec 113 --timeout 60 -- microceph.ceph -s"` should show `HEALTH_OK`
   and every OSD up.
3. **OpenBao**: srvvault1–3 at 10.1.0.40–42, VIP `secrets.home` 10.1.0.39. The seal is static
   auto-unseal, so the nodes unseal themselves.
   `curl -sk https://10.1.0.39:8200/v1/sys/health` should show `"sealed":false`.
4. **Kubernetes**: srvk8s1–3 at 10.1.0.27–29 and srvk8s4 at 10.1.0.44. The apiserver VIP is
   10.1.0.37:16443. `kubectl get nodes` should show all four Ready. Check that the Ceph CSI
   nodeplugins are Running (`-n ceph-csi-rbd-prd`, `-n ceph-csi-cephfs-prd`): without them no PV
   mounts.
5. **Registry**: `registry-prd`, LB 10.2.1.9, ClusterIP 172.17.0.3. The nodes pin that ClusterIP
   in `/etc/hosts`, so image pulls don't wait on dnsmasq. Its storage is the static PV
   `registry-pv`. `curl -s http://10.2.1.9:5000/v2/_catalog` answers.
6. **dnsmasq, then DNS and DHCP**: `dnsmasq-prd`: `dns-0`/`dns-1` on 10.2.1.2/.3, `dhcp` on
   10.2.1.10. The UDM relays DHCP from Intranet, IoT and Guest to 10.2.1.10; it serves no DHCP of
   its own.
   - The pods are Ready, and every EndpointSlice endpoint shows `ready: true`:
     `kubectl -n dnsmasq-prd get endpointslices -o yaml | grep -B3 'ready:'`.
   - MetalLB announces all three:
     `kubectl get servicel2statuses.metallb.io -A | grep -E 'dns-0|dns-1|dhcp'`. No status means
     no ready endpoint, so the address is dark on the LAN.
   - `dig +short router.home @10.2.1.2` returns 10.1.0.1.
   - Leases are being handed out:
     `kubectl -n dnsmasq-prd logs deploy/dhcp -c dhcp-dnsmasq --since=10m | grep -c DHCPACK`
     is above zero.
7. **Postgres, then Keycloak**: `postgres-pas-prd`, then `keycloak-prd`, which reaches its
   database through `postgres-pooler-rw`. `curl -s -o /dev/null -w '%{http_code}\n'
   https://auth.ginbov.nl/realms/homelab/.well-known/openid-configuration` returns 200. Until it
   does, apps that discover OIDC at startup crash-loop (MAT-3), and so do Argo CD's and Jenkins's
   SSO logins.
8. **Apps**: `kubectl get pods -A | grep -vE 'Running|Completed'` is short and shrinking, and
   `kubectl -n argocd-prd get applications` shows everything Synced/Healthy.

## Break-glass

**DHCP down because the `dhcp` pod is not Ready.** Publish the not-ready endpoint, and MetalLB
announces 10.2.1.10 again:

```sh
kubectl -n dnsmasq-prd patch svc dhcp -p '{"spec":{"publishNotReadyAddresses":true}}'
```

The chart pins the field to `false`, so Argo shows the patch as drift. With `selfHeal` off it
stays until the next DnsmasqDeploy sync, which reverts it. Don't push DnsmasqDeploy while DHCP
depends on the patch. Once the pod is Ready, sync or patch it back.

**A pinned image digest missing from the registry** (Keycloak on 2026-09-25): the pod sits in
`ImagePullBackOff` with `NotFound`. Repin the deploy repo to the current digest of the same tag,
as KeycloakDeploy `075184c` did.

**Argo CD without Keycloak.** The local admin account is enabled (`admin.enabled: true` in
`argocd-cm`). `kubectl` with `config-prd-write` works as well.

**One push that syncs two stages of the same deploy repo fails the second on a hook-name clash**
(ANS-124). Delete the other stage's finished `tf-presync-<rev>-presync-<ts>` Job in
`argocd-hooks`; Argo's retry then goes through.

**The operator desktop without DHCP.** Set a static address on "Ethernet 2". The LAN is a
**/16**. The 2026-09-25 attempt used 255.0.0.0, which treats all of 10/8 as on-link, so the
desktop ARPs for addresses it should route through 10.1.0.1. In an elevated prompt:

```bat
netsh interface ipv4 set address "Ethernet 2" static 10.1.0.253 255.255.0.0 10.1.0.1
netsh interface ipv4 set dnsservers "Ethernet 2" static 8.8.8.8 primary
```

Undo afterwards with `netsh interface ipv4 set address "Ethernet 2" dhcp` and
`netsh interface ipv4 set dnsservers "Ethernet 2" dhcp`. `.home` names don't resolve on public
DNS, so reach hosts by IP.

**SSH by IP.** Host certificates carry names, not addresses, so give ssh the name to check the
certificate against. From `ansible/`:

```sh
ssh -o UserKnownHostsFile=files/known_hosts.d/homelab -o GlobalKnownHostsFile=/dev/null \
  -o HostKeyAlias=srvk8s1.home -i ~/.ssh/id_ed25519_ansible ansible@10.1.0.27
```

PVE hosts and the UDM (`root@10.1.0.1`) take `root` with `id_ed25519_pve`. Address list:
router 10.1.0.1; pve/pve1/pve2 .20–.22; srvceph1–3 .24–.26; srvk8s1–3 .27–.29;
`kubernetes-api` .37; `ceph` .38; `secrets` .39; srvvault1–3 .40–.42; srvk8s4 .44;
srviac .45. The static-hosts list in DnsmasqDeploy `chart/templates/stage-manifests.yaml` is
authoritative.
