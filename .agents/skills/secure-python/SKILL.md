---
name: secure-python
description: Implement secure Python CLI and domain code in this slot machine. Use for authentication, role checks, passwords, user input, randomness, and game-engine changes.
---

# Secure Python development

1. Read `docs/PLAN.md`, `ProvidedResources/Requirement.md`, and the selected issue.
   Preserve the team's Python decision and separation of player, technician, and
   commissioner powers.
2. Keep authorization in `auth.require_role`; require it on privileged entry
   points. Deny anonymous callers, stale role claims, and pending password changes.
   Treat the frozen Session as an in-process object, never a serialized credential.
3. Use the pinned Argon2id dependency for passwords. Preserve at least m=19456 KiB,
   t=2, p=1, per-password salts, generic login errors, dummy verification for unknown
   accounts, and account lockout. Never log passwords or hashes.
4. Draw spin entropy only in `rng.py` through `secrets`; allow credential entropy
   in `auth.py` and payment-token entropy in `payment.py`. Never import `random`.
5. Validate input at boundaries. Use JSON for data, never executable or pickle
   payloads. Keep the game engine pure and deterministic.
6. Catch expected authentication/access errors and terminal cancellation at the
   CLI boundary. Use hidden password prompts; keep tracebacks and internal errors
   out of ordinary user flows.
7. Add tests that demonstrate denied access and failure recovery, then follow
   `.agents/skills/security-verification/SKILL.md`.
