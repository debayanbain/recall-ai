variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "recallai"
}

variable "github_owner" {
  description = "GitHub user or org that owns the repository"
  type        = string
  default     = "debayanbain"
}

variable "github_repo" {
  description = "Repository name, without the owner"
  type        = string
  default     = "recall-ai"
}

# The sub claim is the WHOLE access control on this role. A wildcard here
# ("repo:owner/*" or a trailing ":*") means any branch, any pull request and any
# fork's PR workflow may assume it -- which is a stranger pushing a branch and
# getting your ECR push rights. Every entry must name one ref or one environment.
variable "allowed_subjects" {
  description = "Exact GitHub OIDC sub claims permitted to assume the deploy role"
  type        = list(string)
  default = [
    "repo:debayanbain/recall-ai:ref:refs/heads/main",
    "repo:debayanbain/recall-ai:environment:production",
  ]

  validation {
    condition     = alltrue([for s in var.allowed_subjects : !strcontains(s, "*")])
    error_message = "No wildcards in allowed_subjects: a '*' opens the role to forks and PR branches."
  }
}

# An AWS account holds at most ONE provider per URL. If another project already
# created GitHub's, set this false and this layer reads the existing one instead
# of failing with EntityAlreadyExists.
variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider, or adopt the account's existing one"
  type        = bool
  default     = true
}

variable "ecr_repos" {
  description = "Repos this role may push to. Must match 20-data."
  type        = list(string)
  default     = ["recallai-api", "recallai-web"]
}

variable "role_name" {
  type    = string
  default = "recallai-github-deploy"
}
