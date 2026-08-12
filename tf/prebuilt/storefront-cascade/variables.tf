# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only: the self-check identifies the worker node by its
# deterministic kind name ({{CLUSTER_NAME}}-worker2) and the seed relies on a
# ResourceQuota sized to an exact set of requests, which is only predictable on a
# cluster nobody else is scheduling onto. The validation fails fast and loudly
# rather than letting a GKE run produce a silently-unseeded cluster.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/storefront-cascade is kind-only: the seed names {{CLUSTER_NAME}}-worker2 directly and sizes a ResourceQuota to an exact request total, neither of which is reliable on a shared GKE cluster."
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

# Two workers plus the control plane. The maintenance objective drains
# {{CLUSTER_NAME}}-worker2, which needs a second worker for its evicted pods to
# land on; a single-node (or single-worker) cluster leaves the drain nowhere to go.
variable "node_count" {
  type        = number
  description = "Number of nodes. 3 gives a control-plane node plus two workers: one to drain, one to receive."
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
