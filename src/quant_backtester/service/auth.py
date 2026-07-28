from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import jwt

from quant_backtester.service.settings import ServiceSettings


@dataclass(frozen=True)
class Principal:
    actor_id: str
    tenant_id: str
    roles: frozenset[str]

    def permits(self, *required: str) -> bool:
        return "admin" in self.roles or bool(self.roles.intersection(required))


class AuthenticationError(ValueError):
    pass


class Authenticator:
    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._jwks_client = (
            jwt.PyJWKClient(settings.oidc_jwks_url)
            if settings.auth_mode == "oidc" and settings.oidc_jwks_url
            else None
        )

    def authenticate(self, token: str | None) -> Principal:
        if self._settings.auth_mode == "disabled":
            return Principal(
                actor_id="development",
                tenant_id="default",
                roles=frozenset({"admin"}),
            )
        if not token:
            raise AuthenticationError("Missing bearer token")
        if self._settings.auth_mode == "api-key":
            return self._authenticate_api_key(token)
        return self._authenticate_oidc(token)

    def _authenticate_api_key(self, token: str) -> Principal:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for identity in self._settings.api_keys:
            if hmac.compare_digest(digest, identity.token_sha256):
                if not all((identity.actor_id, identity.tenant_id, identity.role)):
                    break
                return Principal(
                    actor_id=identity.actor_id,
                    tenant_id=identity.tenant_id,
                    roles=frozenset({identity.role}),
                )
        raise AuthenticationError("Invalid bearer token")

    def _authenticate_oidc(self, token: str) -> Principal:
        if self._jwks_client is None:
            raise AuthenticationError("OIDC verifier is not configured")
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("Invalid OIDC token") from exc
        return self._principal_from_claims(claims)

    def _principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        actor = str(claims.get("sub", "")).strip()
        tenant = str(claims.get(self._settings.oidc_tenant_claim, "")).strip()
        raw_roles = claims.get(self._settings.oidc_roles_claim, ())
        if isinstance(raw_roles, str):
            roles = frozenset(role for role in raw_roles.split() if role)
        elif isinstance(raw_roles, list):
            roles = frozenset(str(role) for role in raw_roles)
        else:
            roles = frozenset()
        if not actor or not tenant or not roles:
            raise AuthenticationError("OIDC token lacks subject, tenant, or roles")
        return Principal(actor_id=actor, tenant_id=tenant, roles=roles)
