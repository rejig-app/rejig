"""Exercise the hex-byte proof format used by pymobiledevice3's pairing code."""

import binascii
import hashlib
import unittest

from srptools import SRPClientSession, SRPContext, SRPServerSession
from srptools.constants import PRIME_3072, PRIME_3072_GEN


class PairingContractTest(unittest.TestCase):
    def test_server_proof_accepts_hex_bytes_and_rejects_a_changed_proof(self):
        parameters = dict(prime=PRIME_3072, generator=PRIME_3072_GEN, hash_func=hashlib.sha512)
        client_context = SRPContext("Pair-Setup", password="000000", **parameters)
        username, verifier, salt = client_context.get_user_data_triplet()
        client = SRPClientSession(client_context)
        server = SRPServerSession(SRPContext(username, **parameters), verifier)
        client.process(server.public, salt)
        server.process(client.public, salt)
        proof = binascii.unhexlify(server.key_proof_hash)
        self.assertTrue(client.verify_proof(proof.hex().encode()))
        changed = bytes([proof[0] ^ 1]) + proof[1:]
        self.assertFalse(client.verify_proof(changed.hex().encode()))


if __name__ == "__main__":
    unittest.main()
