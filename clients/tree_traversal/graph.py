#   3개 Graph 설명
#
#   CALL_GRAPH (호출 의존성 그래프)
#   trace_span.csv에서 추출한 서비스 간 호출 방향 그래프. parent span의 cmdb_id → child span의 cmdb_id (RPC), 또는 cmdb_id → dsName
#   (JDBC) 관계를 집계한 것. 장애가 이 호출 경로를 따라 전파되므로, 특정 컴포넌트 장애 시 영향 범위를 추론하는 데 사용.
#
#   DEPLOYMENT_GRAPH (배포 토폴로지 그래프)
#   어떤 pod가 어떤 물리/가상 node 위에 배포되어 있는지를 나타내는 그래프. metric_container.csv의 cmdb_id 형식(예:
#   node-6.adservice-0)에서 추출. node-level 장애 발생 시 해당 node 위의 모든 pod가 동시에 영향받으므로, 동시 장애 패턴의 원인을
#   node로 좁히는 데 사용.
#
#   SHARED_RESOURCE_GRAPH (공유 자원 그래프)
#   여러 서비스가 동일한 백엔드 자원(DB, 서비스 등)을 공유하는 관계. CALL_GRAPH에서 피호출자 관점으로 뒤집어, 하나의 자원을
#   호출하는 caller들을 묶은 것. 공유 자원에 장애가 발생하면 의존하는 모든 서비스가 동시에 이상을 보이므로, 동시다발 장애의 root
#   cause를 공유 자원으로 추론하는 데 사용.

from __future__ import annotations

# ---------------------------------------------------------------------------
# Telecom
# ---------------------------------------------------------------------------

_TELECOM_CALL_GRAPH = {
    "os_021": ["docker_003", "docker_004"],
    "os_022": ["docker_001", "docker_002"],
    "docker_001": ["docker_007", "docker_008", "db_007", "db_009"],
    "docker_002": ["docker_007", "docker_008", "db_007", "db_009"],
    "docker_003": ["docker_005", "docker_006", "db_007", "db_009"],
    "docker_004": ["docker_005", "docker_006", "db_007", "db_009"],
    "docker_005": ["db_003"],
    "docker_006": ["db_003"],
    "docker_007": ["db_003"],
    "docker_008": ["db_003"],
}

# Host -> components on that host (from deployment topology).
# OSB (osb_001), CSF (csf_001–005), oracle_11G (db_001–013) have no host in the spec.
_TELECOM_DEPLOYMENT_GRAPH = {
    # redis: 12 instances on os_003, os_004, os_005 (4 per host)
    # "os_003": ["redis_001", "redis_002", "redis_003", "redis_004"],
    # "os_004": ["redis_005", "redis_006", "redis_007", "redis_008"],
    # "os_005": ["redis_009", "redis_010", "redis_011", "redis_012"],
    # container_001: docker_001–004 on os_017–020
    # container_002: docker_005–008 on os_017–020
    "os_017": ["docker_001", "docker_005"],
    "os_018": ["docker_002", "docker_006"],
    "os_019": ["docker_003", "docker_007"],
    "os_020": ["docker_004", "docker_008"],
}

_TELECOM_SHARED_RESOURCE_GRAPH = {
    "db_003": ["docker_005", "docker_006", "docker_007", "docker_008"],
    "db_007": ["docker_001", "docker_002", "docker_003", "docker_004"],
    "db_009": ["docker_001", "docker_002", "docker_003", "docker_004"],
}

# ---------------------------------------------------------------------------
# Bank
# ---------------------------------------------------------------------------

_BANK_CALL_GRAPH = {
    "IG01": ["Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04"],
    "IG02": ["Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04"],
    "Tomcat01": ["MG01", "MG02"],
    "Tomcat02": ["MG01", "MG02"],
    "Tomcat03": ["MG01", "MG02"],
    "Tomcat04": ["MG01", "MG02"],
    "MG01": ["dockerA1", "dockerA2", "dockerB1", "dockerB2"],
    "MG02": ["dockerA1", "dockerA2", "dockerB1", "dockerB2"],
    "dockerA1": ["MG01", "MG02"],
    "dockerA2": ["MG01", "MG02"],
    "dockerB1": ["MG01", "MG02"],
    "dockerB2": ["MG01", "MG02"],
}

_BANK_DEPLOYMENT_GRAPH: dict[str, list[str]] = {}

_BANK_SHARED_RESOURCE_GRAPH = {
    "MG01": ["Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04", "dockerA1", "dockerA2", "dockerB1", "dockerB2"],
    "MG02": ["Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04", "dockerA1", "dockerA2", "dockerB1", "dockerB2"],
}

# ---------------------------------------------------------------------------
# Market Cloudbed-1
# ---------------------------------------------------------------------------

_MARKET_CALL_GRAPH = {
    "frontend": ["adservice", "cartservice", "checkoutservice", "currencyservice", "productcatalogservice", "recommendationservice", "shippingservice"],
    "frontend2": ["adservice2", "cartservice2", "checkoutservice2", "currencyservice2", "productcatalogservice2", "recommendationservice2", "shippingservice2"],
    "checkoutservice": ["cartservice", "currencyservice", "emailservice", "paymentservice", "productcatalogservice", "shippingservice"],
    "checkoutservice2": ["cartservice2", "currencyservice2", "emailservice2", "paymentservice2", "productcatalogservice2", "shippingservice2"],
    "recommendationservice": ["productcatalogservice"],
    "recommendationservice2": ["productcatalogservice"],
}

_MARKET_DEPLOYMENT_GRAPH = {
    "node-5": [
        "adservice-2", "cartservice2-0", "checkoutservice-2",
        "frontend-1", "frontend-2", "shippingservice-2",
    ],
    "node-6": [
        "adservice-0", "adservice-1", "adservice2-0",
        "cartservice-0", "cartservice-1", "cartservice-2",
        "checkoutservice-0", "checkoutservice-1", "checkoutservice2-0",
        "currencyservice-0", "currencyservice-1", "currencyservice-2", "currencyservice2-0",
        "emailservice-0", "emailservice-1", "emailservice-2", "emailservice2-0",
        "frontend-0", "frontend2-0",
        "paymentservice-0", "paymentservice-1", "paymentservice-2", "paymentservice2-0",
        "productcatalogservice-0", "productcatalogservice-1", "productcatalogservice-2", "productcatalogservice2-0",
        "recommendationservice-0", "recommendationservice-1", "recommendationservice-2", "recommendationservice2-0",
        "redis-cart-0", "redis-cart2-0",
        "shippingservice-0", "shippingservice-1", "shippingservice2-0",
    ],
}

_MARKET_SHARED_RESOURCE_GRAPH = {
    "productcatalogservice": ["checkoutservice", "frontend", "recommendationservice", "recommendationservice2"],
    "cartservice": ["checkoutservice", "frontend"],
    "currencyservice": ["checkoutservice", "frontend"],
    "shippingservice": ["checkoutservice", "frontend"],
    "productcatalogservice2": ["checkoutservice2", "frontend2"],
    "cartservice2": ["checkoutservice2", "frontend2"],
    "currencyservice2": ["checkoutservice2", "frontend2"],
    "shippingservice2": ["checkoutservice2", "frontend2"],
}


def get_graphs(dataset_key: str) -> dict[str, dict]:
    """Return call_graph, deployment_graph, shared_resource_graph for the dataset.

    dataset_key: e.g. openrca_market_cb1, openrca_telecom, openrca_bank
    Returns: {"call_graph": {...}, "deployment_graph": {...}, "shared_resource_graph": {...}}
    """
    ds = (dataset_key or "").replace("openrca_", "")

    if ds.startswith("market"):
        return {
            "call_graph": dict(_MARKET_CALL_GRAPH),
            "deployment_graph": dict(_MARKET_DEPLOYMENT_GRAPH),
            "shared_resource_graph": dict(_MARKET_SHARED_RESOURCE_GRAPH),
        }
    if ds.startswith("telecom"):
        return {
            "call_graph": dict(_TELECOM_CALL_GRAPH),
            "deployment_graph": dict(_TELECOM_DEPLOYMENT_GRAPH),
            "shared_resource_graph": dict(_TELECOM_SHARED_RESOURCE_GRAPH),
        }
    if ds.startswith("bank"):
        return {
            "call_graph": dict(_BANK_CALL_GRAPH),
            "deployment_graph": dict(_BANK_DEPLOYMENT_GRAPH),
            "shared_resource_graph": dict(_BANK_SHARED_RESOURCE_GRAPH),
        }

    return {
        "call_graph": {},
        "deployment_graph": {},
        "shared_resource_graph": {},
    }


def _service_from_component(component: str) -> str:
    """Derive service name from pod/cmdb_id (e.g. checkoutservice-0 -> checkoutservice, node-5.frontend-1 -> frontend)."""
    if not component:
        return component
    # node-5.frontend-1 -> frontend-1, then frontend-1 -> frontend (strip trailing -N)
    if "." in component:
        component = component.split(".", 1)[-1]
    # frontend-1 -> frontend, checkoutservice2-0 -> checkoutservice2
    for i, c in enumerate(component):
        if c == "-" and i > 0:
            try:
                rest = component[i + 1:]
                if rest.isdigit():
                    return component[:i]
            except Exception:
                pass
    return component


def get_related_components_for_expand(component: str, dataset_key: str) -> list[str]:
    """Return list of related component names for expand stage using topology graphs.

    Uses CALL_GRAPH (callers + callees), DEPLOYMENT_GRAPH (same node / sibling pods),
    and SHARED_RESOURCE_GRAPH (shared resource and its callers).
    """
    graphs = get_graphs(dataset_key)
    call_g = graphs["call_graph"]
    deploy_g = graphs["deployment_graph"]
    shared_g = graphs["shared_resource_graph"]

    out: set[str] = set()
    comp = (component or "").strip()
    if not comp:
        return []

    # Try exact match and service-level match (for Market pod names like checkoutservice-0)
    candidates = [comp]
    service = _service_from_component(comp)
    if service != comp:
        candidates.append(service)

    for c in candidates:
        # Call graph: callees (downstream) and callers (upstream)
        if c in call_g:
            out.update(call_g[c])
        for caller, callees in call_g.items():
            if c in callees:
                out.add(caller)
                out.update(callees)

        # Deployment: same node, or siblings on same node
        if c in deploy_g:
            out.update(deploy_g[c])
        for node, pods in deploy_g.items():
            if c in pods:
                out.add(node)
                out.update(pods)

        # Shared resource: resource and its callers
        if c in shared_g:
            out.update(shared_g[c])
        for resource, callers in shared_g.items():
            if c in callers:
                out.add(resource)
                out.update(callers)

    out.discard(comp)
    return list(out)


def get_related_components_with_relations(
    component: str,
    dataset_key: str,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return related components for expand **with relation types**.

    Relation types (non-exhaustive):
      - "call_downstream": component -> callee in CALL_GRAPH
      - "call_upstream": caller -> component in CALL_GRAPH
      - "call_sibling": other callees of the same caller in CALL_GRAPH
      - "deploy_node": shared node in DEPLOYMENT_GRAPH
      - "deploy_sibling": other pods on same node in DEPLOYMENT_GRAPH
      - "shared_resource": shared backend resource (e.g. DB) for this component
      - "shared_sibling": other callers sharing the same backend resource
    """
    graphs = get_graphs(dataset_key)
    call_g = graphs["call_graph"]
    deploy_g = graphs["deployment_graph"]
    shared_g = graphs["shared_resource_graph"]

    comp = (component or "").strip()
    if not comp:
        return [], {}

    relations: dict[str, set[str]] = {}

    def _add(target: str, rel: str):
        target = (target or "").strip()
        if not target or target == comp:
            return
        relations.setdefault(target, set()).add(rel)

    candidates = [comp]
    service = _service_from_component(comp)
    if service != comp:
        candidates.append(service)

    for c in candidates:
        # CALL_GRAPH: downstream callees
        if c in call_g:
            for cal in call_g[c]:
                _add(cal, "call_downstream")

        # CALL_GRAPH: upstream callers and siblings
        for caller, callees in call_g.items():
            if c in callees:
                _add(caller, "call_upstream")
                for cal in callees:
                    if cal != c:
                        _add(cal, "call_sibling")

        # DEPLOYMENT_GRAPH: same node / siblings
        if c in deploy_g:
            # When c is a node, its pods are siblings.
            for pod in deploy_g[c]:
                _add(pod, "deploy_sibling")
        for node, pods in deploy_g.items():
            if c in pods:
                _add(node, "deploy_node")
                for pod in pods:
                    if pod != c:
                        _add(pod, "deploy_sibling")

        # SHARED_RESOURCE_GRAPH: shared backend resource and its callers
        if c in shared_g:
            # c is a shared resource; its callers are siblings that depend on it.
            for caller in shared_g[c]:
                _add(caller, "shared_sibling")
        for resource, callers in shared_g.items():
            if c in callers:
                _add(resource, "shared_resource")
                for caller in callers:
                    if caller != c:
                        _add(caller, "shared_sibling")

    related = sorted(relations.keys())
    rel_map: dict[str, list[str]] = {
        tgt: sorted(tags) for tgt, tags in relations.items()
    }
    return related, rel_map
