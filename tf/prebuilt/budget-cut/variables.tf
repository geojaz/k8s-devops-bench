# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only: the seed installs metrics-server patched with
# --kubelet-insecure-tls, a fix for kind's self-signed kubelet serving certs that is
# wrong (and unnecessary) against a real GKE kubelet. The validation fails fast and
# loudly rather than letting a GKE run apply a patch that does not belong there.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/budget-cut is kind-only: the seed patches metrics-server with --kubelet-insecure-tls, which is a kind-specific workaround and wrong on GKE."
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

# Single node on purpose. The whole seed is metrics-server plus four tiny workloads in
# one namespace, and kind removes the control-plane NoSchedule taint on a single-node
# cluster, so they all schedule. More nodes only slows cluster creation down.
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
