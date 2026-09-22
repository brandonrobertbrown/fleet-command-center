# Security Model

## Passkeys (WebAuthn)
Your devices' face unlock (Face ID, Windows Hello, Android biometrics) are
platform authenticators. On unlock:

1. Console sends a challenge
2. Your device's secure enclave signs it with the key minted at enrollment
3. Console verifies the signature against the enrolled public key

**Your face never leaves your device. Nothing biometric is stored server-side.**
Origin-binding makes phishing the credential pointless.

## Layers
- TLS-only (plaintext HTTP dead by default)
- All state-changing API routes require a bearer session
- 8h sessions, recovery PIN (sha256-hashed, attempt-limited)
- Passkey credentials in a chmod-600 JSON

## Honest limits
LAN-facing tool. Do not port-forward it. If you need remote access, use your
VPN (WireGuard/Tailscale) first, then the console.
