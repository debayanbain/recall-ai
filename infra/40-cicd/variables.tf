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

variable "github_branch" {
  description = "The branch whose workflow runs may deploy"
  type        = string
  default     = "main"
}

# GitHub's numeric ids for the owner and the repository. With "immutable IDs in OIDC
# subject claims" enabled -- which newer repositories have on -- the sub is
#
#   repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:refs/heads/main
#
# rather than repo:<owner>/<repo>:ref:refs/heads/main, and `StringEquals` against the
# short form fails with a bare "Not authorized to perform sts:AssumeRoleWithWebIdentity"
# that names nothing. It is the better format: a rename cannot silently hand the trust
# to whoever claims the old name.
#
# Read the ids from a failed attempt rather than guessing -- CloudTrail records the sub
# verbatim as `userIdentity.userName` (see the README). Set both to "" for an account
# where the short form is still in use.
variable "github_owner_id" {
  description = "Numeric GitHub id of the owner; \"\" for the pre-immutable-id sub format"
  type        = string
  default     = "91155437"
}

variable "github_repo_id" {
  description = "Numeric GitHub id of the repository"
  type        = string
  default     = "1341938364"
}

# The sub claim is the WHOLE access control on this role. A wildcard here
# ("repo:owner/*" or a trailing ":*") means any branch, any pull request and any
# fork's PR workflow may assume it -- which is a stranger pushing a branch and
# getting your ECR push rights. Every entry must name one ref or one environment.
#
# Empty means "derive them", which is the normal case; set it to pin something the
# derivation does not cover, such as a second branch or a tag ref.
variable "allowed_subjects" {
  description = "Exact GitHub OIDC sub claims permitted to assume the deploy role"
  type        = list(string)
  default     = []

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
