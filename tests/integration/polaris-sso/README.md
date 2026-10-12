# Strict Polaris SSO integration fixtures

`run.sh` requires Docker, Maven, Java 25, and an explicit local `POLARIS_SOURCE` repository containing commit `0101ce955069af1bcf1dd4299b03250679d9b1e1`. It applies the tracked patch to a fresh disposable clone, compiles the pinned emulator, runs focused JUnit tests, then executes the two Java probes against a private loopback-only MariaDB container.

The probes call the compiled `SsoTicketConsumer` through Hikari and cover expiry, replay, byte-exact matching, replacement races, commit-boundary faults, 16-way concurrency, and duplicate-ticket fail-closed behavior. They do not claim full Netty handshake, HTTP token, reconnect, or production deployment compatibility.
