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

# KinD cluster. The shop namespace, the immutable checkout-config ConfigMap, the
# checkout Deployment (shadowed env), and the sibling receipts Deployment are all
# applied by setup.sh.
module "cluster" {
  source          = "../../modules/cluster"
  infra_provider  = var.infra_provider
  project_id      = var.project_id
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  kubeconfig_path = var.kubeconfig_path
}

# Outside-the-cluster setup. All three objects can be applied together (see
# scripts/setup.sh); the only ordering requirement -- the ConfigMap existing before
# the Deployments that reference it -- is satisfied by document order within a
# single kubectl apply, so there is exactly one manifest file here.
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
