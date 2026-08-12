# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# There is nothing kind-specific in this seed's mechanics, but the guard is kept for
# consistency with the other prebuilt stacks: it fails fast and loudly rather than
# running unseeded on a shared GKE cluster nobody validated this against.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/least-privilege-rollback is kind-only: this stack has not been validated against a shared GKE cluster."
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

# Single node on purpose. The whole seed is one namespace with a handful of RBAC
# objects and a single controller pod, and kind removes the control-plane NoSchedule
# taint on a single-node cluster, so it schedules. More nodes only slows cluster
# creation down.
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
