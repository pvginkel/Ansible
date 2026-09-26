#!/usr/bin/env python3
"""What an Argo CD cutover will NOT change on a Helm-created release.

Argo's first sync adopts live objects it renders, but it cannot remove a field the old
chart set and the new render omits: the objects carry no
`kubectl.kubernetes.io/last-applied-configuration`, so Argo's three-way merge degrades to a
two-way one whose delete bucket is empty by construction. Server-side apply does not help
either — the field is owned by the manager `helm`, and SSA only deletes a field when the
manager that owns it stops declaring it. Both proven 2026-09-20; Ansible's
docs/runbooks/argocd.md, "What a cutover does not change", has the finding.

The test is decidable: a field sticks iff `metadata.managedFields` says helm owns it and the
render never declares it. Same one level up for whole objects. This script computes both.

Usage:
    helm template <ns> chart --namespace <ns> --values config/<stage>/values.yaml \
        --set hook.repo=...,hook.revision=...,hook.stage=<stage>,hook.namespace=<ns> > render.yaml
    kubectl get <kinds> -n <ns> -o json --show-managed-fields > live.json
    ./stuck_fields.py <ns> render.yaml live.json [extra-object.json ...]

Cluster-scoped objects (Namespace, ClusterRole, ClusterRoleBinding) are fetched one at a time
and passed as extra arguments. Ports and other API-defaulted subfields are matched leniently
and `protocol` is dropped, or every defaulted field reads as a false positive.
"""

import json, yaml, sys
NS=sys.argv[1]; REND=sys.argv[2]; LIVE=sys.argv[3]; EXTRA=sys.argv[4:]
rend=[d for d in yaml.safe_load_all(open(REND)) if d]
rmap={}
for d in rend:
    n=d['metadata'].get('name') or '<generated>'
    rmap[(d['kind'],n)]=d
live=[o for o in json.load(open(LIVE))['items'] if o['metadata'].get('namespace')==NS]
for f in EXTRA:
    try: live.append(json.load(open(f)))
    except Exception: pass
def helm_entry(o):
    for f in o['metadata'].get('managedFields',[]):
        if f.get('manager')=='helm' and not f.get('subresource'): return f
def match(node,sel):
    if not isinstance(node,list): return None
    for it in node:
        if isinstance(it,dict) and all(it.get(k)==v for k,v in sel.items()): return it
    for it in node:
        if isinstance(it,dict) and any(k in it for k in sel) and all(it.get(k)==v for k,v in sel.items() if k in it): return it
def walk(fields,node,prefix=''):
    for key,sub in (fields or {}).items():
        if key=='.': continue
        child=None; missing=True
        if key.startswith('f:'):
            name=key[2:]; here=f'{prefix}.{name}' if prefix else name
            if isinstance(node,dict) and name in node: child,missing=node[name],False
        elif key.startswith('k:'):
            sel=json.loads(key[2:]); here=prefix+'['+','.join(f'{k}={v}' for k,v in sel.items())+']'
            child=match(node,sel); missing=child is None
        elif key.startswith('v:'):
            val=json.loads(key[2:]); here=f'{prefix}[={val}]'; missing=not(isinstance(node,list) and val in node)
        elif key.startswith('i:'):
            i=int(key[2:]); here=f'{prefix}[{i}]'
            if isinstance(node,list) and len(node)>i: child,missing=node[i],False
        else: continue
        if missing: yield here
        elif sub and any(k!='.' for k in sub): yield from walk(sub,child,here)
helm_objs=[o for o in live if (o['metadata'].get('annotations') or {}).get('meta.helm.sh/release-name')==NS]
print(f'--- {NS}: {len(helm_objs)} Helm-stamped live objects, {len(rmap)} rendered ---')
miss=[o for o in helm_objs if (o['kind'],o['metadata']['name']) not in rmap]
print('A. Helm objects absent from the render:', ', '.join(f'{o["kind"]}/{o["metadata"]["name"]}' for o in miss) or 'none')
from collections import Counter
c=Counter(); detail={}
for o in helm_objs:
    k=(o['kind'],o['metadata']['name'])
    if k not in rmap: continue
    e=helm_entry(o)
    if not e: print(f'   !! {k[0]}/{k[1]} has no helm manager entry'); continue
    for p in walk(e.get('fieldsV1',{}),rmap[k]):
        if p.startswith(('metadata.creationTimestamp','status')) or p.endswith('.protocol'): continue
        lab = 'imagePullPolicy' if p.endswith('imagePullPolicy') else p
        c[lab]+=1; detail.setdefault(lab,[]).append(f'{k[0]}/{k[1]}')
print('B. Stuck fields:')
for p,n in c.most_common(): print(f'     {p:52} x{n}')
print('     TOTAL', sum(c.values()))
