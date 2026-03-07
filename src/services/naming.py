import hashlib


def generate_ws_hash(slug: str) -> str:
    """Generate 8-char hex hash from workspace slug."""
    return hashlib.md5(slug.encode()).hexdigest()[:8]


def generate_resource_hash(reference_id: str, name: str) -> str:
    """Generate 8-char salted hex hash for a resource."""
    return hashlib.md5((reference_id + name).encode()).hexdigest()[:8]


def make_namespace(slug: str) -> str:
    """Generate K8s namespace name: paas-ws-{ws_hash}."""
    return f"paas-ws-{generate_ws_hash(slug)}"


def make_service_subdomain(slug: str, reference_id: str, name: str) -> str:
    """Generate cloud service subdomain: paas-cs-{ws_hash}-{svc_hash}."""
    return f"paas-cs-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"


def make_smarthome_subdomain(slug: str, reference_id: str, name: str) -> str:
    """Generate smart home subdomain: paas-sm-{ws_hash}-{sm_hash}."""
    return f"paas-sm-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"
