"""Diagnostic wrappers preserve the exact native eligibility contract."""
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from solbreaker import collision, diagnostics


class DiagnosticFastPathTests(unittest.TestCase):
    def setUp(self):
        diagnostics.close()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(diagnostics.close)
        self.directory = temporary.name

    def test_known_wrappers_keep_native_guards_and_close_restores_originals(self):
        contract = collision._DRIVE_PREFIX_METHODS
        rays = collision._GROUND_RAYS_IMPL
        contacts = collision.CollisionWorld._contacts
        recorder = diagnostics.install(self.directory)
        self.assertIs(diagnostics.install(self.directory), recorder)
        active = collision._DRIVE_PREFIX_METHODS
        self.assertIs(active['_contacts'], collision.CollisionWorld._contacts)
        self.assertIs(active['_contacts'].__wrapped__, contacts)
        self.assertIs(collision._GROUND_RAYS_IMPL, collision.CollisionWorld._static_rays)
        for name, original in contract.items():
            if name != '_contacts':
                self.assertIs(active[name], original)
        diagnostics.close()
        self.assertIs(collision._DRIVE_PREFIX_METHODS, contract)
        self.assertIs(collision._GROUND_RAYS_IMPL, rays)
        self.assertIs(collision.CollisionWorld._contacts, contacts)
        self.assertIs(collision.CollisionWorld._static_rays, rays)

    def test_foreign_replacements_are_not_authorised_by_logging(self):
        for name, contract in (('_contacts', collision._DRIVE_PREFIX_METHODS['_contacts']),
                               ('_static_rays', collision._GROUND_RAYS_IMPL)):
            with self.subTest(method=name):
                def foreign(*args, **kwargs):
                    return []
                with patch.object(collision.CollisionWorld, name, foreign):
                    try:
                        diagnostics.install(self.directory)
                        guarded = (collision._DRIVE_PREFIX_METHODS['_contacts'] if name == '_contacts'
                                   else collision._GROUND_RAYS_IMPL)
                        self.assertIs(guarded, contract)
                        self.assertIsNot(guarded, getattr(collision.CollisionWorld, name))
                    finally:
                        diagnostics.close()


if __name__ == '__main__':
    unittest.main()
