# A service in Kubernetes

This page runs an agent's web service as a Deployment and the runner as a Job in a cluster; how to start the service and its settings are on [An agent inside a web service](services.md).

## The token and the key in a Kubernetes pod

In a pod, give the token and the control key to Scrimmage as files from a Secret volume, never as `env` with `secretKeyRef`.
An environment variable is inherited by every program the agent starts, and `kubectl exec <pod> -- env` shows it.
A service with `SCRIMMAGE_TOKEN` in its environment stops, and the runner reads `SCRIMMAGE_TOKEN_FILE` only.

```yaml
securityContext:
  runAsUser: 10001
  runAsNonRoot: true
  fsGroup: 10001
containers:
  - name: agent
    env:
      - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
    volumeMounts:
      - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
volumes:
  - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
```

The kubelet writes a Secret file as root, with the pod's `fsGroup` as its group and mode `440`, and `token` is a link to `..data/token`.
Scrimmage still reads a plain file you own with mode `600` first.
When the path is a Secret volume's link, it reads the file only if all of these hold:

- the path is the kubelet's own link, `token -> ..data/token`, and `..data` is a link to a folder of the same directory; a link to anywhere else is refused;
- the process is not root;
- root owns the file;
- the file's group is one of the process's groups, which `fsGroup` adds;
- the mode has no bit for others, none to write and none to run: `400` or `440`;
- the volume is mounted read-only.

Each refusal names the setting that fixes it: `securityContext.fsGroup`, `runAsUser` and `runAsNonRoot: true`, or the volume's `defaultMode`.
A plain file owned by another user, and a plain file of mode `640`, are still refused.

## In Kubernetes

In a cluster, the runner runs as a Job, and the service runs as a Deployment of two replicas behind a Service.
Every secret arrives as a Secret volume.
This repository tests the example service this way on kind, with the files below.
kind is the Kubernetes project's own test cluster; the files are the same on a production cluster.

1. One namespace holds the test, and enforces the Pod Security `restricted` standard.
2. `scrimmage cert` makes the control server's certificate, and `kubectl create secret` stores it in two Secrets.
3. A Secret, `scrimmage-token`, holds the token.
4. The agent runs as the Deployment `acme-agent`, two replicas of `uvicorn --workers 2`, behind the Service `acme-agent` on port 8000.
5. The runner runs as the Job `scrimmage-test`, behind the Service `scrimmage` on port 7700.
   It writes its records to a volume claim, `scrimmage-records`.
6. NetworkPolicies deny everything in the namespace, then allow the agent to reach the runner on 7700, the runner to reach the agent on 8000, the agent to reach the model, and every pod to reach cluster DNS.
7. The agent's pods reach the runner at `https://scrimmage:7700`, and the runner reaches the agent at `http://acme-agent:8000/chat`.

The cluster needs two things:

- a network plugin that enforces NetworkPolicy, such as Calico, which the tests use;
- a storage class for the records: the tests use `standard`, kind's local-path class, which is for tests only, and a production cluster names a CSI class, such as the EBS one.

`TAG` in an image name stands for your image's tag, and `NAMESPACE` for the test's namespace.
`imagePullPolicy: Never` runs only an image already on the node; use your registry's policy instead.

### The namespace and the accounts

```yaml title="namespace.yaml"
# The namespace one test runs in. The harness replaces NAMESPACE with s8-<random>.
# enforce refuses a pod that breaks the restricted standard. A Deployment or a Job that breaks it
# is still admitted and only its pods are refused, so warn prints the problem when the workload
# is applied, and audit records it. The version is the cluster's own minor, pinned.
apiVersion: v1
kind: Namespace
metadata:
  name: NAMESPACE
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.36
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: v1.36
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/audit-version: v1.36
```

None of the accounts has a Role, and no pod gets an API token: the runner never calls the Kubernetes API, and a pod needs no rights to mount a Secret.

```yaml title="service-accounts.yaml"
# No Role and no RoleBinding for any of them, and no API token mounted into a pod.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: scrimmage-runner
automountServiceAccountToken: false
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: acme-agent
automountServiceAccountToken: false
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fake-model
automountServiceAccountToken: false
```

### The certificate

Make the certificate with [`scrimmage cert`](cli.md#scrimmage-cert), as for Docker, once per test namespace, on the machine that applies the manifests.
It names the three names the agent's pods may reach the runner by: `scrimmage`, which `SCRIMMAGE_CONTROL_URL` holds, and its two longer forms.

```bash
NS=my-test
rm -rf .scrimmage-cert
scrimmage cert --name scrimmage --name scrimmage.$NS.svc --name scrimmage.$NS.svc.cluster.local \
  --days 1 --out .scrimmage-cert && \
kubectl create secret tls scrimmage-control-tls -n $NS \
  --cert .scrimmage-cert/server.pem --key .scrimmage-cert/server.key && \
kubectl create secret generic scrimmage-ca-cert -n $NS --from-file=ca.crt=.scrimmage-cert/ca.pem
rm -rf .scrimmage-cert
```

The first `rm -rf` clears an old folder, which `scrimmage cert` would refuse.
The `&&` stops at the first command that fails, so no Secret is made from files that were not written.
The last `rm -rf` is a line of its own, so the key is removed even when a create fails.
The block is safe to paste into a shell; it never exits it.

`scrimmage-control-tls` holds `tls.crt` and `tls.key` for the runner, and `scrimmage-ca-cert` holds only `ca.crt` for the agent.
No key is ever in the agent's pod.

- The authority's key is never written: `scrimmage cert` drops it once the server certificate is signed, so nothing in or out of the cluster can sign another control certificate.
- The authority is limited to the three names given and the names under them, and it excludes every IP address, so it is good for this namespace only.
- The server's key passes through the machine that runs `scrimmage cert`, in a folder only you can read, and through the API call of `kubectl create secret`.
  The last `rm -rf` removes it from the machine, whatever the creates did.

The tests make the two Secrets with these same commands.

#### When the certificate expires

`--days 1` because a Job runs one test, and the certificate is good for 24 hours.

- A runner that starts with an expired certificate exits 2 at its start, naming `control_tls`.
- A test that outlives the 24 hours has every new handshake with the runner refused.

To make a new one:

1. Delete the two Secrets: `kubectl delete secret scrimmage-control-tls scrimmage-ca-cert -n $NS`.
2. Run the certificate block above again, all of it.
3. Restart the agent's pods: `kubectl rollout restart deployment/acme-agent -n $NS`.
   A new certificate comes from a new authority, and the SDK reads `SCRIMMAGE_CONTROL_CA` once, at its start.

`kubectl create` refuses a Secret that exists, so the delete comes first.

### With cert-manager

A team that already runs cert-manager, the CNCF project many clusters issue certificates with, can make the same two Secrets with it instead.
The agent and the Job read the same two Secrets either way, so no other manifest changes.

The authority and its Issuer live in a namespace of their own, `scrimmage-pki`, made once per cluster.
cert-manager signs every request an Issuer gets, so an Issuer in the test's namespace would sign for the control name for anyone who can make a `Certificate` there.

```yaml title="pki-authority.yaml"
# The certificate authority for the control channel, made once per cluster in scrimmage-pki.
# No test namespace holds an Issuer.
apiVersion: v1
kind: Namespace
metadata:
  name: scrimmage-pki
---
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: scrimmage-selfsigned
  namespace: scrimmage-pki
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: scrimmage-ca
  namespace: scrimmage-pki
spec:
  isCA: true
  commonName: scrimmage-ca
  secretName: scrimmage-ca
  duration: 43800h
  # A DNS constraint permits a name and every name that ends with it. scrimmage is the host in
  # SCRIMMAGE_CONTROL_URL, svc covers scrimmage.<namespace>.svc and cluster.local covers
  # scrimmage.<namespace>.svc.cluster.local. Every IP address is excluded. A client that checks
  # the constraint refuses any other name or any address this authority signs.
  nameConstraints:
    critical: true
    permitted:
      dnsDomains:
        - scrimmage
        - svc
        - cluster.local
    excluded:
      ipRanges:
        - 0.0.0.0/0
        - "::/0"
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Never
  issuerRef:
    name: scrimmage-selfsigned
    kind: Issuer
---
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: scrimmage-ca
  namespace: scrimmage-pki
spec:
  ca:
    secretName: scrimmage-ca
```

One certificate per test namespace, with `NAMESPACE` filled in:

```yaml title="pki-control.yaml"
# The control server's certificate for one test namespace. The harness fills in NAMESPACE.
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: scrimmage-control-NAMESPACE
  namespace: scrimmage-pki
spec:
  secretName: scrimmage-control-NAMESPACE
  duration: 24h
  privateKey:
    algorithm: ECDSA
    size: 256
  usages:
    - server auth
    - digital signature
  dnsNames:
    - scrimmage
    - scrimmage.NAMESPACE.svc
    - scrimmage.NAMESPACE.svc.cluster.local
  issuerRef:
    name: scrimmage-ca
    kind: Issuer
```

Copy its Secret into the test's namespace as the same two Secrets.

```bash
SOURCE="secret/scrimmage-control-$NS -n scrimmage-pki"
kubectl create secret generic scrimmage-control-tls -n $NS \
  --from-file=tls.crt=<(kubectl get $SOURCE -o jsonpath='{.data.tls\.crt}' | base64 -d) \
  --from-file=tls.key=<(kubectl get $SOURCE -o jsonpath='{.data.tls\.key}' | base64 -d)
kubectl create secret generic scrimmage-ca-cert -n $NS \
  --from-file=ca.crt=<(kubectl get $SOURCE -o jsonpath='{.data.ca\.crt}' | base64 -d)
```

The key passes through a pipe, never a file.
The authority lives 5 years and keeps its key, so a renewed server certificate never changes what the agent trusts.
Its name constraint permits `scrimmage`, `svc` and `cluster.local`, so unlike the default it covers every namespace; [Security](security.md#with-cert-manager) says what follows.
The tests install cert-manager only for the two tests of this option.

### The token

Make the token Secret from standard input, so the token is on no command line and in no file:

```bash
python3 -c "import secrets; print(secrets.token_hex(32), end='')" \
  | kubectl create secret generic scrimmage-token -n $NS --from-file=token=/dev/stdin
```

### The records volume, the Services and the policies

The records outlive the Job's pod on the claim, and are read back by the reader pod below.
Records hold your test's data: delete the claim when you no longer need them.

```yaml title="records-pvc.yaml"
# The class "standard" is kind's local-path provisioner. A production cluster names a CSI class.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: scrimmage-records
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests:
      storage: 1Gi
```

```yaml title="services.yaml"
apiVersion: v1
kind: Service
metadata:
  name: scrimmage
spec:
  selector:
    app.kubernetes.io/name: scrimmage-runner
  ports:
    - {name: control, port: 7700, targetPort: control}
---
apiVersion: v1
kind: Service
metadata:
  name: acme-agent
spec:
  selector:
    app.kubernetes.io/name: acme-agent
  ports:
    - {name: http, port: 8000, targetPort: http}
---
apiVersion: v1
kind: Service
metadata:
  name: fake-model
spec:
  selector:
    app.kubernetes.io/name: fake-model
  ports:
    - {name: http, port: 8080, targetPort: http}
```

```yaml title="network-policies.yaml"
# Deny everything, then allow four paths: the agent to the runner on 7700, the runner to the
# agent on 8000, the agent to the fake model on 8080, and every pod to cluster DNS.
# A policy selects one set of pods, so each path is two policies: egress on the sender, ingress
# on the receiver. Calico checks a policy after a Service's address is translated, so the rules
# name pods and ports, never Service addresses.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dns
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: kube-system}
          podSelector:
            matchLabels: {k8s-app: kube-dns}
      ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-runner-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: scrimmage-runner}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: acme-agent}
      ports:
        - {protocol: TCP, port: 7700}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-runner-egress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: scrimmage-runner}
      ports:
        - {protocol: TCP, port: 7700}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: runner-to-agent-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: scrimmage-runner}
      ports:
        - {protocol: TCP, port: 8000}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: runner-to-agent-egress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: scrimmage-runner}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: acme-agent}
      ports:
        - {protocol: TCP, port: 8000}
---
# The real-model check replaces the two agent-to-model policies with TCP 443 to 0.0.0.0/0,
# except the pod and service ranges, 172.16.0.0/12 and 169.254.0.0/16, since a NetworkPolicy
# cannot name a host.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-model-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: fake-model}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: acme-agent}
      ports:
        - {protocol: TCP, port: 8080}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-to-model-egress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels: {app.kubernetes.io/name: fake-model}
      ports:
        - {protocol: TCP, port: 8080}
```

The tests run a fake model in the namespace, the `fake-model` the policies name.
With a real provider, replace the two agent-to-model policies as their comment says.

### The agent

```yaml title="agent.yaml"
# The acme order bot as a service: two replicas, each uvicorn with two workers.
# The token and the authority arrive as Secret volumes. The service refuses a token in its
# environment. The authority's Secret holds ca.crt alone, so no key is ever in this pod.
# OPENAI_API_KEY is the fake model's, not a secret.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-agent
spec:
  replicas: 2
  selector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  template:
    metadata:
      labels: {app.kubernetes.io/name: acme-agent}
    spec:
      serviceAccountName: acme-agent
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 5
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: agent
          image: scrimmage-compose-service:TAG
          imagePullPolicy: Never
          command: ["scrimmage-run", "uvicorn", "acme.service:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
          env:
            - {name: SCRIMMAGE_SERVICE, value: "1"}
            - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
            - {name: SCRIMMAGE_CONTROL_URL, value: "https://scrimmage:7700"}
            - {name: SCRIMMAGE_CONTROL_CA, value: /run/scrimmage/ca/ca.pem}
            - {name: SCRIMMAGE_TOOLS, value: "acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock"}
            - {name: SCRIMMAGE_ALLOW_IMPORTS, value: "acme."}
            - {name: OPENAI_BASE_URL, value: "http://fake-model:8080/v1"}
            - {name: OPENAI_API_KEY, value: test}
          ports: [{containerPort: 8000, name: http}]
          readinessProbe:
            httpGet: {path: /openapi.json, port: http}
            periodSeconds: 2
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
            - {name: ca, mountPath: /run/scrimmage/ca, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
        - name: ca
          secret:
            secretName: scrimmage-ca-cert
            defaultMode: 0444
            items: [{key: ca.crt, path: ca.pem}]
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 64Mi}}
```

### The agent without `scrimmage-run`

A Deployment that keeps its own `command` sets `PYTHONPATH` instead, as in [Start it without `scrimmage-run`](services.md#start-it-without-scrimmage-run).
This file is `agent.yaml` with that one change, and the tests run the same test against it.
Read the path from your own image with `docker run --rm <image> scrimmage path`.

```yaml title="agent-pythonpath.yaml"
# The agent of agent.yaml, started without scrimmage-run, for a Deployment that keeps its own
# command. PYTHONPATH begins with the folder that holds Scrimmage's sitecustomize.py, and
# SCRIMMAGE_SERVICE=1 switches the service on, which is all scrimmage-run does for a service.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: acme-agent
spec:
  replicas: 2
  selector:
    matchLabels: {app.kubernetes.io/name: acme-agent}
  template:
    metadata:
      labels: {app.kubernetes.io/name: acme-agent}
    spec:
      serviceAccountName: acme-agent
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 5
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: agent
          image: scrimmage-compose-service:TAG
          imagePullPolicy: Never
          command: ["uvicorn", "acme.service:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
          env:
            # First on the path: the folder `scrimmage path` prints in this image. Read it once with
            # docker run --rm <image> scrimmage path
            - {name: PYTHONPATH, value: /usr/local/lib/python3.12/site-packages/scrimmage/_boot}
            - {name: SCRIMMAGE_SERVICE, value: "1"}
            - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
            - {name: SCRIMMAGE_CONTROL_URL, value: "https://scrimmage:7700"}
            - {name: SCRIMMAGE_CONTROL_CA, value: /run/scrimmage/ca/ca.pem}
            - {name: SCRIMMAGE_TOOLS, value: "acme.tools:get_order,acme.tools:check_inventory,acme.tools:refund_order,acme.tools:get_stock"}
            - {name: SCRIMMAGE_ALLOW_IMPORTS, value: "acme."}
            - {name: OPENAI_BASE_URL, value: "http://fake-model:8080/v1"}
            - {name: OPENAI_API_KEY, value: test}
          ports: [{containerPort: 8000, name: http}]
          readinessProbe:
            httpGet: {path: /openapi.json, port: http}
            periodSeconds: 2
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
            - {name: ca, mountPath: /run/scrimmage/ca, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
        - name: ca
          secret:
            secretName: scrimmage-ca-cert
            defaultMode: 0444
            items: [{key: ca.crt, path: ca.pem}]
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 64Mi}}
```

### The Job and its file

The Job's file is the ConfigMap `scrimmage-config`.
`control` is the runner pod's own address, from the downward API, and the Service `scrimmage` points at it.
The pod's address is removed from the records, as is any `${NAME}` value of 8 characters or more.

```toml title="scrimmage.toml"
# The Job's file, the ConfigMap scrimmage-config. The tests below it are the example's.
# POD_IP is the runner pod's own address from the downward API, so the agent's pods reach the
# control server there. The Service `scrimmage` and the certificate's names point at it.
[agent]
service     = "http://acme-agent:8000/chat"
body        = {message = "{question}", session = "{session}"}
answer      = "reply"
tools       = ["acme.tools:get_order", "acme.tools:check_inventory", "acme.tools:refund_order", "acme.tools:get_stock"]
real        = ["tool:check_inventory", "process:uname"]
model_hosts = [{host = "fake-model", provider = "openai"}]
control     = "${POD_IP}:7700"
control_tls = {cert = "/run/scrimmage/tls/tls.crt", key = "/run/scrimmage/tls/tls.key"}
runs = 30
record_grace_s = 0.2
record_wait_s = 10
run_timeout = 30

[[test]]
name     = "order-status"
question = "Where is order 1234?"
expect   = ["shipped", ["Oct 30", "October 30"]]

[[test.mock]]
call    = "tool:get_order"
returns = "shipped, Oct 30"

[[test.mock]]
call    = "tool:get_stock"             # returns an acme.models.Stock object
returns = {sku = "SKU-42", count = 12}

[[test.fault]]
call = "tool:get_order"
do   = "timeout"
expect = [["unable", "sorry", "try again"]]
forbid = ["Oct 30", "October 30"]
```

`--records` puts the records folder inside the volume, not beside the read-only file.

```yaml title="runner-job.yaml"
# One test, one pod. backoffLimit 0: a retry would start a second test and spend twice.
# The pod's exit code is scrimmage test's: 0, 2 or 3. The result is result.json on the volume.
apiVersion: batch/v1
kind: Job
metadata:
  name: scrimmage-test
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  template:
    metadata:
      labels: {app.kubernetes.io/name: scrimmage-runner}
    spec:
      restartPolicy: Never
      serviceAccountName: scrimmage-runner
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 5
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: runner
          image: scrimmage-runner:TAG
          imagePullPolicy: Never
          args: ["test", "/etc/scrimmage/scrimmage.toml", "--records", "/records/scrimmage-runs"]
          env:
            - name: POD_IP
              valueFrom: {fieldRef: {fieldPath: status.podIP}}
            - {name: SCRIMMAGE_TOKEN_FILE, value: /run/scrimmage/token/token}
          ports: [{containerPort: 7700, name: control}]
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: config, mountPath: /etc/scrimmage, readOnly: true}
            - {name: token, mountPath: /run/scrimmage/token, readOnly: true}
            - {name: tls, mountPath: /run/scrimmage/tls, readOnly: true}
            - {name: records, mountPath: /records}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: config, configMap: {name: scrimmage-config}}
        - {name: token, secret: {secretName: scrimmage-token, defaultMode: 0400}}
        - name: tls
          secret:
            secretName: scrimmage-control-tls
            defaultMode: 0400
            items: [{key: tls.crt, path: tls.crt}, {key: tls.key, path: tls.key}]
        - {name: records, persistentVolumeClaim: {claimName: scrimmage-records}}
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 64Mi}}
```

`backoffLimit: 0` means the cluster never starts the test a second time: a failed test ends the Job as `Failed`, with one pod.

### Run it

```bash
kubectl apply --server-side -n $NS -f service-accounts.yaml -f records-pvc.yaml \
  -f services.yaml -f network-policies.yaml
kubectl create configmap scrimmage-config -n $NS --from-file=scrimmage.toml
kubectl apply --server-side -n $NS -f agent.yaml
kubectl rollout status deployment/acme-agent -n $NS
kubectl apply --server-side -n $NS -f runner-job.yaml
POD="pod -n $NS -l batch.kubernetes.io/job-name=scrimmage-test"
EXIT='{.items[0].status.containerStatuses[0].state.terminated.exitCode}'
for i in $(seq 360); do   # up to 30 minutes
  kubectl get $POD -o jsonpath="$EXIT" | grep -q . && break; sleep 5
done
kubectl get $POD -o jsonpath="$EXIT"     # 0, 2 or 3, as scrimmage test exits; empty if still running
```

Read the exit code from the pod's status, as above, and the result from the records.
`kubectl logs` mixes any line the runner writes on stderr, such as its waiting message, into its stdout, so it is for a person to read, never for a program.
The loop gives up after 30 minutes, so a pod that never ends, such as one whose image is missing, does not hang it.

### Read the records

The reader pod mounts the records volume read-only and runs nothing else.
It reads the records after the Job and its pod are deleted, too.

```yaml title="reader.yaml"
# Reads the records after the test's Job and its pod are gone. It mounts the volume read-only.
apiVersion: v1
kind: Pod
metadata:
  name: reader
  labels: {app.kubernetes.io/name: scrimmage-reader}
spec:
  serviceAccountName: scrimmage-runner
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 0
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: reader
      image: scrimmage-runner:TAG
      imagePullPolicy: Never
      command: ["sleep", "infinity"]
      resources:
        requests: {cpu: 125m, memory: 128Mi}
        limits: {memory: 256Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      volumeMounts:
        - {name: records, mountPath: /records, readOnly: true}
  volumes:
    - name: records
      persistentVolumeClaim: {claimName: scrimmage-records, readOnly: true}
```

```bash
kubectl apply --server-side -n $NS -f reader.yaml
kubectl exec -n $NS reader -- ls /records/scrimmage-runs
kubectl exec -n $NS reader -- scrimmage show /records/scrimmage-runs/<folder>
kubectl exec -n $NS reader -- cat /records/scrimmage-runs/<folder>/result.json
```

Each run's summary names the pod that served it in `claimed_by`, and both digests of its plan, as [a service run's summary line](json-output.md#a-service-runs-summary-line) says.

### When the agent cannot reach the runner

Each case below is tested in the kind cluster with the manifests above and one fault.

| The fault | What the Job's pod ends with |
|---|---|
| The agent's egress policy to the runner allows nothing | exit 3 after 30 s of asking, with the timeout's hint, as in the table above |
| The Service `scrimmage` has a selector that matches no pod | exit 3 after 30 s of asking, with the same timeout's hint |
| The same Service, in a namespace with no NetworkPolicy | exit 3 after 30 s of asking, with the refused hint: `nothing listens there, as when a Kubernetes Service has no ready endpoints; check that the Service's selector matches this runner's pod` |
| No Service named `scrimmage` in the namespace | exit 3 after 30 s of asking, with the name's hint: `the name scrimmage does not resolve where the service runs; check the host in SCRIMMAGE_CONTROL_URL and that the Service is in the service's namespace` |

A Service with no endpoints shows as a timeout here, not as a refusal.
With no endpoint, kube-proxy never translates the Service's address to a pod's, so the agent's egress policies, which allow only named pods, drop the packet before kube-proxy can refuse it.
Without an egress policy the same Service refuses the connection, and the hint names its selector.

In each case, the runner prints its waiting line once, sends no setup request and no run, and writes no record and no `result.json` to the volume.
The message is the last line of the pod's log, since there is no result to read.

A policy that cuts the agent off from the runner in the middle of a test does not stop it.
Calico checks a policy against each new connection, and every control request is a new connection, so the cut takes effect at the next request.
The run inside its request at that moment gets a reply from the service, but cannot send its last batch, so it ends `infrastructure error` with `incomplete` true in its summary.
That reply holds no answer: the run's next model call needs a budget reservation from the runner, which cannot be reached, so the call is refused as `control_unreachable`.
The reservation of the call that was in flight stays held, so the runner warns that the run ended still holding part of the budget.
Each later run's claim fails, its gate answers `503 plan-unavailable`, and it ends `infrastructure error` too, after up to 10 s of waiting for the claim.
No run after the cut ends with a result.
The test ends with exit 3, and `result.json` says how many runs were sent and how few were claimed, in `runs_sent` and `claims_received`.

