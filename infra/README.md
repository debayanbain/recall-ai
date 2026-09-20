# RecallAI on AWS — Phase 0 and Phase 1

Two layers, one AWS account, ~$200 of credits over six months.

- **Layer 1 (always-on):** EC2 spot + K3s. Your resume URL. Runs 24/7.
- **Layer 2 (burst):** EKS via Terraform. Created when you practise, destroyed after.

This document covers **Phase 0** (account hardening) and **Phase 1** (the foundation
stack both layers sit on). Do not skip Phase 0. It is 30 minutes and it is the
difference between a controlled experiment and a surprise bill.

---

## PHASE 0 — Account hardening (30 minutes, $0)

### 0.1 Lock the root account

Sign in as root one last time and do these, in order:

1. **Enable MFA on root.** IAM → Security credentials → Assign MFA device.
2. **Delete any root access keys.** There should be zero. If one exists, delete it.
3. **Create an IAM user** called `debayan-admin` with `AdministratorAccess`,
   console access, and its own MFA.
4. Sign out of root. **You should never sign in as root again** except for
   billing settings and account closure.

Interview answer: *"Root is break-glass only. Day-to-day work goes through an IAM
principal with MFA, so a compromised credential has a blast radius and an audit trail."*

### 0.2 Configure the CLI

```bash
aws configure --profile recallai
#   AWS Access Key ID:     <from debayan-admin>
#   Secret Access Key:     <from debayan-admin>
#   Default region name:   ap-southeast-1
#   Default output format: json

export AWS_PROFILE=recallai        # add to ~/.zshrc
aws sts get-caller-identity        # must print your account id and the admin user ARN
```

Pick **ap-southeast-1 (Singapore)** and never leave it. Resources created in a region you
do not look at are resources that bill silently for six months.

### 0.3 Budgets — do this BEFORE creating anything

Billing and Cost Management → Budgets → Create budget → Cost budget.

Create **four** alerts on one monthly cost budget, at actual spend of:

| Threshold | What it means |
|---|---|
| $1 | Proves the alert pipeline works. You should get this email in week 1. |
| $50 | 25% of credits gone. Check you are on plan. |
| $100 | Half gone. If it is not month 3, something is wrong. |
| $160 | Stop and audit everything before continuing. |

Then enable **Cost Anomaly Detection** (free) with an email monitor.

Creating a cost budget is also one of the five activities that earns $20 of the
second $100 in credits — so this step pays for itself.

### 0.4 The three things that will destroy this project

1. **Do not create or join an AWS Organization. Do not enable Control Tower.
   Do not join the AWS Partner Network.** Any of these expires your Free Tier
   credits *immediately* and force-upgrades you to a paid plan. Half the "AWS
   best practice" blogs open with "first, set up Organizations". Not here.
2. **Do not assume the old free tier applies.** You are on the credit-based
   Free plan. There is no 750-hours-of-t3.micro grant. Every EC2 hour draws
   down your $200. Your bill will read **$0.00** the entire time while credits
   silently drain — so the *credit balance* is the number you watch, not the bill.
3. **Do not leave EKS running overnight.** $0.10/hour for the control plane
   alone, whether or not a single pod is scheduled.

### 0.5 Verify your account can create a load balancer

New AWS accounts are sometimes blocked from creating ELBs until they have billing
history. You do not want to discover that in Phase 5 with a half-built cluster.

Find out now, in the console: EC2 → Load Balancers → Create → Application Load
Balancer. Fill in the minimum, create it, confirm it appears, then **delete it
immediately**. Five minutes of ALB time costs about $0.002.

If it fails with *"This AWS account currently does not support creating load
balancers"*, open a support case now — it takes a day or two to clear.

### 0.6 Check your credit balance

Billing and Cost Management → Credits. Write down the number and the expiry date.
Check this page on the 1st of every month against the plan:

| Checkpoint | Credits consumed should be under |
|---|---|
| End of month 1 | $25 |
| End of month 2 | $45 |
| End of month 3 | $65 |
| End of month 6 | $140 |

Ahead of that curve? Cut **Layer 2 hours**, not Layer 1.

---

## PHASE 1 — The foundation stack (2–3 hours, ~$0.15/month)

### What you are building and why it is separate

Three Terraform stacks, three separate state files:

| Stack | Contains | Lifecycle |
|---|---|---|
| `bootstrap/` | The S3 bucket holding all other state | Created once, never touched |
| `00-foundation/` | VPC, subnets, ECR, S3, SSM, GitHub OIDC | Applied once, never destroyed |
| `20-burst/` (Phase 5) | EKS, node group, ALB | `apply` and `destroy` repeatedly |

**Separate state files are the entire safety design.** `terraform destroy` in
`20-burst/` is *physically incapable* of touching your resume URL, because those
resources are not in its state. One monolithic state plus a mistyped `-target`
takes down your live demo.

That is the answer to *"how do you manage Terraform state across environments?"* —
state boundaries follow blast radius, not folder aesthetics.

Nothing in `00-foundation` bills by the hour. A VPC, an internet gateway, subnets,
route tables, an S3 gateway endpoint, IAM roles and SSM Standard parameters are all
free. You pay only for stored bytes in S3 and ECR.

### 1.1 Bootstrap the state bucket

```bash
cd infra/bootstrap
terraform init
terraform apply
```

Copy the `state_bucket_name` output.

This stack keeps its state **locally**, in `infra/bootstrap/terraform.tfstate`.
That is the chicken-and-egg answer: something has to create the bucket before a
bucket exists to store state in. Keep that file — it is tiny and only describes
one bucket.

### 1.2 Point the foundation stack at that bucket

```bash
cd ../00-foundation
sed -i '' 's/BUCKET_NAME_FROM_BOOTSTRAP/<paste-the-name-here>/' versions.tf

cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars and set github_repo = "yourname/recallai"
```

### 1.3 Init, review, apply

```bash
terraform init          # downloads providers, configures the S3 backend
terraform fmt -check    # formatting gate — CI should run this too
terraform validate      # syntax and type checking, no AWS calls
terraform plan -out=tf.plan
```

**Read the plan before applying.** Not as a ritual — as the habit that separates
people who use Terraform from people who run it. You are looking for:

- Roughly 30 resources to add, 0 to change, 0 to destroy.
- No `aws_nat_gateway` anywhere. If one appears, something is wrong.
- The two subnets are in two *different* availability zones.

```bash
terraform apply tf.plan
terraform output
```

### 1.4 Set the real secret values

Terraform created the parameter *names* with the value `PLACEHOLDER` and now
ignores the value forever. You set the real ones:

```bash
P=/recallai/prod

aws ssm put-parameter --name "$P/DATABASE_URL"        --value 'postgresql+asyncpg://...neon...' --type SecureString --overwrite
aws ssm put-parameter --name "$P/SECRET_KEY"          --value "$(openssl rand -hex 32)"         --type SecureString --overwrite
aws ssm put-parameter --name "$P/TOKEN_ENCRYPTION_KEY" --value "$(openssl rand -hex 32)"        --type SecureString --overwrite
aws ssm put-parameter --name "$P/OPENAI_API_KEY"      --value 'sk-...'                          --type SecureString --overwrite
aws ssm put-parameter --name "$P/GEMINI_API_KEY"      --value '...'                             --type SecureString --overwrite
aws ssm put-parameter --name "$P/APIFY_TOKEN"         --value '...'                             --type SecureString --overwrite

# IMPORTANT: a SECOND Telegram bot, not your live one. See the warning below.
aws ssm put-parameter --name "$P/TELEGRAM_BOT_TOKEN"  --value '...'                             --type SecureString --overwrite

# verify
aws ssm get-parameters-by-path --path "$P" --recursive --with-decryption \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table
```

**Why not put these in Terraform?** Because `terraform.tfstate` is plaintext JSON.
Anything Terraform manages the *value* of ends up sitting in that S3 bucket in the
clear. This pattern gives you the parameter tree as code and keeps the secrets out
of state.

**Why not Secrets Manager?** $0.40 per secret per month. Seven secrets is ~$20 over
six months for rotation you are not using. Parameter Store Standard is free to
10,000 parameters and External Secrets Operator reads it identically.

### 1.5 Verify

```bash
aws ec2 describe-vpcs      --filters "Name=tag:Project,Values=recallai" --query 'Vpcs[].VpcId' --output text
aws ec2 describe-subnets   --filters "Name=tag:Project,Values=recallai" --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone}' --output table
aws ecr describe-repositories --query 'repositories[].repositoryName' --output text

# This MUST return nothing. A NAT gateway is $33/month.
aws ec2 describe-nat-gateways --query 'NatGateways[?State==`available`]' --output text
```

Then commit:

```bash
git add infra/ && git commit -m "infra: foundation stack (VPC, ECR, S3, SSM, OIDC)"
```

`.gitignore` already excludes `*.tfstate`, `terraform.tfvars` and `.terraform/`.
Never commit those.

---

## The two traps that are specific to your project

**1. The Telegram webhook collision.** Webhook registration is *global per bot
token*. The moment Layer 2 registers its webhook with the same token as Layer 1,
Telegram stops delivering to Layer 1 — your resume demo goes quiet and nothing in
either log explains why. Create a second bot with `@BotFather` for Layer 2 now,
before you need it.

**2. Your boot guards will reject a sloppy deploy.** `validate_deployment_config`
refuses to start outside `ENV=dev` on a placeholder or short `SECRET_KEY`, on
`COOKIE_SECURE=false`, or on a `"*"` entry in `CORS_ORIGINS`; under `ENV=prod` every
origin must additionally be `https://` and non-local. In Kubernetes this presents as
`CrashLoopBackOff` with the reason in the logs. That is your own code working as
designed — read the pod logs before assuming the cluster is broken.

---

## What Phase 1 lets you say in an interview

> "I split Terraform state by blast radius rather than by folder convention. Shared
> networking, the registry and the secret tree live in a foundation stack that is
> applied once. The ephemeral EKS environment is a separate root module with its own
> state, so destroying it cannot reach the always-on environment. Secrets are declared
> in Terraform but their values are set out-of-band, because state is plaintext —
> Terraform owns the parameter tree, not the parameters."

That paragraph is worth more than a screenshot of a running cluster.

---

## Next: Phase 2

Layer 1 — an EC2 spot instance in an ASG, K3s, CloudFront in front, SSM Session
Manager instead of SSH, and your resume URL live. Say the word and I will build it.
