"""OAuth 2.1 bearer-token verification for the HTTP transport.

Only relevant when serving over HTTP. Tokens are validated locally against the
issuer's published signing keys — no introspection call, so a public PKCE
client needs no secret here.

Deliberately noisy on rejection: a token that fails validation logs *why*, plus
the claims it actually carried. A silent 401 during an OAuth handshake is close
to undebuggable from the client side, where all you see is "authentication
failed".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request

from mcp.server.auth.provider import AccessToken, TokenVerifier

try:
    import jwt
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise ModuleNotFoundError(
        "Verifying bearer tokens needs PyJWT and cryptography. Install the "
        "HTTP extra: pip install 'grocy-mcp[http]'."
    ) from exc

log = logging.getLogger(__name__)

# Asymmetric only, and pinned rather than read from the token header: accepting
# whatever `alg` a token claims is the classic JWT confusion attack (most
# infamously `alg: none`, but equally HS256 signed with the public key).
DEFAULT_ALGORITHMS = ["RS256", "ES256"]

# Tolerance for clock skew between this server and the issuer.
LEEWAY_SECONDS = 30

DISCOVERY_SUFFIX = "/.well-known/openid-configuration"


def discover_jwks_uri(issuer: str, timeout: int = 10) -> str:
    """Find the issuer's JWKS endpoint.

    Tries RFC 8414 discovery first. Kanidm is handled as a documented special
    case: it serves per-client keys at <issuer>/public_key.jwk and its issuer
    URL has a path, which discovery at the root would not find.
    """
    for url in (issuer + DISCOVERY_SUFFIX, issuer.rstrip("/") + "/public_key.jwk"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                doc = json.loads(resp.read())
        except Exception:
            continue
        if isinstance(doc, dict) and doc.get("jwks_uri"):
            return doc["jwks_uri"]
        # public_key.jwk is the key itself, not a document pointing at one.
        if isinstance(doc, dict) and (doc.get("kty") or doc.get("keys")):
            return url
    raise RuntimeError(
        f"Could not find a JWKS endpoint for issuer {issuer!r}. Set "
        "GROCY_MCP_OIDC_JWKS_URI explicitly."
    )


class OIDCTokenVerifier(TokenVerifier):
    """Validates a JWT access token offline against the issuer's JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | None = None,
        jwks_uri: str | None = None,
        required_scopes: list[str] | None = None,
        algorithms: list[str] | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.required_scopes = set(required_scopes or [])
        self.algorithms = algorithms or DEFAULT_ALGORITHMS
        self._jwks_uri = jwks_uri
        self._jwks = None

    def _keys(self):
        if self._jwks is None:
            uri = self._jwks_uri or discover_jwks_uri(self.issuer)
            # Caches keys in memory and only refetches when an unknown `kid`
            # shows up, so key rotation is picked up without a restart and the
            # steady state costs no network at all.
            self._jwks = jwt.PyJWKClient(uri, cache_keys=True, lifespan=3600)
        return self._jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        # PyJWKClient does blocking HTTP on a cache miss. This runs inside the
        # server's event loop, so it has to go to a thread or a single cold
        # fetch stalls every other in-flight request.
        try:
            return await asyncio.to_thread(self._verify_sync, token)
        except Exception:
            log.exception("token verification raised unexpectedly; rejecting")
            return None

    def _verify_sync(self, token: str) -> AccessToken | None:
        required = ["exp", "iss", "sub"] + (["aud"] if self.audience else [])
        try:
            signing_key = self._keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": required,
                    "verify_aud": bool(self.audience),
                },
            )
        except jwt.PyJWTError as exc:
            log.warning("rejecting token: %s: %s", type(exc).__name__, exc)
            self._log_unverified_claims(token)
            return None

        # `scope` is a space-delimited string per RFC 8693. Some issuers emit a
        # `scopes` list instead; accept either rather than failing shut on a
        # shape difference.
        raw = claims.get("scope") or claims.get("scopes") or ""
        scopes = raw.split() if isinstance(raw, str) else list(raw)

        missing = self.required_scopes - set(scopes)
        if missing:
            log.warning(
                "rejecting token for %s: missing scope(s) %s (had: %s)",
                claims.get("preferred_username") or claims.get("sub"),
                ", ".join(sorted(missing)),
                ", ".join(scopes) or "none",
            )
            return None

        return AccessToken(
            token=token,
            client_id=self.audience or claims.get("azp") or claims.get("sub"),
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=claims.get("sub"),
            claims=claims,
        )

    def _log_unverified_claims(self, token: str) -> None:
        """Show what the token actually said, to make a mismatch obvious.

        Signature-checked validation has already failed by this point, so these
        claims are untrusted and used for the log line only — never to make an
        access decision.
        """
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            log.warning("  (token is not a readable JWT at all)")
            return
        log.warning(
            "  token claimed iss=%r aud=%r sub=%r scope=%r; this server expects "
            "iss=%r aud=%r",
            unverified.get("iss"),
            unverified.get("aud"),
            unverified.get("sub"),
            unverified.get("scope") or unverified.get("scopes"),
            self.issuer,
            self.audience,
        )


def verifier_from_env(prefix: str = "GROCY_MCP") -> OIDCTokenVerifier:
    """Build a verifier from `<PREFIX>_OIDC_*` environment variables.

    Only `<PREFIX>_OIDC_ISSUER` is required; the JWKS endpoint is discovered
    from it unless `<PREFIX>_OIDC_JWKS_URI` says otherwise.
    """
    issuer = os.environ.get(f"{prefix}_OIDC_ISSUER", "").rstrip("/")
    if not issuer:
        raise RuntimeError(
            f"{prefix}_OIDC_ISSUER must be set to serve over HTTP with auth. "
            "Pass --no-auth to serve without it (localhost only)."
        )
    algorithms = os.environ.get(f"{prefix}_OIDC_ALGORITHMS", "")
    return OIDCTokenVerifier(
        issuer=issuer,
        audience=os.environ.get(f"{prefix}_OIDC_AUDIENCE") or None,
        jwks_uri=os.environ.get(f"{prefix}_OIDC_JWKS_URI") or None,
        required_scopes=os.environ.get(f"{prefix}_OIDC_SCOPES", "").split() or None,
        algorithms=algorithms.split() or None,
    )
