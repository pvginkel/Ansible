# Workloads on a rebuilt cluster

When to use this runbook: the prd Kubernetes cluster is back on its nodes but empty — rebuilt
from nothing, every object gone — while Ceph, OpenBao and the `TerraformState` repository
survived. A power cut is [`cold-boot.md`](cold-boot.md): there the objects survive and
Kubernetes restarts everything without Argo. A single node is [`k8s-rebuild.md`](k8s-rebuild.md).

**Not rehearsed.** Every render below was reproduced offline and checked against the live
cluster with a server-side dry run and `kubectl diff`, which matched what Argo owns. The order,
the readiness waits and Argo's adoption are reasoned, not run.

## Why by hand

Argo CD renders every deploy repo with the `homelab-shared` library from `https://charts.home`,
and charts.home is itself an Argo app. On an empty cluster nothing renders until charts.home
answers, and charts.home answers only once these run, in this order:

| App | Why it is in the chain |
|---|---|
| `external-secrets` | makes every other app's Secrets, Argo's repo credentials among them (every repo here is private) |
| `ceph-csi-cephfs` | mounts the static CephFS volumes below |
| `registry` | every other image in the chain is pulled from it; its own is from Docker Hub |
| `dnsmasq` | `.home` DNS: CoreDNS forwards `home.` to it |
| `nginx` | terminates TLS for `charts.home` and `tfmirror.home`; its certificates live on its volume |
| `tfmirror` | every PreSync hook's `terraform init` takes the `pvginkel/homelab` provider from it |
| `charts` | charts.home itself |

`ceph-csi-rbd` and `step-ca` join the chain only if nginx's certificates have expired and
certbot must re-issue from `ca.home`.

Argo CD's own chart needs no charts.home, but it carries ExternalSecrets, so External Secrets
goes in before Argo. So the shape is: External Secrets by hand, Argo
([`argocd.md`](argocd.md#bootstrapping-argo-from-nothing)), the rest of the chain by hand, and
then Argo adopts it all and deploys everything else.

## Before you start

- Ansible has converged the nodes (`site-k8s.yml`): Calico, MetalLB's pool, CoreDNS and the
  nodes' `/etc/hosts`. CoreDNS pins `secrets.home` to OpenBao's VIP, so External Secrets reaches
  OpenBao before dnsmasq is up — check with
  `cexec iac kubectl $KC -n kube-system get cm coredns -o yaml | grep secrets.home`.
- OpenBao is unsealed and Ceph is `HEALTH_OK` ([`cold-boot.md`](cold-boot.md) steps 2 and 3).
- Checkouts under `/work`: `Charts`, `ArgoCDDeploy`, and each chain app's deploy repo at the
  revision its stage tracks (`main` unless ArgoCDDeploy's `releases/values.yaml` sets
  `targetRevision`). The deploy repos are `ExternalSecretsDeploy`, `CephCsiCephfsDeploy`,
  `RegistryDeploy`, `DnsmasqDeploy`, `NginxDeploy`, `TfmirrorDeploy`, `ChartsDeploy`, and
  `CephCsiRbdDeploy` and `StepCaDeploy` if certificates need re-issuing. They are private,
  so clone them with the GitHub credentials.
- `KC="--kubeconfig ~/.kube/config-prd-write --context prd"`, as in `argocd.md`, and `KV` the
  cluster's version: the Server Version `cexec iac kubectl $KC version` prints.

## Rendering an app by hand

`scripts/argo-hand-render.py` renders an app-stage the way Argo does. It reads the app's
entry in ArgoCDDeploy's registry. For an upstream app, it renders the upstream chart with the
stage's values, then the companion chart. It packages `homelab-shared` from the Charts checkout
in place of charts.home, drops the hook resources, and stamps each object with Argo's
tracking annotation so that Argo adopts the objects in place:

```sh
cexec iac sh -c "python3 scripts/argo-hand-render.py <app> /work/<DeployRepo> --kube-version $KV \
  > /tmp/<app>-prd.yaml && kubectl $KC apply -f /tmp/<app>-prd.yaml"
```

Namespaces and CRDs come first in the output, so one apply creates them before the objects that
need them. `--part upstream` and `--part companion` split an upstream app in two.

## Procedure

1. **External Secrets' AppRole.** Stage the one Secret External Secrets cannot make for
   itself. The values are in RoboForm under "OpenBao eso AppRole":

   ```sh
   cexec iac kubectl $KC create namespace external-secrets-prd
   cexec iac kubectl $KC -n external-secrets-prd create secret generic openbao-eso-approle \
     --from-literal=role_id=<role_id> --from-literal=secret_id=<secret_id>
   ```

2. **External Secrets.** Its CRDs are too large for a client-side apply, and its admission
   webhooks fail closed. Apply the upstream chart server-side, wait for the webhook, then apply
   the companion chart, which holds the `openbao-prd` ClusterSecretStore:

   ```sh
   cexec iac sh -c "python3 scripts/argo-hand-render.py external-secrets /work/ExternalSecretsDeploy \
     --kube-version $KV --part upstream > /tmp/eso-up.yaml && \
     kubectl $KC apply --server-side --field-manager=argocd-controller -f /tmp/eso-up.yaml"
   cexec iac kubectl $KC -n external-secrets-prd rollout status deploy/external-secrets-prd-webhook
   ```

   Then render and apply `--part companion` the same way. `kubectl $KC get clustersecretstore
   openbao-prd` shows `Ready`.

3. **Argo CD**: [`argocd.md`](argocd.md#bootstrapping-argo-from-nothing), steps 1 to 8. At
   step 4, every Application but Argo's own shows a `ComparisonError` until step 10 below; that
   is expected. Its hooks' credential Secret, `argocd-hooks/argocd-hook-credentials`, takes eight
   OpenBao leaves and is made only if all eight exist: check it is there before step 10.

4. **CephFS CSI**: render and apply `ceph-csi-cephfs`. External Secrets then makes
   `csi-cephfs-secret` and `csi-cephfs-secret-user` in `ceph-csi-cephfs-prd`. Wait for both, and
   for the nodeplugin pods Running.

5. **The static CephFS volumes.** The hooks' Terraform makes these PVs, but the hooks cannot
   run yet. The subvolumes survived in Ceph, so create the PVs by hand. Terraform finds them in
   its state later and leaves them alone.

   | PV | Namespace | Claim | Subvolume | Size |
   |---|---|---|---|---|
   | `registry-pv` | `registry-prd` | `registry-pvc` | `registry-prd-data` | 300Gi |
   | `dhcp-pv` | `dnsmasq-prd` | `dhcp-pvc` | `dnsmasq-prd-dhcp` | 10Mi |
   | `management-api-pv` | `dnsmasq-prd` | `management-api-pvc` | `dnsmasq-prd-management-api` | 10Mi |
   | `nginx-pv` | `nginx-prd` | `nginx-pvc` | `nginx-prd-data` | 10Mi |

   Each subvolume's path, from Ceph:

   ```sh
   ssh root@pve1 "qm guest exec 113 --timeout 60 -- microceph.ceph fs subvolume getpath cephfs <subvolume> k8s"
   ```

   Then one PV per row:

   ```yaml
   apiVersion: v1
   kind: PersistentVolume
   metadata:
     name: <PV>
   spec:
     accessModes: [ReadWriteMany]
     persistentVolumeReclaimPolicy: Retain
     storageClassName: ""
     volumeMode: Filesystem
     capacity:
       storage: <Size>
     claimRef:
       name: <Claim>
       namespace: <Namespace>
     csi:
       driver: cephfs.csi.ceph.com
       volumeHandle: <PV>
       volumeAttributes:
         clusterID: e940ce5a-fe62-49e5-aafc-dce7ce4f01db
         fsName: cephfs
         rootPath: <the path getpath printed>
         staticVolume: "true"
       nodeStageSecretRef:
         name: csi-cephfs-secret-user
         namespace: ceph-csi-cephfs-prd
   ```

   The `clusterID` is the Ceph fsid: `microceph.ceph fsid` through the same guest exec.

6. **Registry**: render and apply `registry`, and wait for its rollout. It keeps ClusterIP
   172.17.0.3, which the nodes and CoreDNS pin. Its LoadBalancer address is dynamic, so on an
   empty cluster it may not be the 10.2.1.9 it was. The images on its volume are back, so pulls
   from `registry:5000` work again.

7. **dnsmasq**: render and apply `dnsmasq`. `dns-0`/`dns-1` come up on 10.2.1.2/.3 and `dhcp`
   on 10.2.1.10 ([`cold-boot.md`](cold-boot.md) step 6 has the checks).

8. **nginx**, then **tfmirror**: render and apply each, and wait for its rollout. nginx's
   certificates for `charts.home` and `tfmirror.home` are on its volume. If they have expired,
   first render and apply `ceph-csi-rbd`, create `step-ca-db-pv` (claim `step-ca-prd/step-ca-db-pvc`, RBD image `step-ca-prd-db` in
   pool `k8s`, 1Gi, ext4, `ReadWriteOnce`, node-stage Secret
   `ceph-csi-rbd-prd/csi-rbd-secret-user`; the live PV is the shape to copy), render and apply `step-ca`, and let certbot re-issue.
   Apply step-ca after dnsmasq and nginx, because it takes a dynamic LoadBalancer address.

9. **charts**: render and apply `charts`. `curl -sL https://charts.home/index.yaml` answers.

10. **Argo takes over.** Refresh the Applications, or wait for Argo's poll. Each app now
    renders, and auto-sync adopts the hand-applied objects and runs the PreSync hooks. The
    hooks' Terraform finds the PVs from step 5 and changes nothing. A hook gets three retries
    (30s, 60s, 120s); an app whose hook failed through them waits for a manual sync. Everything
    else in the registry deploys in the same pass. Done when every Application is Synced and
    Healthy ([`cold-boot.md`](cold-boot.md) step 8).

Afterwards:

- KubeCoder's cluster identities need re-minting after a full cluster loss (the KubeCoder repo's
  `docs/operations/cluster-identity-remint.md`), and the Argo `kubecoder` account's token was
  re-minted at step 3.
- `kubernetes.home` reaches the microk8s dashboard only through nginx's annotations on the addon's
  Service, which no app applies (NginxDeploy's README, "kubernetes.home"). Re-add them:

  ```sh
  kubectl $KC patch service kubernetes-dashboard -n kube-system --patch '{"metadata": {"annotations": {
    "nginx.webathome.org/server-name": "kubernetes.home, kubernetes",
    "nginx.webathome.org/enable-ssl": "yes",
    "nginx.webathome.org/target-port": "443/ssl"}}}'
  ```

## charts.home broken on a running cluster

The same recipe fixes charts.home when a bad release broke it and Argo cannot render the fix:
`kubectl $KC -n charts-prd rollout undo deploy/charts` for a bad image, or render and apply
`charts` from a ChartsDeploy checkout at a good revision. Argo's next sync takes it from there.
