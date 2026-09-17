"""Hashing, keys, addresses and the compact difficulty encoding.

Like Dogecoin (and Litecoin before it) the proof of work is Scrypt with N=1024, r=1, p=1 over the
80-byte block header, salted with the header itself. The block *id* is double-SHA256 of the same
header - two hashes of one header, the Litecoin convention. Keys are secp256k1 ECDSA; addresses are
Base58Check with Dogecoin's version byte 0x1E, which is why they start with a D.
"""
import hashlib
from ecdsa import SigningKey, VerifyingKey, SECP256k1, BadSignatureError
from ecdsa.util import sigencode_der, sigdecode_der, sigdecode_string

ADDRESS_VERSION = 0x1E
_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def sha256d(b: bytes) -> bytes:
    return sha256(sha256(b))


def scrypt_hash(header: bytes) -> bytes:
    """The proof-of-work hash. Scrypt is memory-hard on purpose: the 128 KB scratchpad is what kept
    early GPUs and ASICs at bay for a while, and it is why a CPU can still play here at all."""
    return hashlib.scrypt(header, salt=header, n=1024, r=1, p=1, dklen=32)


def hash160(b: bytes) -> bytes:
    # Bitcoin uses RIPEMD160(SHA256(x)); RIPEMD160 is gone from many OpenSSL builds, so this is
    # SHA256 truncated to 20 bytes. Same length, same purpose, no legacy provider needed.
    return sha256(b)[:20]


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big"); out = bytearray()
    while n:
        n, r = divmod(n, 58); out.append(_B58[r])
    pad = len(b) - len(b.lstrip(b"\0"))
    return (_B58[0:1] * pad + out[::-1]).decode()


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s.encode():
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


def b58check_encode(version: int, payload: bytes) -> str:
    data = bytes([version]) + payload
    return b58encode(data + sha256d(data)[:4])


def b58check_decode(s: str) -> tuple[int, bytes]:
    raw = b58decode(s)
    data, check = raw[:-4], raw[-4:]
    if sha256d(data)[:4] != check: raise ValueError("bad address checksum")
    return data[0], data[1:]


def pubkey_to_address(pub: bytes) -> str:
    return b58check_encode(ADDRESS_VERSION, hash160(pub))


def is_valid_address(addr: str) -> bool:
    try:
        v, payload = b58check_decode(addr); return v == ADDRESS_VERSION and len(payload) == 20
    except Exception:
        return False


def new_keypair() -> tuple[str, str, str]:
    """(private key hex, compressed public key hex, address)"""
    sk = SigningKey.generate(curve=SECP256k1)
    pub = sk.get_verifying_key().to_string("compressed")
    return sk.to_string().hex(), pub.hex(), pubkey_to_address(pub)


def sign(priv_hex: str, digest: bytes) -> str:
    sk = SigningKey.from_string(bytes.fromhex(priv_hex), curve=SECP256k1)
    return sk.sign_digest_deterministic(digest, hashfunc=hashlib.sha256, sigencode=sigencode_der).hex()


def verify(pub_hex: str, digest: bytes, sig_hex: str) -> bool:
    """DER (the Python wallet) or 64-byte compact r||s (the browser wallet, noble-secp256k1)."""
    try:
        vk = VerifyingKey.from_string(bytes.fromhex(pub_hex), curve=SECP256k1)
        sig = bytes.fromhex(sig_hex)
        dec = sigdecode_string if len(sig) == 64 else sigdecode_der
        return vk.verify_digest(sig, digest, sigdecode=dec)
    except (BadSignatureError, ValueError, TypeError):
        return False


# --- difficulty: Bitcoin's "compact" target encoding (the 'bits' field) ---
MAX_TARGET = 0x00000FFFFF000000000000000000000000000000000000000000000000000000  # easy: ~2^20 hashes


def target_to_bits(target: int) -> int:
    size = (target.bit_length() + 7) // 8
    if size <= 3:
        mant = target << (8 * (3 - size))
    else:
        mant = target >> (8 * (size - 3))
    if mant & 0x800000:
        mant >>= 8; size += 1
    return (size << 24) | mant


def bits_to_target(bits: int) -> int:
    size = bits >> 24; mant = bits & 0x7FFFFF
    return mant << (8 * (size - 3)) if size > 3 else mant >> (8 * (3 - size))


def work_from_bits(bits: int) -> int:
    """Expected hashes to find a block at this target - what 'chain work' adds up."""
    return (1 << 256) // (bits_to_target(bits) + 1)


def difficulty_from_bits(bits: int) -> float:
    return MAX_TARGET / bits_to_target(bits)
