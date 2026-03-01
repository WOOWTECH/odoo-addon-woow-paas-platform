"""Tests for naming utility module."""
import unittest

from ..services.naming import (
    generate_ws_hash,
    generate_resource_hash,
    make_namespace,
    make_service_subdomain,
    make_smarthome_subdomain,
)


class TestNaming(unittest.TestCase):
    """Test cases for naming utility functions."""

    def test_generate_ws_hash_deterministic(self):
        """Test that the same slug always produces the same hash."""
        h1 = generate_ws_hash('my-workspace')
        h2 = generate_ws_hash('my-workspace')
        self.assertEqual(h1, h2)

    def test_generate_ws_hash_length(self):
        """Test that ws_hash is exactly 8 hex chars."""
        h = generate_ws_hash('test-slug')
        self.assertEqual(len(h), 8)
        # Should be valid hex
        int(h, 16)

    def test_generate_ws_hash_different_slugs(self):
        """Test that different slugs produce different hashes."""
        h1 = generate_ws_hash('workspace-a')
        h2 = generate_ws_hash('workspace-b')
        self.assertNotEqual(h1, h2)

    def test_generate_resource_hash_deterministic(self):
        """Test that same inputs produce same hash."""
        h1 = generate_resource_hash('ref-123', 'my-service')
        h2 = generate_resource_hash('ref-123', 'my-service')
        self.assertEqual(h1, h2)

    def test_generate_resource_hash_length(self):
        """Test that resource hash is exactly 8 hex chars."""
        h = generate_resource_hash('ref-abc', 'svc-name')
        self.assertEqual(len(h), 8)
        int(h, 16)

    def test_generate_resource_hash_salted(self):
        """Test that different reference_ids produce different hashes."""
        h1 = generate_resource_hash('ref-1', 'same-name')
        h2 = generate_resource_hash('ref-2', 'same-name')
        self.assertNotEqual(h1, h2)

    def test_make_namespace_format(self):
        """Test namespace follows paas-ws-{hash} format."""
        ns = make_namespace('my-workspace')
        self.assertTrue(ns.startswith('paas-ws-'))
        self.assertEqual(len(ns), 16)  # 'paas-ws-' (8) + hash (8)

    def test_make_namespace_k8s_valid(self):
        """Test namespace meets K8s naming constraints (max 63 chars, lowercase)."""
        ns = make_namespace('a-very-long-workspace-slug-that-is-quite-long')
        self.assertLessEqual(len(ns), 63)
        self.assertEqual(ns, ns.lower())
        # K8s names must be DNS-compatible
        import re
        self.assertTrue(re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', ns))

    def test_make_service_subdomain_format(self):
        """Test service subdomain follows paas-cs-{ws_hash}-{svc_hash} format."""
        sd = make_service_subdomain('my-ws', 'ref-123', 'my-svc')
        self.assertTrue(sd.startswith('paas-cs-'))
        parts = sd.split('-')
        # paas-cs-{8char}-{8char}
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], 'paas')
        self.assertEqual(parts[1], 'cs')
        self.assertEqual(len(parts[2]), 8)
        self.assertEqual(len(parts[3]), 8)

    def test_make_smarthome_subdomain_format(self):
        """Test smarthome subdomain follows paas-sm-{ws_hash}-{sm_hash} format."""
        sd = make_smarthome_subdomain('my-ws', 'ref-456', 'my-home')
        self.assertTrue(sd.startswith('paas-sm-'))
        parts = sd.split('-')
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], 'paas')
        self.assertEqual(parts[1], 'sm')
        self.assertEqual(len(parts[2]), 8)
        self.assertEqual(len(parts[3]), 8)

    def test_make_smarthome_subdomain_k8s_valid(self):
        """Test smarthome subdomain meets K8s naming constraints."""
        sd = make_smarthome_subdomain('ws-slug', 'ref-id', 'home-name')
        self.assertLessEqual(len(sd), 63)
        self.assertEqual(sd, sd.lower())

    def test_different_resource_types_different_prefixes(self):
        """Test that service and smarthome subdomains have different prefixes."""
        svc = make_service_subdomain('ws', 'ref', 'name')
        sm = make_smarthome_subdomain('ws', 'ref', 'name')
        self.assertTrue(svc.startswith('paas-cs-'))
        self.assertTrue(sm.startswith('paas-sm-'))
        self.assertNotEqual(svc, sm)
