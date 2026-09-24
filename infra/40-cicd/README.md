# 40-cicd — GitHub Actions deploys, without a single AWS key

`git push origin main` → checks → ARM64 image in ECR → `alembic upgrade head`
as a Job → rolling restart of the API and the worker → `GET /health` must
answer 200.

## What this layer creates

| Resource | Why |
|---|---|
| `aws_iam_openid_connect_provider.github` | Lets AWS trust a GitHub-signed token instead of a stored access key |
| `aws_iam_role.deploy` | What a workflow run becomes for one hour |
| `aws_iam_role_policy.deploy` | ECR push to two repos, three SSM parameters, one KMS key, one port-forward to one instance |

An account holds **one** OIDC provider per URL. If another project already made
GitHub's, set `create_oidc_provider = false` and this layer adopts it.

## Who can assume the role

`var.allowed_subjects` — exact `sub` claims, no wildcards, validated by the
variable itself. Without the `sub` condition the provider trusts *GitHub*, which
means every repository on github.com. `repo:owner/*` or a trailing `:*` is the
same hole in a friendlier shape: a stranger opening a pull request runs a
workflow on your branch protection's blind side and pushes to your registry.

Two subjects are listed because a job that names `environment: production` gets
`repo:owner/repo:environment:production` in the claim instead of the `ref:` one.
`build` has no environment; `deploy` does.

## When a run cannot assume the role

The action prints ten `Assuming role with OIDC` lines and then one error, and the error
is not specific enough to act on. CloudTrail is, because it records the claim STS was
actually handed:

```bash
aws cloudtrail lookup-events --region ap-south-1 --max-results 1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity \
  --query 'Events[].CloudTrailEvent' --output text | python3 -m json.tool | grep -E 'userName|errorMessage'
```

`userName` is the `sub` verbatim. Compare it to the trust policy:

```bash
aws iam get-role --role-name recallai-github-deploy \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition' --output json
```

Two failures read almost the same and are not:

| Error | Meaning |
|---|---|
| `Request ARN is invalid` | the `AWS_DEPLOY_ROLE_ARN` string is malformed -- a trailing `%` copied out of zsh, a space, a newline |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | the ARN is well-formed; the `sub` or the `aud` does not match |

The second one bit this repository: GitHub mints **immutable-id subjects**, so the claim
is `repo:owner@91155437/recall-ai@1341938364:ref:refs/heads/main`, not
`repo:owner/recall-ai:ref:refs/heads/main`. `github_owner_id` and `github_repo_id` are
what build the long form; set them to `""` for an account still using the short one.

## What the role deliberately cannot do

Terraform state, IAM, EC2, the uploads bucket, and `/recallai/app/*` — the
application's real secrets. CI ships an image; it has never needed the OpenAI
key or the database URL. Infrastructure stays a person running terraform.

## How kubectl reaches a cluster with no public API

Ports 22 and 6443 are shut on the security group, so the runner forwards 6443
over SSM (`AWS-StartPortForwardingSession`, the one document the role may use,
against the one instance id published by 30-compute) and talks to
`127.0.0.1:6443`.

It authenticates as `github-deploy`, a ServiceAccount scoped to the `recallai`
namespace (`k8s/70-deploy-rbac.yaml`): no secrets verbs, nothing cluster-scoped,
no `delete` on Deployments. The node's own kubeconfig is cluster-admin and is
never copied anywhere.

Said plainly rather than overclaimed: RBAC guards the API, not the kubelet.
Anything that can create a Pod in a namespace can mount that namespace's Secrets
and print them. The real boundary is that only `main` can assume the role at all.

## One-time setup

```bash
# 1. The AWS side
cd infra/40-cicd
terraform init
terraform apply
terraform output -raw deploy_role_arn

# 2. The GitHub side (repo VARIABLE, not a secret -- an ARN is not sensitive)
gh variable set AWS_DEPLOY_ROLE_ARN --body "$(terraform output -raw deploy_role_arn)"

# 3. The cluster side -- needs scripts/tunnel.sh running in another terminal
cd ../..
./scripts/cicd-kubeconfig.sh      # applies the RBAC, stores the kubeconfig in SSM
```

Then push to `main`.

## Changing an app secret

Still by hand, and still two commands — CI cannot read or write them:

```bash
./scripts/env-to-ssm.sh .env.production   # laptop -> Parameter Store
./scripts/ssm-to-k8s.sh                   # Parameter Store -> Kubernetes Secret
kubectl -n recallai rollout restart deploy/recall-api deploy/recall-worker
```

## Rolling back

Every image is tagged with its commit and ECR tags are immutable, so a rollback
is a deploy of an older tag:

```bash
gh workflow run deploy.yml -f image_tag=<previous-commit-sha>
```

The build step notices the image already exists and skips straight to the
rollout.

## Not wired up on purpose

- **Terraform in CI.** A role that can run `terraform apply` is an admin role,
  and the blast radius of a compromised workflow becomes the whole account.
- **The namespace and the ClusterIssuers.** Cluster-scoped bootstrap, applied
  once by a person.
- **Pinning actions by commit SHA.** `@v4` follows a moving tag. Pinning digests
  is the hardening step to take when this repo has more than one writer.
