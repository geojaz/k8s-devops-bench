# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only: the seed's self-check asserts exact status.qosClass values,
# which the API server computes from the pod spec and which is only predictable on a
# cluster nobody else is scheduling onto. The validation fails fast and loudly rather
# than letting a GKE run produce a silently-unseeded cluster.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/noisy-neighbor is kind-only: the seed's self-check asserts exact QoS class on seeded pods and cannot do that reliably on a shared GKE cluster."
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

# Three nodes on purpose, not one. The task models a FIXED TWO-NODE WORKER POOL WITH
# NO HEADROOM: kind removes the control-plane NoSchedule taint only on a single-node
# cluster, so at node_count=3 the control-plane stays unschedulable and the two worker
# nodes are the entire capacity the seeded workloads compete for, matching the
# prompt's "fixed two-node pool" framing.
variable "node_count" {
  type        = number
  description = "Number of nodes. 3 gives an unschedulable control-plane plus a fixed two-node worker pool."
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
