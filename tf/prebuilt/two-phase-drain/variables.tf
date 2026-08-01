# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only: the seed and self-check both name
# {{CLUSTER_NAME}}-worker and {{CLUSTER_NAME}}-worker2 directly, which is only
# a deterministic mapping on kind. The validation fails fast and loudly rather
# than letting a GKE run produce a silently-unseeded cluster.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/two-phase-drain is kind-only: the seed names {{CLUSTER_NAME}}-worker and {{CLUSTER_NAME}}-worker2 directly, which is not a reliable mapping on a shared GKE cluster."
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

# Two workers plus the control plane. The whole task is draining one worker at
# a time while the other absorbs its workload; a single worker leaves nowhere
# for evicted pods to land during either phase.
variable "node_count" {
  type        = number
  description = "Number of nodes. 3 gives a control-plane node plus two workers, both patch-pending: each must be drained in turn while the other keeps serving."
  default     = 3
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
