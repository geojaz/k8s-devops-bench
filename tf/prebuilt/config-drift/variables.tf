# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only for consistency with the rest of the prebuilt set: the seed
# has to prove an immutable ConfigMap genuinely rejects a live update (see
# scripts/setup.sh), and kind is the only target this stack has been exercised
# against. The validation fails fast and loudly rather than letting a GKE run
# produce a silently-unseeded cluster.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/config-drift is kind-only: the seed has only been exercised against kind."
  }
}

variable "cluster_name" {
  type        = string
  description = "Name of the cluster to provision"
}

variable "location" {
  type        = string
  description = "Region/zone (GCP) or 'local' (KinD)"
  default     = "local"
}

# Single node on purpose. The whole seed is two tiny nginx Deployments, and kind
# removes the control-plane NoSchedule taint on a single-node cluster, so they both
# schedule. More nodes only slows cluster creation down.
variable "node_count" {
  type        = number
  description = "Number of nodes. 1 gives a single control-plane node that also schedules workloads."
  default     = 1
}

variable "machine_type" {
  type        = string
  description = "VM instance type (GCP only, unused here)"
  default     = ""
}

variable "project_id" {
  type        = string
  description = "GCP Project ID (unused here)"
  default     = ""
}

variable "kubeconfig_path" {
  type        = string
  description = "Target path to write kubeconfig"
  default     = "~/.kube/config"
}

variable "wait_timeout" {
  type        = string
  description = "Seconds each bounded poll in setup.sh will wait before declaring SEED FAIL."
  default     = "180"
}

# Declared but unused, matching the other prebuilt stacks: the provider resolver
# forwards namespace= to every stack, and an undeclared var is silently dropped
# with a warning that reads like a real problem.
variable "namespace" {
  type    = string
  default = "default"
}
