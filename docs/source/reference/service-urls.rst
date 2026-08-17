=========================
Addresses and URLs
=========================

Web and API
===========

.. list-table::
   :header-rows: 1
   :widths: 24 34 42

   * - Service
     - URL
     - Notes
   * - GitLab
     - ``https://gitlab.dev.lo``
     - Git, container registry, package registry
   * - Container registry
     - ``registry.gitlab.dev.lo``
     - ``rke2/images``, ``rke2/charts``, ``rke2/packages``
   * - Keycloak
     - ``https://sso.k8s.dev.lo``
     - Realms ``master`` (admins) and ``dev-lo`` (apps)
   * - OpenBao
     - ``https://bao.k8s.dev.lo``
     - Also ``bao login -method=oidc`` via ``localhost:8250``
   * - Grafana
     - ``https://grafana.k8s.dev.lo``
     - Metrics, logs, traces
   * - Longhorn
     - ``https://longhorn.k8s.dev.lo``
     - Behind oauth2-proxy
   * - Garage S3
     - ``https://s3.k8s.dev.lo``
     - Also ``https://<bucket>.s3.k8s.dev.lo``
   * - FreeIPA
     - ``https://core.dev.lo``
     - Users, groups, DNS, the root CA
   * - Kubernetes API
     - ``https://kube.dev.lo:6443``
     - Virtual address, floated by kube-vip
   * - Artifacts
     - ``http://192.168.2.99/``
     - Apache, internal and tunnel only

Fixed addresses
===============

.. list-table::
   :header-rows: 1
   :widths: 34 24 42

   * - What
     - Address
     - Notes
   * - ``repo01`` external
     - ``192.168.1.20``
     - SSH, SOCKS5 ``1080``, WireGuard ``51820/udp``
   * - ``repo01`` internal
     - ``192.168.2.99``
     - Apache ``80``, apt-cacher-ng ``3142``
   * - ``core01``
     - ``192.168.2.4``
     - DNS, LDAPS ``636``, NTP
   * - Kubernetes API VIP
     - ``192.168.2.20``
     - ``kube.dev.lo``
   * - Control plane
     - ``192.168.2.21-23``
     -
   * - Workers
     - ``192.168.2.31-33``
     -
   * - LoadBalancer pool
     - ``192.168.2.40-52``
     -
   * - Cluster DNS
     - ``192.168.2.40``
     - Authoritative for ``k8s.dev.lo``
   * - Ingress
     - ``192.168.2.41``
     - Every ``*.k8s.dev.lo`` name
   * - Tunnel
     - ``10.66.66.1`` / ``10.66.66.2``
     - Gateway / controller

Files that matter
=================

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Path
     - Holds
   * - ``~/.config/rke2lab/env.sh``
     - Every secret, as shell exports. Mode ``0600``
   * - ``~/.config/rke2lab/sealed-secrets-key.yaml``
     - The sealing key backup
   * - ``~/.config/rke2lab/flux-deploy-token.yml``
     - Flux's read-only repository token
   * - ``~/.config/rke2lab/k8s-ca/``
     - The cluster intermediate CA key pair
   * - ``~/.kube/dev-lo.config``
     - Cluster-admin kubeconfig
   * - ``/data1/gitlab/rke2-deploy-token.yml`` (repo01)
     - The registry token nodes pull with
   * - ``/etc/rancher/rke2/rke2.yaml`` (servers)
     - Node-local kubeconfig
   * - ``/etc/rancher/rke2/registries.yaml`` (all nodes)
     - Mirror rules and the pull credential
