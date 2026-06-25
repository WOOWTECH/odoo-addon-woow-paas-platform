import hashlib
import re


def generate_ws_hash(slug: str) -> str:
    """Generate 8-char hex hash from workspace slug."""
    return hashlib.md5(slug.encode()).hexdigest()[:8]


def generate_resource_hash(reference_id: str, name: str) -> str:
    """Generate 8-char salted hex hash for a resource."""
    return hashlib.md5((reference_id + name).encode()).hexdigest()[:8]


def make_namespace(slug: str, prefix: str = 'paas-ws-') -> str:
    """Generate K8s namespace name: {prefix}{ws_hash}."""
    return f"{prefix}{generate_ws_hash(slug)}"


def _env_token(env: str = '') -> str:
    """Normalise a deploy-env value into a DNS-safe naming token segment.

    Returned segment is inserted right after the ``paas-`` prefix
    (format ``paas-{env}-{type}-``):

    - ``''`` / ``'prod'`` -> ``''``. Production names stay byte-identical to the
      pre-env-token scheme, so existing records and hard-coded format assertions
      do not drift.
    - any other env (e.g. ``'stg'``) -> ``'{env}-'``.

    Case/whitespace are normalised as a guard against a mis-set value. The
    upstream ``deploy_env`` config is a Selection whitelist, so this is a
    defensive normaliser, not a full sanitiser.
    """
    normalised = (env or '').lower().strip()
    if normalised in ('', 'prod'):
        return ''
    # slugify to a DNS-safe label segment. deploy_env is a Selection whitelist
    # upstream, but a raw ir.config_parameter write could be anything — never let
    # it produce an illegal DNS/k8s label (spaces / underscores / non-ascii / a
    # trailing dash) that would break the CF tunnel name or DNS comment.
    slug = re.sub(r'[^a-z0-9]+', '-', normalised).strip('-')
    return f"{slug}-" if slug else ''


def make_service_subdomain(slug: str, reference_id: str, name: str, env: str = '') -> str:
    """Generate cloud service subdomain: paas-{env}-cs-{ws_hash}-{svc_hash}."""
    return f"paas-{_env_token(env)}cs-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"


def make_smarthome_subdomain(slug: str, reference_id: str, name: str, env: str = '') -> str:
    """Generate smart home subdomain: paas-{env}-sm-{ws_hash}-{sm_hash}."""
    return f"paas-{_env_token(env)}sm-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"


def make_security_access_subdomain(slug: str, reference_id: str, name: str, env: str = '') -> str:
    """Generate security access subdomain: paas-{env}-sa-{ws_hash}-{sa_hash}."""
    return f"paas-{_env_token(env)}sa-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"
