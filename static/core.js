// core.js - the wallet's cryptography, shared by /wallet and /market. Keys live in this browser only.
import * as secp from '/static/vendor/secp256k1.js';
export const COIN = 1e8, enc = new TextEncoder(), dec = new TextDecoder();
export const hex = b => [...b].map(x => x.toString(16).padStart(2, '0')).join(''), unhex = h => new Uint8Array(h.match(/../g).map(x => parseInt(x, 16)));
export const b64 = b => btoa(String.fromCharCode(...b)), unb64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
export const sha256 = async b => new Uint8Array(await crypto.subtle.digest('SHA-256', b)), sha256d = async b => sha256(await sha256(b));
const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
export function b58encode(bytes) { let n = 0n; for (const b of bytes) n = n * 256n + BigInt(b); let s = ''; while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n } for (const b of bytes) { if (b !== 0) break; s = '1' + s } return s }
export async function address(pub) { const h = (await sha256(pub)).slice(0, 20); const data = new Uint8Array(21); data[0] = 0x1e; data.set(h, 1); const chk = (await sha256d(data)).slice(0, 4); const full = new Uint8Array(25); full.set(data); full.set(chk, 21); return b58encode(full) }
export async function derive(password, saltHex) { const km = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, ['deriveBits']);
  const bits = new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: unhex(saltHex), iterations: 310000 }, km, 512));
  const encKey = await crypto.subtle.importKey('raw', bits.slice(0, 32), 'AES-GCM', false, ['encrypt', 'decrypt']); return { encKey, auth: hex(await sha256(bits.slice(32))) } }
export async function lock(encKey, privHex) { const iv = crypto.getRandomValues(new Uint8Array(12)); const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, encKey, enc.encode(JSON.stringify({ k: privHex })))); const out = new Uint8Array(12 + ct.length); out.set(iv); out.set(ct, 12); return b64(out) }
export async function unlock(encKey, blob) { const b = unb64(blob); const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: b.slice(0, 12) }, encKey, b.slice(12)); return JSON.parse(dec.decode(pt)).k }
export async function api(path, body) { const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || r.statusText); return d }
export const fmt = n => (n / COIN).toLocaleString(undefined, { maximumFractionDigits: 8 });
export const keypair = privHex => ({ priv: privHex, pub: hex(secp.getPublicKey(unhex(privHex), true)) });
export const randomPriv = () => hex(secp.utils.randomPrivateKey());
// a compact secp256k1 signature over sha256d(bytes)
export async function signBytes(privHex, bytes) { return (await secp.signAsync(await sha256d(bytes), unhex(privHex))).toCompactHex() }
// canonical JSON the server checks: sorted keys, no spaces (Python: json.dumps(sort_keys=True, separators=(",", ":")))
const sortDeep = v => Array.isArray(v) ? v.map(sortDeep) : (v && typeof v === 'object') ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sortDeep(v[k])])) : v;
export const canonical = o => JSON.stringify(sortDeep(o));   // keys sorted at every level, like Python's sort_keys=True
export async function signed(me, payload) { payload = { ...payload, ts: Math.floor(Date.now() / 1000) }; return { payload, pubkey: me.pub, signature: await signBytes(me.priv, enc.encode(canonical(payload))) } }
// session: the unlocked wallet, this tab only, gone when the tab closes
export const session = { get: () => { try { return JSON.parse(sessionStorage.getItem('donut.wallet') || 'null') } catch { return null } }, set: w => { try { w ? sessionStorage.setItem('donut.wallet', JSON.stringify(w)) : sessionStorage.removeItem('donut.wallet') } catch {} } };
