# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only: the seed's self-check waits on Kyverno's background scan to
# populate PolicyReports within a fixed budget, which is only predictable on a cluster
# nobody else is scheduling onto. The validation fails fast and loudly rather than
# letting a GKE run produce a silently-unseeded cluster.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/policy-enforcement-window is kind-only: the seed's self-check budgets a fixed wait for Kyverno's background scan and cannot do that reliably on a shared GKE cluster."
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

# Single node on purpose. The whole seed is Kyverno plus eight tiny pods across three
# namespaces, and kind removes the control-plane NoSchedule taint on a single-node
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
