# An `external` key fell due

An `external` key is one SecretRotator never rotates. Its value changes outside the tool, by a
procedure of its own. The tool schedules it like any other key and says when it falls due. You do
the work, then press **Done**, which records it. Read this when the rotator announces one, or when
`secret-rotator ui` lists one.

Which keys are `external`, and why, is design R81 and R82 in
[`secret-rotation/design.md`](../../../AnsibleSpecs/secret-rotation/design.md). The catalog,
[`catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md), gives each key's interval and
`notes`. The `notes` are the key's procedure, or say where it is written down. Each procedure stays
in the doc that holds it. This runbook is only what comes around it.

## How it falls due

A key falls due at its rotation stamp plus its interval, and at once while it has no stamp
([openbao.md §5](openbao.md#5--rotation), the run state). Once `external` is among the nightly
run's `kinds_enabled` ([go-live](secret-rotator-go-live.md#going-live)), the nightly run announces
it as it does a `manual` key, and never starts it. A Telegram line comes 28, 21 and 14 days before
the key falls due, then every night from 13 days before it until the key is done. Once the key is
due, it is also on the standing card tagged `Rotator Standing Card`, under *Manual rotations due*.

## What to do

1. Open `secret-rotator ui` on srviac, from the VS Code task **secret-rotator ui (srviac)** or with:

   ```sh
   ssh -t ansible@srviac "sudo iac -c 'secret-rotator ui'"
   ```

2. Select the key's box, `○ <leaf>#<key> · external`. The selected box shows the key's `notes`. One
   procedure can cover several keys, such as the bootstrap tier's. Press `f` on one of those boxes
   to narrow the list to the `external` boxes.
3. Do what the notes say, outside the tool. Done writes no value, so the procedure writes every
   value it changes, in OpenBao too. Examples: the new Wi-Fi password into `shared/wifi-iot`, and
   the Terraform states' age key into `iac/tf-backend` and its copies in the KubeCoder catalog bags.
4. Press **Done** once the work is done, and not before. The stamp is the only record that the key
   was rotated. Done stamps the key with today's date and restarts its interval, and clears an
   `expires_at` the key's entry carries. Nothing else happens: no value is written, no copy is
   rewritten, nothing is restarted, and a marker leaf stays as it is. The box shows `done` and
   leaves the list.
5. Press Done on each key that the procedure rotated, one box at a time.

Every `external` key is in the list, whether it is due or not. Done on a key that is not due yet
also stamps it, so the key next falls due a full interval from today. A stamp that fails shows on
the box with `Retry` · `Abort` · `Details`, and `Retry` stamps again.

## Without the UI

`secret-rotator run <leaf>` runs the same plan in a terminal, from the VS Code task
**secret-rotator run (srviac)**. Pick the key's `external` plan when the leaf has more than one. It
prints the notes, `done` confirms, and the key is stamped.
