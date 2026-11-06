# Secure Random Number Generation

Random numbers (or strings) are needed for various security critical functionality, such as generating encryption keys, IVs, session IDs, CSRF tokens or password reset tokens. As such, it is important that these are generated securely, and that it is not possible for an attacker to guess and predict them.

It is generally not possible for computers to generate truly random numbers (without special hardware), so most systems and languages provide two different types of randomness.

## Pseudo-Random Number Generators (PRNG)

Pseudo-Random Number Generators (PRNG) provide low-quality randomness that are much faster, and can be used for non-security related functionality (such as ordering results on a page, or randomizing UI elements). However, they must not be used for anything security critical, as it is often possible for attackers to guess or predict the output.

## Cryptographically Secure Pseudo-Random Number Generators (CSPRNG)

Cryptographically Secure Pseudo-Random Number Generators (CSPRNG) are designed to produce a much higher quality of randomness (more strictly, a greater amount of entropy), making them safe to use for security-sensitive functionality. However, they are slower and more CPU intensive, can end up blocking in some circumstances when large amounts of random data are requested. As such, if large amounts of non-security related randomness are needed, they may not be appropriate.

The table below shows the recommended algorithms for each language, as well as insecure functions that should not be used.

| Language | Unsafe Functions | Cryptographically Secure Functions |
| --- | --- | --- |
| C | `random()`, `rand()` | `getrandom(2)` |
| Java | `Math.random()`, `StrictMath.random()`, `java.util.Random`, `java.util.SplittableRandom`, `java.util.concurrent.ThreadLocalRandom` | `java.security.SecureRandom`, `java.util.UUID.randomUUID()` |
| PHP | `array_rand()`, `lcg_value()`, `mt_rand()`, `rand()`, `uniqid()` | `Random\Engine\Secure` in PHP 8.2, `random_bytes()`, `random_int()` in PHP 7, `openssl_random_pseudo_bytes()` in PHP 5 |
| .NET/C# | `Random()` | `RandomNumberGenerator` |
| Objective-C | Subclasses of `GKRandomSource`, `rand()`, `random()` | `SecRandomCopyBytes`, the `arc4random` family on current Apple platforms |
| Python | `random()` | `secrets()` |
| Ruby | `rand()`, `Random` | `SecureRandom` |
| Go | `rand` using `math/rand` package | `crypto.rand` package |
| Rust | `rand::rngs::SmallRng` and the other generators the `rand` docs mark as insecure | `rand::rng()` (ThreadRng), `rand::rngs::SysRng` |
| Node.js | `Math.random()` | `crypto.randomBytes()`, `crypto.randomInt()`, `crypto.randomUUID()` |

## UUIDs and GUIDs

Universally unique identifiers (UUIDs or GUIDs) are sometimes used as a quick way to generate random strings. Although they can provide a reasonable source of randomness, this will depend on the type or version of the UUID that is created.

Specifically, version 1 UUIDs are comprised of a high precision timestamp and the MAC address of the system that generated them, so are not random (although they may be hard to guess, given the timestamp is to the nearest 100ns). Type 4 UUIDs are randomly generated, although whether this is done using a CSPRNG will depend on the implementation. Unless this is known to be secure in the specific language or framework, the randomness of UUIDs should not be relied upon.

## Defense in Depth

Applications should be designed to still be secure even if cryptographic controls fail. Any information that is stored in an encrypted form should also be protected by additional layers of security. Application should also not rely on the security of encrypted URL parameters, and should enforce strong access control to prevent unauthorized access to information.