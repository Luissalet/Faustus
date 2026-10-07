"""Compatibility provider for a local workspace; no credential store or users."""
from core.auth import ADMIN_PRIVILEGES
from src.constants import PASSWORD_MIN_LENGTH
from src.owner_identity import DEFAULT_LOCAL_OWNER


class LocalWorkspaceAccess:
    local_only = True
    is_configured = True
    signup_enabled = False
    users = {}

    def status(self, token=None):
        return {'configured':True, 'authenticated':True, 'username':None,
                'is_admin':True, 'auth_enabled':False, 'auth_disabled':True,
                'privileges':dict(ADMIN_PRIVILEGES)}

    def is_admin(self, owner):
        # Legacy routes call this capability check; it creates no account.
        return owner in (None, '', DEFAULT_LOCAL_OWNER)

    def get_privileges(self, owner):
        return dict(ADMIN_PRIVILEGES)

    def list_users(self):
        return []

    def validate_token(self, token):
        return False

    def get_username_for_token(self, token):
        return None

    def revoke_token(self, token):
        return None

    def policy(self):
        return {'password_min_length':PASSWORD_MIN_LENGTH}
