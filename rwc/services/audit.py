import logging

from flask import has_request_context, request
from flask_login import current_user

from ..extensions import db
from ..models import AuditLog

log = logging.getLogger("rwc.audit")


def audit(action, entity_type=None, entity_id=None, detail=None, actor=None, actor_label=None, commit=False):
    """Record a security/ops relevant action. Added to the current session; pass commit=True for standalone use."""
    if actor is None and has_request_context() and getattr(current_user, "is_authenticated", False):
        actor = current_user
    ip = request.remote_addr if has_request_context() else None
    entry = AuditLog(
        actor_id=getattr(actor, "id", None),
        actor_label=actor_label or (actor.email if actor is not None else "anonymous"),
        action=action, entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None,
        detail=(detail or "")[:2000], ip=ip,
    )
    db.session.add(entry)
    log.info("audit action=%s entity=%s:%s actor=%s ip=%s", action, entity_type, entity_id, entry.actor_label, ip)
    if commit:
        db.session.commit()
    return entry
