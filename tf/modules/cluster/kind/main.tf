# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

terraform {
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
  }
}

locals {
  # Stock Calico manifests assume 192.168.0.0/16 for their IPAM pool. The
  # manifest itself leaves CALICO_IPV4POOL_CIDR commented out and instead
  # auto-detects the pool from each node's spec.podCIDR, so what actually
  # has to match is the pod subnet kubeadm hands out to nodes. When a caller
  # disables the default CNI and does not set an explicit pod_subnet, default
  # to Calico's own assumption rather than leaving the two silently mismatched.
  effective_pod_subnet = var.pod_subnet != "" ? var.pod_subnet : (var.disable_default_cni ? "192.168.0.0/16" : "")
}

resource "kind_cluster" "default" {
  name            = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = pathexpand(var.kubeconfig_path)

  # The provider's own wait_for_ready polls kind's node-Ready condition
  # (sigs.k8s.io/kind's waitforready action), which never becomes true
  # without a CNI. When the default CNI is disabled, skip the provider's
  # wait and do our own after the CNI is installed, below.
  wait_for_ready = var.disable_default_cni ? false : true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }

    dynamic "node" {
      for_each = range(max(0, var.node_count - 1))
      content {
        role = "worker"
      }
    }

    # Only emit a networking stanza when a caller opts into a non-default
    # value. With both inputs at their defaults, no networking block is
    # rendered at all, which keeps the config identical to before this
    # stanza existed.
    dynamic "networking" {
      for_each = (var.disable_default_cni || var.pod_subnet != "") ? [1] : []
      content {
        disable_default_cni = var.disable_default_cni ? true : null
        pod_subnet          = local.effective_pod_subnet != "" ? local.effective_pod_subnet : null
      }
    }
  }
}

# Installs a real CNI so NetworkPolicy is actually enforced, but only when a
# caller opted into disable_default_cni. This has to live here, not in
# setup.sh, because the cluster is never considered ready without a CNI:
# setup.sh only runs after Terraform's own wait, and that wait never
# resolves for a CNI-less cluster. The module owns the full sequence itself:
# create the cluster (without waiting), install the CNI, then wait for
# rollout and node readiness before returning control to the caller.
resource "null_resource" "install_cni" {
  count      = var.disable_default_cni ? 1 : 0
  depends_on = [kind_cluster.default]

  triggers = {
    cluster = kind_cluster.default.name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG='${pathexpand(var.kubeconfig_path)}'
      kubectl apply -f '${var.cni_manifest_url}'
      kubectl -n kube-system rollout status daemonset/calico-node --timeout='${var.cni_wait_timeout}'
      kubectl wait --for=condition=Ready nodes --all --timeout='${var.cni_wait_timeout}'
    EOT
  }
}

# Duplicates the KinD context to match a GKE-like name pattern.
# This is required for third-party gke-mcp tools to resolve the context when
# running tasks against local KinD clusters, as the MCP client expects the
# context to conform to the "gke_{project}_{location}_{cluster}" format.
resource "null_resource" "duplicate_context" {
  depends_on = [kind_cluster.default]

  triggers = {
    kubeconfig   = pathexpand(var.kubeconfig_path)
    kind_cluster = "kind-${var.cluster_name}"
    kind_user    = "kind-${var.cluster_name}"
    gke_context  = "gke_${var.project_id}_${var.location}_${var.cluster_name}"
  }

  provisioner "local-exec" {
    command = "kubectl --kubeconfig='${self.triggers.kubeconfig}' config set-context '${self.triggers.gke_context}' --cluster='${self.triggers.kind_cluster}' --user='${self.triggers.kind_user}'"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "kubectl --kubeconfig='${self.triggers.kubeconfig}' config delete-context '${self.triggers.gke_context}' || true"
  }
}
