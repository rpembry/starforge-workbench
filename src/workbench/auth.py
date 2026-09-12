"""Fail-closed local API authentication for the NL-functional checkpoint.

This is a machine credential, not a browser password. Cloudflare JWT validation
and browser identity are a later deployment prerequisite, not inferred from headers.
"""
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from .repository import Problem


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


class Auth:
    def __init__(self, credentials: dict):
        if set(credentials) != {'operator', 'collector'}:
            raise RuntimeError('Credentials must contain separate operator and collector tokens')
        if any(not isinstance(v, str) or len(v) < 32 for v in credentials.values()):
            raise RuntimeError('Use randomly generated tokens of at least 32 characters')
        if credentials['operator'] == credentials['collector']:
            raise RuntimeError('Operator and collector tokens must differ')
        self.credentials = credentials

    @classmethod
    def from_file(cls, path: Path):
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError('Credentials must be owned by this user with mode 0600')
        return cls(json.loads(path.read_text()))

    def authenticate(self, request: Request):
        header = request.headers.get('authorization', '')
        if header.startswith('Bearer '):
            token = header[7:]
            for role, expected in self.credentials.items():
                if hmac.compare_digest(token, expected):
                    return Principal(role, role)
        raise Problem(401, 'unauthorized', 'A valid operator or collector bearer token is required')


class CloudflareAuth:
    """Validate Access signatures/issuer/audience before assigning a local role."""
    def __init__(self, issuer, audience, browser_emails, service_roles, jwks=None):
        import re
        import jwt
        if not re.fullmatch(r'https://[a-z0-9-]+\.cloudflareaccess\.com', issuer):
            raise RuntimeError('Cloudflare issuer must be a trusted team HTTPS origin')
        if not audience or not browser_emails or any(role not in {'operator', 'collector'} for role in service_roles.values()):
            raise RuntimeError('Cloudflare audience, browser allowlist, and valid roles are required')
        self.issuer, self.audience = issuer, audience
        self.browser_emails = {email.casefold() for email in browser_emails}
        self.service_roles = service_roles
        self.jwks = jwks or jwt.PyJWKClient(issuer+'/cdn-cgi/access/certs', cache_jwk_set=True, lifespan=300, timeout=5)

    def authenticate(self, request: Request):
        import jwt
        token = request.headers.get('cf-access-jwt-assertion', '')
        if not token or len(token) > 16384:
            raise Problem(401, 'unauthorized', 'Cloudflare Access authentication is required')
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=self.audience, issuer=self.issuer,
                                options={'require': ['exp', 'iat', 'aud', 'iss']}, leeway=10)
        except (jwt.PyJWTError, OSError, ValueError):
            raise Problem(401, 'unauthorized', 'Cloudflare Access token validation failed') from None
        service = claims.get('common_name')
        if service:
            role = self.service_roles.get(service)
            if role:
                return Principal('service:'+service, role)
        else:
            email = claims.get('email')
            if isinstance(email, str) and email.casefold() in self.browser_emails:
                return Principal(email.casefold(), 'operator')
        raise Problem(403, 'identity_not_allowed', 'This verified identity is not authorized for Workbench')

    @classmethod
    def from_file(cls, path: Path):
        if path.is_symlink():
            raise RuntimeError('Cloudflare authentication config must not be a symlink')
        data = json.loads(path.read_text())
        return cls(**data)
