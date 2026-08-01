terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

provider "kind" {}

# KinD cluster. The ledger namespace, its StatefulSets, quota, and the cordoned
# node are all set up by setup.sh, which needs both workers to exist before it
# can identify which one holds ledger-1's volume.
module "cluster" {
  source          = "../../modules/cluster"
  infra_provider  = var.infra_provider
  project_id      = var.project_id
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  kubeconfig_path = var.kubeconfig_path
  # Pinned newer than the module default (kindest/node:v1.29.2). On 1.29.2 the
  # scheduler's FailedScheduling event for this exact topology never reports a
  # "volume node affinity conflict" reason, it just says nodes are unschedulable.
  # This task's whole diagnosis depends on that per-plugin reason existing, so a
  # node image where the scheduler actually reports it is required.
  node_image = "kindest/node:v1.32.2"
}

# Outside-the-cluster setup. Runs during `tofu apply`, before the agent starts.
# THE ORDER IS THE SEED here too: setup.sh must let all three ledger pods
# schedule and bind their PVs first, discover which node ledger-1 landed on,
# and only then cordon that node and delete the pod -- see scripts/setup.sh.
resource "null_resource" "setup" {
  depends_on = [module.cluster]

  triggers = {
    cluster = module.cluster.cluster_name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"
    environment = {
      INFRA_PROVIDER = var.infra_provider
      PROJECT_ID     = var.project_id
      CLUSTER_NAME   = module.cluster.cluster_name
      LOCATION       = var.location
      KUBECONFIG     = pathexpand(var.kubeconfig_path)
      MANIFESTS_DIR  = "${path.module}/manifests"
      WAIT_TIMEOUT   = var.wait_timeout
    }
  }
}
