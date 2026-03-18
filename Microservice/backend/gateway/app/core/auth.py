"""
Nerve IDP — JWT Authentication and RBAC

Roles (in increasing privilege order):
  developer           → read catalog, submit deploys, scaffold services
  sre                 → + execute runbooks, unfreeze services, override blast radius
  platform_engineer   → + manage policies, bulk fleet operations, chaos experiments
  engineering_manager → + view cost and team data, read-only on everything

Role hierarchy is enforced per-endpoint via the require_role dependency.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

logger = logging.getLogger(__name__)

# RBAC role hierarchy — higher index = more privilege
ROLE_HIERARCHY = [
    "developer",
    "sre",
    "platform_engineer",
    "engineering_manager",
]

security_scheme = HTTPBearer()


class TokenData(BaseModel):
    sub: str           # username
    role: str          # RBAC role
    team: Optional[str] = None
    exp: Optional[datetime] = None


class CurrentUser(BaseModel):
    username: str
    role: str
    team: Optional[str] = None

    def has_role(self, required_role: str) -> bool:
        """
        Returns True if the user's role is >= the required role in the hierarchy.
        A platform_engineer can do anything a developer or SRE can do.
        """
        try:
            user_level = ROLE_HIERARCHY.index(self.role)
            required_level = ROLE_HIERARCHY.index(required_role)
            return user_level >= required_level
        except ValueError:
            return False


def create_access_token(
    username: str,
    role: str,
    team: Optional[str] = None,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": username,
        "role": role,
        "team": team,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload = {
        "sub": username,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> TokenData:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return TokenData(
            sub=payload["sub"],
            role=payload.get("role", "developer"),
            team=payload.get("team"),
            exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
) -> CurrentUser:
    """FastAPI dependency — extracts and validates JWT, returns CurrentUser."""
    token_data = decode_token(credentials.credentials)
    return CurrentUser(
        username=token_data.sub,
        role=token_data.role,
        team=token_data.team,
    )


def require_role(minimum_role: str):
    """
    FastAPI dependency factory — enforces minimum RBAC role.

    Usage:
        @router.post("/runbooks/{id}/execute")
        async def execute_runbook(
            current_user: CurrentUser = Depends(require_role("sre"))
        ):
            ...
    """
    async def _require_role(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if not current_user.has_role(minimum_role):
            logger.warning(
                "RBAC denied: user=%s role=%s required=%s",
                current_user.username,
                current_user.role,
                minimum_role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "message": f"This action requires the '{minimum_role}' role or higher. "
                               f"Your current role is '{current_user.role}'.",
                },
            )
        return current_user

    return _require_role


# ─────────────────────────────────────────────
# Internal auth — for Alertmanager freeze webhook
# ─────────────────────────────────────────────
async def require_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
) -> bool:
    """
    Simpler auth for internal service calls (e.g. Alertmanager → freeze endpoint).
    Uses a static bearer token, not a JWT.
    """
    if credentials.credentials != settings.NERVE_INTERNAL_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_internal_token", "message": "Invalid internal service token."},
        )
    return True
