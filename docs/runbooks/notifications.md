# Notifications runbook

The Notifications platform: ntfy and the notification bridge in one pod, the Deployment
`notifications` in `notifications-prd`, which Argo CD deploys from
[NotificationsDeploy](https://github.com/pvginkel/NotificationsDeploy). A sender holds a
registration's token and submits to the bridge's API, and the bridge delivers to the
registration's ntfy topic, its Telegram chats or both. Read this to seed the platform's two
OpenBao leaves, to subscribe a phone, to add a registration, to seed the Jenkins adapter's leaf,
when `NtfyDown`, `NotificationBridgeDown`, `NotificationDeliveryFailing` or
`AlertmanagerWebhookFailing` fires, and to restore the platform.

The pod, its Services and its Secrets are in NotificationsDeploy's
[README](https://github.com/pvginkel/NotificationsDeploy/blob/main/README.md). The bridge's API is
in Notifications' [README](https://github.com/pvginkel/Notifications/blob/main/README.md), and
its configuration and refusals in
[`docs/bridge-configuration.md`](https://github.com/pvginkel/Notifications/blob/main/docs/bridge-configuration.md).
The design is NotificationsSpecs `handovers/ntfy-notifications-2026-10-10.md`, §4.

## Conventions

- **The operator's keystroke.** Every OpenBao write and every `kubectl` write is the operator's.
- **No secret on a screen, on a command line or in shell history.** A generated value is held in
  a shell variable inside a `( … )` subshell, which ends with the block, and reaches OpenBao on
  stdin. jq takes the values from its environment, never from its arguments. One step prints a
  secret, because it has to: [Subscribing a phone](#subscribing-a-phone) shows a user's
  `<user>-password`, which is typed into the phone.
- **Shell.** Set this up in each bash shell that runs these steps, in the KubeCoder pod. Re-source
  `bao-login.sh` once `bao` answers `permission denied`, which means its token has expired.

  ```bash
  cd /work/Ansible && . scripts/bao-login.sh
  bao() { cexec iac bao "$@"; }
  k()   { cexec iac kubectl --context prd -n notifications-prd "$@" </dev/null; }
  kw()  { cexec iac kubectl --kubeconfig "$HOME/.kube/config-prd-write" --context prd -n notifications-prd "$@" </dev/null; }
  p()   { cexec iac kubectl --context prd -n prometheus-prd "$@" </dev/null; }
  pw()  { cexec iac kubectl --kubeconfig "$HOME/.kube/config-prd-write" --context prd -n prometheus-prd "$@" </dev/null; }
  ```

- **Checkouts.** The commands read NotificationsDeploy and KubeCoderDeploy at `origin/main`, from
  checkouts under `/work/scratch`. Clone a missing one with
  `git clone https://github.com/pvginkel/<Repo>.git /work/scratch/<Repo>`.

## The two leaves

The chart's two ExternalSecrets read every key below but the users' passwords
(NotificationsDeploy `config/prd/values.yaml`, `externalSecrets`). The kind and interval of each
key are its leaf's row in the secret catalog,
[`catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md).

| Leaf (`kv/`) | Key | What it is | Seeded from |
|---|---|---|---|
| `eso/prd/notifications/prd/ntfy-auth` | `pvginkel-password` | The password of the ntfy user `pvginkel`, an admin. The operator's phone and the web app log in with it | Generated |
| | `pvginkel-hash` | Its bcrypt hash, which ntfy's auth config takes | `pvginkel-password`, through `ntfy user hash` |
| | `mvdbovenkamp-password` | The password of the ntfy user `mvdbovenkamp`, which may only read `pilot-app-mvdbovenkamp`. mvdbovenkamp's phone logs in with it | Generated |
| | `mvdbovenkamp-hash` | Its bcrypt hash, which ntfy's auth config takes | `mvdbovenkamp-password`, through `ntfy user hash` |
| | `bridge-hash` | The bcrypt hash of the ntfy user `bridge`, which only writes, and never logs in | A throwaway password that is never stored, through `ntfy user hash` |
| | `bridge-token` | The `bridge` user's token, `tk_` and 29 characters: the bridge's `NTFY_TOKEN` | `ntfy token generate` |
| `eso/prd/notifications/prd/registrations` | `jenkins-token`, `alertmanager-token`, `kubecoder-token`, `kubecoder-dev-token` | The bearer token each registration's senders hold: 43 URL-safe characters, the rotator's `random` | Generated |
| | `jenkins-telegram-bot-token`, `jenkins-telegram-chat-ids` | The Jenkins bot and its chat. This leaf is the primary for both | This leaf: block 3 reads them back before it writes |
| | `alertmanager-telegram-bot-token`, `alertmanager-telegram-chat-ids` | Alertmanager's bot | `eso/prd/prometheus/prd/telegram`: `bot_token`, `chat_id` |
| | `kubecoder-telegram-bot-token`, `kubecoder-telegram-chat-ids` | KubeCoder prd's bot and its alerts chats | `eso/prd/kubecoder/prd/bot-token#token`; KubeCoderDeploy `config/prd/values.yaml` `bot.alertChatIds`, joined by commas |
| | `kubecoder-dev-telegram-bot-token`, `kubecoder-dev-telegram-chat-ids` | KubeCoder dev's bot and its alerts chats | `eso/prd/kubecoder/dev/bot-token#token`; KubeCoderDeploy `config/dev/values.yaml` `bot.alertChatIds`, joined by commas |

A new leaf also needs what [openbao.md § A new leaf](openbao.md#a-new-leaf) lists.

## Seeding both leaves

Four blocks, in order, in one shell set up as in [Conventions](#conventions). Paste each block
whole. A block that fails stops before it writes.

1. **ntfy's CLI.** The release binary of the server version the chart pins, which the comment on
   `images.ntfy` names, checked against the release's checksums:

   ```bash
   unset ntfy
   nd=/work/scratch/NotificationsDeploy kd=/work/scratch/KubeCoderDeploy
   git -C "$nd" fetch -q origin && git -C "$kd" fetch -q origin
   ver=$(git -C "$nd" show origin/main:config/prd/values.yaml | sed -n 's|.*binwiederhier/ntfy v\([0-9.]*\).*|\1|p')
   dl=$(mktemp -d) rel=https://github.com/binwiederhier/ntfy/releases/download/v$ver
   curl -sSfL --output-dir "$dl" -O "$rel/ntfy_${ver}_linux_amd64.tar.gz" -O "$rel/checksums.txt"
   (cd "$dl" && grep " ntfy_${ver}_linux_amd64.tar.gz\$" checksums.txt | sha256sum --check) &&
     tar -xzf "$dl/ntfy_${ver}_linux_amd64.tar.gz" -C "$dl" && ntfy=$dl/ntfy_${ver}_linux_amd64/ntfy
   ```

   The check prints `ntfy_<version>_linux_amd64.tar.gz: OK`. On `FAILED` nothing is extracted and
   `$ntfy` stays unset, so block 2 stops before it writes.

2. **`ntfy-auth`.** `ntfy user hash` reads the password twice on stdin. Its `password:` and
   `confirm:` prompts go to stderr and wait for nothing.

   ```bash
   (
     set -eo pipefail
     pw=$(openssl rand -base64 18 | tr '+/' '-_')
     mpw=$(openssl rand -base64 18 | tr '+/' '-_')
     throwaway=$(openssl rand -base64 18)
     ph=$(printf '%s\n%s\n' "$pw" "$pw" | "$ntfy" user hash)
     mh=$(printf '%s\n%s\n' "$mpw" "$mpw" | "$ntfy" user hash)
     bh=$(printf '%s\n%s\n' "$throwaway" "$throwaway" | "$ntfy" user hash)
     bt=$("$ntfy" token generate)
     PW=$pw PH=$ph MPW=$mpw MH=$mh BH=$bh BT=$bt jq -n '{"pvginkel-password": env.PW,
         "pvginkel-hash": env.PH, "mvdbovenkamp-password": env.MPW, "mvdbovenkamp-hash": env.MH,
         "bridge-hash": env.BH, "bridge-token": env.BT}' \
       | bao kv put -mount=kv eso/prd/notifications/prd/ntfy-auth -
   )
   ```

3. **`registrations`.** The tokens are generated, the Jenkins bot's keys read back from this leaf,
   their primary, the other bots' keys copied from their leaves, and the KubeCoder chat lists read
   from KubeCoderDeploy:

   ```bash
   (
     set -eo pipefail
     rnd() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'; }
     src() { bao kv get -mount=kv -field="$2" "eso/prd/$1"; }
     chats() { git -C "$kd" show "origin/main:config/$1/values.yaml" | yq -r '.bot.alertChatIds | join(",")'; }
     jb=$(src notifications/prd/registrations jenkins-telegram-bot-token)
     jc=$(src notifications/prd/registrations jenkins-telegram-chat-ids)
     ab=$(src prometheus/prd/telegram bot_token)
     ac=$(src prometheus/prd/telegram chat_id)
     kb=$(src kubecoder/prd/bot-token token)
     kc=$(chats prd)
     db=$(src kubecoder/dev/bot-token token)
     dc=$(chats dev)
     JT=$(rnd) JB=$jb JC=$jc AT=$(rnd) AB=$ab AC=$ac KT=$(rnd) KB=$kb KC=$kc DT=$(rnd) DB=$db DC=$dc \
     jq -n '{"jenkins-token": env.JT, "jenkins-telegram-bot-token": env.JB,
         "jenkins-telegram-chat-ids": env.JC, "alertmanager-token": env.AT,
         "alertmanager-telegram-bot-token": env.AB, "alertmanager-telegram-chat-ids": env.AC,
         "kubecoder-token": env.KT, "kubecoder-telegram-bot-token": env.KB,
         "kubecoder-telegram-chat-ids": env.KC, "kubecoder-dev-token": env.DT,
         "kubecoder-dev-telegram-bot-token": env.DB, "kubecoder-dev-telegram-chat-ids": env.DC}' \
       | bao kv put -mount=kv eso/prd/notifications/prd/registrations -
   )
   ```

   Blocks 2 and 3 each answer with the leaf's path and its metadata, `version` among it.

4. **Check, and clean up.** Each key prints as `true` when it holds a value: six keys, then
   twelve.

   ```bash
   for leaf in ntfy-auth registrations; do
     bao kv get -mount=kv -format=json "eso/prd/notifications/prd/$leaf" | jq -c '.data.data | map_values(length > 0)'
   done
   rm -r "$dl"
   ```

### Re-seeding a running deployment

Skip this on the first seeding: before Argo CD has deployed the platform there is nothing to
restart, and the commands below answer `Error from server (NotFound): namespaces
"notifications-prd" not found`.

Blocks 2 and 3 write their leaf whole. Run again, block 2 gives `pvginkel` and `mvdbovenkamp` new
passwords, so both phones log in again, and block 3 gives every registration a new token, which each
sender's copy then follows (the Jenkins adapter's: [Seeding the Jenkins adapter's
leaf](#seeding-the-jenkins-adapters-leaf)). A running pod takes new values once its ExternalSecrets
have synced and it has restarted:

```bash
for es in ntfy-auth registrations; do kw annotate externalsecret "$es" force-sync="$(date +%s)" --overwrite; done
k get externalsecrets          # both SecretSynced
kw rollout restart deployment/notifications
```

## Subscribing a phone

The server is `https://ntfy.webathome.org`. The user `pvginkel` reads every topic, the
registrations' ntfy topics:

```bash
git -C /work/scratch/NotificationsDeploy show origin/main:config/prd/values.yaml | yq -r '[.registrations[].ntfy.topic] | unique[]'
```

The user `mvdbovenkamp` reads only `pilot-app-mvdbovenkamp`. A user's password is
`<user>-password`, which this prints:
`bao kv get -mount=kv -field=<user>-password eso/prd/notifications/prd/ntfy-auth`.

1. Install the ntfy Android app (Google Play or F-Droid). Neither flavor uses Firebase for a
   self-hosted server.
2. Add each topic with **Use another server** set to `https://ntfy.webathome.org`. Anonymous
   clients get no topic, so the app asks for the user and the password.
3. The web app at `https://ntfy.webathome.org` takes the same login and topics.

The iOS app gets no instant notifications from this server: it needs the server to forward to
ntfy.sh (`NTFY_UPSTREAM_BASE_URL`), which the chart leaves unset.

## Adding a registration

One values entry, one token, one copy and one icon. The bridge refuses to start while a
registration's placeholder names a variable the Secret `registrations` lacks, so the keys are in
the leaf before NotificationsDeploy's push.

1. **The token**, generated into the `registrations` leaf. `patch`, never `put`: `put` replaces a
   leaf whole.

   ```bash
   reg=<registration>
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n' \
     | bao kv patch -mount=kv eso/prd/notifications/prd/registrations "$reg-token=-"
   ```

2. **Telegram**, for a registration with that channel: the bot's token, copied from the bot's own
   leaf, and the chat ids, comma-separated.

   ```bash
   (
     set -eo pipefail
     bot=$(bao kv get -mount=kv -field=<key> eso/prd/<bot's leaf>)
     printf %s "$bot" | bao kv patch -mount=kv eso/prd/notifications/prd/registrations "$reg-telegram-bot-token=-"
   )
   printf %s '<chat id>,<chat id>' | bao kv patch -mount=kv eso/prd/notifications/prd/registrations "$reg-telegram-chat-ids=-"
   ```

   Make the bot an admin of its group, so that a dismiss can delete its message after 48 hours.
3. **The copy**: the token into the sender's own leaf, which the sender's ExternalSecret reads. Use
   `put` instead of `patch` when that leaf is new.

   ```bash
   (
     set -eo pipefail
     t=$(bao kv get -mount=kv -field="$reg-token" eso/prd/notifications/prd/registrations)
     printf %s "$t" | bao kv patch -mount=kv eso/prd/<sender's leaf> <key>=-
   )
   ```

   The sender reaches the bridge in-cluster at
   `http://notification-bridge.notifications-prd.svc:8090`.
4. **The catalog.** The `registrations` row gains the new keys, the sender's leaf row the copy,
   and SecretRotator's seed both ([openbao.md § A new leaf](openbao.md#a-new-leaf)).
5. **The icon**: a 128-pixel square PNG in NotificationsDeploy `chart/files/icons/`. The render
   test holds the icons' total under 190,000 bytes.
6. **The values entry** in NotificationsDeploy `config/prd/values.yaml`: the registration under
   `registrations`, each secret a whole `$NAME` placeholder, and one line per key under
   `externalSecrets.secrets.registrations.data`, the key under its placeholder's upper-snake name
   (`<registration>-telegram-chat-ids` as `<REGISTRATION>_TELEGRAM_CHAT_IDS`). Run `kc project test`
   there, then push. The changed ConfigMap and ExternalSecret restart the pod, with a short gap.
7. Subscribe the phone to the topic if it is a new one ([Subscribing a phone](#subscribing-a-phone)).

## Seeding the Jenkins adapter's leaf

The Jenkins adapter, the Deployment `jenkins-notifications-bridge` in `jenkins-prd`, reads its two
keys from `eso/prd/jenkins-notifications-bridge/prd/config`, through the ExternalSecret
`jenkins-notifications-bridge-secrets`:

| Key | What it is | Seeded from |
|---|---|---|
| `jenkins-token` | The Jenkins API token the adapter reads builds with | This leaf: block 1 reads it back before it writes |
| `notifications-token` | The `jenkins` registration's bearer token, which the adapter submits under | `eso/prd/notifications/prd/registrations#jenkins-token` |

Seed it again once block 3 of [Seeding both leaves](#seeding-both-leaves) has given the `jenkins`
registration a new token, so the adapter's copy follows. Three blocks, in one shell set up as in
[Conventions](#conventions).

1. **The leaf.** `put`, which writes the leaf whole. It answers with the leaf's path and its
   metadata.

   ```bash
   (
     set -eo pipefail
     src() { bao kv get -mount=kv -field="$2" "eso/prd/$1"; }
     jt=$(src jenkins-notifications-bridge/prd/config jenkins-token)
     nt=$(src notifications/prd/registrations jenkins-token)
     JT=$jt NT=$nt jq -n '{"jenkins-token": env.JT, "notifications-token": env.NT}' \
       | bao kv put -mount=kv eso/prd/jenkins-notifications-bridge/prd/config -
   )
   ```

2. **Check.** It prints `{"jenkins-token":true,"notifications-token":true}` when both keys hold a
   value.

   ```bash
   bao kv get -mount=kv -format=json eso/prd/jenkins-notifications-bridge/prd/config | jq -c '.data.data | map_values(length > 0)'
   ```

3. **The adapter.** It takes the new token once its ExternalSecret has synced and it has
   restarted.

   ```bash
   jw() { cexec iac kubectl --kubeconfig "$HOME/.kube/config-prd-write" --context prd -n jenkins-prd "$@" </dev/null; }
   jw annotate externalsecret jenkins-notifications-bridge-secrets force-sync="$(date +%s)" --overwrite
   jw rollout restart deployment/jenkins-notifications-bridge
   ```

## When ntfy or the bridge is down

`NtfyDown` and `NotificationBridgeDown` are critical. They reach the Homelab Alerts group through
Alertmanager's one Telegram receiver, `telegram-direct`, never through the bridge. Alertmanager's
other alerts go through the bridge, so while either is down they reach neither the phone nor the
group, and [`AlertmanagerWebhookFailing`](#alertmanagerwebhookfailing-fires) fires on the same
direct receiver.

One pod serves both Services, and it is ready only while ntfy, the bridge and nginx all pass their
readiness checks. While ntfy fails its health check, the bridge's API is refused as well, though
the bridge runs, and Telegram delivers nothing through it. Only `NtfyDown` fires: the scrape
follows the endpointslices, which keep listing the not-ready pod, so it keeps scraping the bridge.
The same holds the other way: while the bridge fails its check, the phones cannot reach ntfy
either, and only `NotificationBridgeDown` fires.

First look:

```bash
k get pods,externalsecrets
k describe pod -l app.kubernetes.io/name=notifications
k logs deploy/notifications -c ntfy --tail=50                  # add --previous after a restart
k logs deploy/notifications -c notification-bridge --tail=50
```

| What you see | Why | What to do |
|---|---|---|
| An ExternalSecret not `SecretSynced`; a container in `CreateContainerConfigError` | A leaf or a key it reads is missing in OpenBao | [Seed it](#seeding-both-leaves) and force the sync, as that section ends; the container starts once the Secret is there |
| The bridge's log ends `notification-bridge refuses to start: <why>` | Its registrations or their variables | The why names it; the refusals are bridge-configuration.md's [§ Refusals](https://github.com/pvginkel/Notifications/blob/main/docs/bridge-configuration.md#refusals) |
| The bridge's log ends `unable to open database file` | Its state directory is missing or not writable | The `bridge-state` init container's log, `k logs deploy/notifications -c bridge-state` |
| The pod `Pending` or `ContainerCreating`, with volume events | The state volume does not attach | The events name it. The Deployment's `Recreate` strategy starts the new pod only after the old one has gone |
| The Application not Synced or not Healthy | The sync or its hook | [argocd § Diagnosing a failed sync](argocd.md#diagnosing-a-failed-sync); the app at `https://argocd.home/applications/notifications-prd` |

A restart: `kw rollout restart deployment/notifications`.

### NtfyDown fires

ntfy's metrics target, the Service `ntfy`'s port 9090, has been down for 10 minutes. The phones
get nothing, and the bridge's API is down with it. Read ntfy's log first: ntfy exits at start on
an auth config it cannot read, which is the `ntfy-auth` leaf's (block 2 of
[Seeding both leaves](#seeding-both-leaves)). Then the table above.

### NotificationBridgeDown fires

The bridge's target, the Service `notification-bridge`'s port 8090, has been down for 10 minutes:
the container is not running or does not answer. Every sender's submit fails, and while the bridge
fails its readiness check the phones cannot reach ntfy either. Read the bridge's log first, then the
table above.

## NotificationDeliveryFailing fires

A registration's channel answered `error` in the last 15 minutes. The alert's `registration` and
`channel` labels say which. The bridge neither queues nor retries, so each failed call is a
notification that channel did not show or did not take down. Each submit and dismiss logs one JSON
line, at `warning` when a channel failed, with each channel's result and its why:

```bash
k logs deploy/notifications -c notification-bridge --since=1h | grep '"level": "warning"'
```

The why's shape is in Notifications' README,
[§ The per-channel result](https://github.com/pvginkel/Notifications/blob/main/README.md#the-per-channel-result).

- **Telegram, `HTTP 401`**: the bot's token in the `registrations` leaf is not the bot's. It is a
  copy, so copy it again from the bot's leaf ([The two leaves](#the-two-leaves)), then force the
  sync and restart.
- **Telegram, `HTTP 403` or `HTTP 400`**: the bot was removed from the chat, or a chat id is wrong.
  The why names the chat.
- **ntfy, not reached or `HTTP 5xx`**: ntfy itself; [NtfyDown](#ntfydown-fires).

`http://prometheus.home` shows the failures per registration and channel:
`sum by (registration, channel) (rate(notifications_total{outcome!="ok"}[15m]))`.

## AlertmanagerWebhookFailing fires

Alertmanager gave up on a delivery to one of its webhooks in the last 15 minutes. Two of them are
the paths of the adapter sidecar `alertmanager-notifications-bridge` in the pod
`prometheus-prd-alertmanager-0`, on the pod's loopback: `notifications`, which takes every alert
but `Heartbeat` and the three direct ones, one notification per alert group, and
`notifications-oneshot`, Argo CD's two events. The adapter submits under the `alertmanager` registration, with the token it reads on every call
from the Secret `alertmanager-notifications`, a copy of `alertmanager-token` in the leaf
`eso/prd/prometheus/prd/notifications`. The third is `healthchecks`, the heartbeat's ping. The
alert reaches the group through `telegram-direct` and carries no receiver label: Alertmanager's
log names the receiver first in each failure's `err`, and the adapter's JSON lines carry the
bridge's status as `bridge_status` and its own answer as `status`, never the token.

```bash
p logs prometheus-prd-alertmanager-0 -c alertmanager --since=1h | grep 'Notify for alerts failed'
p logs prometheus-prd-alertmanager-0 -c alertmanager-notifications-bridge --since=1h | grep '"level": "warning"'
```

Alertmanager retries a 5xx until the group's interval runs out and a 4xx not at all. It records
only a delivery that went through, so once the fault is gone each group goes out at its next
flush, every 5 minutes on the root route.

| What you see | Why | What to do |
|---|---|---|
| `healthchecks` in the `err` | healthchecks.io refused the heartbeat's ping or did not answer | Nothing of the adapter's: healthchecks.io's status |
| The adapter answered `503`, `the bridge: …` | The bridge did not answer within 120 s, or could not be reached | [When ntfy or the bridge is down](#when-ntfy-or-the-bridge-is-down) |
| The adapter answered `502` | The bridge answered 5xx | The bridge's log, `k logs deploy/notifications -c notification-bridge --since=1h` |
| The adapter answered `503`, naming the token file | The Secret `alertmanager-notifications` is missing or empty. Its volume is optional, so the pod starts without it | `p get externalsecret alertmanager-notifications`; seed the leaf as for `401`, then force the sync |
| The adapter answered `401`, `unknown bearer token` | The copy is not the registration's token | Copy it again: [Adding a registration](#adding-a-registration) step 3, with `reg=alertmanager`, the leaf `prometheus/prd/notifications` and the key `token`. Then force the sync |
| The adapter answered `400` or `413`, with a `bridge_status` | The bridge refused the submit; `result` says why | Notifications' README, [§ Status codes](https://github.com/pvginkel/Notifications/blob/main/README.md#status-codes) |
| No line from the adapter, and the pod not ready | The sidecar is not running. Alertmanager's Service then has no ready endpoint, so `http://alertmanager.home` answers nothing and Argo CD's events do not arrive; Prometheus still sends Alertmanager its alerts | `p describe pod prometheus-prd-alertmanager-0`, then the adapter's log with `--previous` |

Forcing the sync, `pw annotate externalsecret alertmanager-notifications force-sync="$(date +%s)"
--overwrite`, needs no restart: the kubelet updates the mounted file in place, and the adapter
reads it on its next call.

## Restore

The platform's state is the RBD image `notifications-prd-state`, behind the PV
`notifications-prd-state-pv`, and the two leaves the Secrets `ntfy-auth` and `registrations` come
from. Nothing else holds any. No backup stream covers the image
([backup-freshness](backup-freshness.md)).

- **The pod, the namespace or the Application gone, the image whole.** Sync the Application, or
  register it again ([argocd § Registering, undeploying and unregistering an
  app](argocd.md#registering-undeploying-and-unregistering-an-app)). The PreSync hook's apply
  reattaches the `Retain` PV, which still names the chart's claim, so the pod finds its files.
  ESO makes both Secrets again from the leaves.
- **The image gone.** Nothing restores its content. The next sync's apply creates an empty image,
  and the pod's init containers make its two directories. ntfy writes its users and token from
  `ntfy-auth` at every start, so logins work at once. What is lost is ntfy's message cache, the
  last 24 hours, and the bridge's open sequences: a later submit under a lost sequence id sends a
  new Telegram message and a dismiss leaves the old one, while ntfy still updates and clears in
  place (Notifications README,
  [§ Sequence ids](https://github.com/pvginkel/Notifications/blob/main/README.md#sequence-ids)).
- **A leaf gone.** Restore OpenBao ([openbao.md](openbao.md)), or [seed](#seeding-both-leaves)
  the leaf again. A new seeding gives new values, with what [Seeding both
  leaves](#seeding-both-leaves) says follows from them. Block 3 reads the Jenkins bot's token and
  chat ids back from `registrations`, their primary, so it cannot seed a `registrations` leaf that
  is gone: restore OpenBao for that one.
