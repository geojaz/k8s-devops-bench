# Defaulted to "kind" rather than left required, unlike the provider-neutral stacks.
# This task is kind-only: the seed dynamically discovers which of the two kind
# worker nodes holds ledger-1's PV and cordons it by name, and relies on kind's
# local-path provisioner binding a PV to whichever node a pod first schedules
# onto. Neither behavior is guaranteed on a shared GKE cluster. The validation
# fails fast and loudly rather than letting a GKE run produce a silently-
# unseeded cluster.
variable "infra_provider" {
  type        = string
  description = "Target provider. This stack supports kind only."
  default     = "kind"

  validation {
    condition     = var.infra_provider == "kind"
    error_message = "prebuilt/ledger-restore is kind-only: the seed depends on kind's local-path provisioner binding a PV to the node a pod first schedules onto, and cannot do that reliably on a shared GKE cluster."
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

# Two workers plus the control plane. The whole defect is one worker cordoned
# after maintenance while a StatefulSet pod's volume is pinned to it; a single
# worker leaves nowhere for the seed to put ledger-0/ledger-2 once the other
# worker is cordoned, and no second node to hold the untainted-but-tolerated
# maintenance taint distractor.
variable "node_count" {
  type        = number
  description = "Number of nodes. 3 gives a control-plane node plus two workers: one holds ledger-1's volume and gets cordoned, the other keeps serving ledger-0/ledger-2."
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
